import asyncio
import hashlib
import logging
import os
import re
import signal
import time
from collections import Counter
from urllib.parse import quote

import aiohttp
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultMpeg4Gif,
    InputFile,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

from gifreels import SITE, add_page, media_url, parse_listing, slugify

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# Where to look for results. {slug} = "big-tits", {q} = url-encoded query.
# If you find the site's real search URL (browser dev tools > Network), set it
# in Render as GIFREELS_SEARCH_URL, e.g.  https://gifreels.com/search?q={q}
SEARCH_TEMPLATES = [
    t for t in (
        os.getenv("GIFREELS_SEARCH_URL"),
        f"{SITE}/tag/{{slug}}",
        f"{SITE}/search?q={{q}}",
    ) if t
]
PAGE_PARAM = os.getenv("GIFREELS_PAGE_PARAM", "page")   # ?page=2 style paging
MAX_PAGES_PER_REQUEST = 6
MAX_PAGE = 30

SMALL_BYTES = 1024 * 1024        # Telegram's documented inline limit (1 MB)
MAX_BYTES = int(float(os.getenv("MAX_GIF_MB", "5")) * 1024 * 1024)
DOWNLOAD_LIMIT = 20 * 1024 * 1024

VERIFY_CONCURRENCY = 20
VERIFY_TIMEOUT = 4
PAGE_TIMEOUT = 8
INLINE_BUDGET = 8.0
SEARCH_BUDGET = 15.0
INLINE_TARGET = 40
SEARCH_TARGET = 40
PAGE_SIZE = 10
CACHE_TTL = 600
CACHE_MAX = 200
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "1") != "0"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TELEGRAM_UA = "TelegramBot (like TwitterBot)"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gifreels-bot")

SITE_SEM = asyncio.Semaphore(4)
PAGE_CACHE: dict[tuple[str, int], tuple[float, list[dict], bool]] = {}
TEMPLATE_FOR: dict[str, str] = {}
SEEN: dict[str, set] = {}
INFLIGHT: dict[tuple[str, int], asyncio.Task] = {}
BACKGROUND: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- #
# gifreels.com scraping
# --------------------------------------------------------------------------- #
async def fetch_listing(session: aiohttp.ClientSession, url: str, fails: Counter):
    """Returns (html or None, status or error text)."""
    try:
        async with SITE_SEM:
            async with session.get(
                url,
                headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    fails[f"page_http_{resp.status}"] += 1
                    return None, resp.status
                return await resp.text(errors="ignore"), 200
    except asyncio.TimeoutError:
        fails["page_timeout"] += 1
        return None, "timeout"
    except aiohttp.ClientError as exc:
        fails["page_connection_error"] += 1
        return None, type(exc).__name__


async def fetch_page_candidates(
    session: aiohttp.ClientSession,
    query: str,
    page: int,
    fails: Counter,
    stats: list | None,
) -> list[dict]:
    """Candidate GIFs (not yet verified) from one page of the site's listing."""
    key = query.casefold()
    slug = slugify(query)
    templates = SEARCH_TEMPLATES
    if page > 1 and key in TEMPLATE_FOR:
        templates = [TEMPLATE_FOR[key]]

    for template in templates:
        base = template.replace("{slug}", slug).replace("{q}", quote(query))
        url = base if page == 1 else add_page(base, page, PAGE_PARAM)
        html, status = await fetch_listing(session, url, fails)
        entry = {"url": url, "status": status, "ids": 0, "new": 0}
        if stats is not None:
            stats.append(entry)
        if not html:
            continue

        items = parse_listing(html)
        entry["ids"] = len(items)
        if not items:
            continue

        if page == 1:
            TEMPLATE_FOR[key] = template
            SEEN[key] = set()
        seen = SEEN.setdefault(key, set())
        new = [i for i in items if i["id"] not in seen]
        entry["new"] = len(new)
        seen.update(i["id"] for i in new)
        return [
            {"id": i["id"], "title": i["title"], "media_url": media_url(i["id"]), "order": n}
            for n, i in enumerate(new)
        ]
    return []


# --------------------------------------------------------------------------- #
# Check that each MP4 really exists and is small enough for Telegram
# --------------------------------------------------------------------------- #
async def verify_media(session, sem, cand: dict, fails: Counter) -> dict | None:
    try:
        async with sem:
            async with session.get(
                cand["media_url"],
                headers={"User-Agent": TELEGRAM_UA, "Range": "bytes=0-1023", "Accept": "*/*"},
                max_redirects=3,
            ) as resp:
                if resp.status not in (200, 206):
                    fails[f"media_http_{resp.status}"] += 1
                    return None
                try:
                    head = await resp.content.readexactly(12)
                except asyncio.IncompleteReadError:
                    fails["media_empty"] += 1
                    return None
                if head[4:8] != b"ftyp":
                    fails["media_not_mp4"] += 1
                    return None

                size = None
                if resp.status == 206:
                    match = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                    if match:
                        size = int(match.group(1))
                elif resp.content_length is not None:
                    size = resp.content_length

                if size is not None and size > MAX_BYTES:
                    fails["media_too_big"] += 1
                    return None
                rank = 1 if size is None else (0 if size <= SMALL_BYTES else 2)
                return {**cand, "size": size, "rank": rank}
    except asyncio.TimeoutError:
        fails["media_timeout"] += 1
    except aiohttp.ClientError:
        fails["media_connection_error"] += 1
    except Exception:
        fails["media_other_error"] += 1
    return None


async def verify_many(session, cands, deadline, target, fails) -> list[dict]:
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    tasks = [asyncio.create_task(verify_media(session, sem, c, fails)) for c in cands]
    found: list[dict] = []
    pending = set(tasks)
    try:
        while pending and len(found) < target:
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.result():
                    found.append(task.result())
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return found


async def get_page(session, query: str, page: int, budget: float, stats: list | None = None):
    """One site page -> (usable GIFs, has_more). Cached; concurrent identical calls share work."""
    key = (query.casefold(), page)
    hit = PAGE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL and stats is None:
        return hit[1], hit[2]

    async def work():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        fails: Counter = Counter()
        cands = await fetch_page_candidates(session, query, page, fails, stats)
        verified = await verify_many(session, cands, deadline, len(cands), fails)
        verified.sort(key=lambda v: (v["rank"], v["order"]))
        log.info("search %r page %d -> %d candidates, %d usable (%s)",
                 query, page, len(cands), len(verified), dict(fails))
        if stats is not None:
            stats.append({"page": page, "candidates": len(cands), "usable": len(verified),
                          "fails": dict(fails)})
        return verified, bool(cands)

    task = INFLIGHT.get(key)
    if task is None:
        task = asyncio.create_task(work())
        INFLIGHT[key] = task
        task.add_done_callback(lambda _t, k=key: INFLIGHT.pop(k, None))
    verified, has_more = await asyncio.shield(task)

    if has_more:
        if len(PAGE_CACHE) >= CACHE_MAX:
            PAGE_CACHE.pop(next(iter(PAGE_CACHE)))
        PAGE_CACHE[key] = (time.monotonic(), verified, has_more)
    return verified, has_more


async def collect(session, query: str, start_page: int, target: int, budget: float,
                  stats: list | None = None):
    """Gather site pages until `target` usable GIFs. Returns (items, next_page, ended)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    results: list[dict] = []
    page = start_page
    ended = False

    while len(results) < target and page < start_page + MAX_PAGES_PER_REQUEST and page <= MAX_PAGE:
        remaining = deadline - loop.time()
        if remaining < 2.0:
            break
        items, has_more = await get_page(session, query, page, remaining, stats)
        results.extend(items)
        page += 1
        if not has_more:
            ended = True
            break
    return results[:50], page, ended or page > MAX_PAGE


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "GifReels Search Bot (18+ adult content)\n\n"
        "Private search: /search big tits\n"
        "Inline search: type @YourBotUsername latina in any chat, "
        "pick a result and it is sent to that chat.\n\n"
        "Results come from gifreels.com."
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /search latina")
        return

    status = await update.effective_message.reply_text("Searching gifreels.com…")
    session = context.application.bot_data["http"]
    results, _, _ = await collect(session, query, 1, SEARCH_TARGET, SEARCH_BUDGET)
    context.user_data["results"] = {"query": query, "items": results}

    if not results:
        await status.edit_text(
            f"No GIFs found for “{query}”. Try a simpler keyword (e.g. a tag like “latina”).\n"
            f"Admin: /debug {query}"
        )
        return
    await show_page(status, context, 0)


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /debug latina")
        return

    status = await update.effective_message.reply_text("Running diagnostics…")
    session = context.application.bot_data["http"]
    stats: list = []
    results, next_page, ended = await collect(
        session, query, 1, SEARCH_TARGET, SEARCH_BUDGET, stats
    )

    lines = [f"Query: {query}", f"Usable GIFs: {len(results)} (more pages: {'no' if ended else 'yes'})"]
    for entry in stats:
        if "fails" in entry:
            lines.append(
                f"Page {entry['page']}: candidates {entry['candidates']}, usable {entry['usable']}, "
                f"failures {entry['fails'] or 'none'}"
            )
        else:
            lines.append(
                f"GET {entry['url']} -> {entry['status']}, gif links {entry['ids']}, new {entry['new']}"
            )
    if results:
        lines.append(f"First: {results[0]['media_url']} ({results[0].get('size')} bytes)")
    await status.edit_text("\n".join(lines)[:3900], disable_web_page_preview=True)


async def show_page(message, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    state = context.user_data.get("results", {})
    items = state.get("items", [])
    query = state.get("query", "")
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))

    first = page * PAGE_SIZE
    current = items[first:first + PAGE_SIZE]
    keyboard = [
        [InlineKeyboardButton(
            f"{first + i + 1}. {(item.get('title') or 'GIF')[:35]}",
            callback_data=f"send:{first + i}",
        )]
        for i, item in enumerate(current)
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    try:
        await message.edit_text(
            f"GifReels results for: {query}\n"
            f"Showing {first + 1}–{first + len(current)} of {len(items)}.\n"
            "Tap a result to send it.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except TelegramError as exc:
        log.debug("edit_text skipped: %s", exc)


async def download_media(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=30), max_redirects=3
        ) as resp:
            if resp.status != 200:
                return None
            if resp.content_length and resp.content_length > DOWNLOAD_LIMIT:
                return None
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                buf += chunk
                if len(buf) > DOWNLOAD_LIMIT:
                    return None
        data = bytes(buf)
        return data if data[4:8] == b"ftyp" else None
    except Exception:
        return None


async def send_gif(context: ContextTypes.DEFAULT_TYPE, chat_id: int, item: dict) -> bool:
    caption = (item.get("title") or "")[:900] or None
    try:
        await context.bot.send_animation(chat_id=chat_id, animation=item["media_url"], caption=caption)
        return True
    except TelegramError as exc:
        log.info("Telegram could not fetch the URL (%s); uploading the file instead", exc)

    data = await download_media(context.application.bot_data["http"], item["media_url"])
    if not data:
        return False
    try:
        await context.bot.send_animation(
            chat_id=chat_id,
            animation=InputFile(data, filename="gifreels.mp4"),
            caption=caption,
        )
        return True
    except TelegramError:
        log.exception("Uploading the downloaded GIF failed")
        return False


async def result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    kind, _, arg = (callback.data or "").partition(":")
    state = context.user_data.get("results")

    if not state:
        await callback.answer("Results expired. Please search again.", show_alert=True)
        return
    try:
        number = int(arg)
    except ValueError:
        await callback.answer()
        return

    if kind == "page":
        await callback.answer()
        await show_page(callback.message, context, number)
        return

    if kind == "send":
        items = state.get("items", [])
        if not 0 <= number < len(items):
            await callback.answer("Result unavailable. Search again.", show_alert=True)
            return
        await callback.answer("Sending…")
        if not await send_gif(context, update.effective_chat.id, items[number]):
            await context.bot.send_message(
                update.effective_chat.id, "Telegram could not load that GIF. Try another result."
            )


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    query = (inline_query.query or "").strip()

    if len(query) < 2:
        await safe_answer(inline_query, [], cache_time=2)
        return

    try:
        offset = max(1, int(inline_query.offset or 1))
    except ValueError:
        offset = 1

    session = context.application.bot_data["http"]
    items, next_page, ended = await collect(session, query, offset, INLINE_TARGET, INLINE_BUDGET)

    results = []
    used = set()
    for item in items:
        if item["id"] in used:
            continue
        used.add(item["id"])
        results.append(
            InlineQueryResultMpeg4Gif(
                id=item["id"][:64],
                mpeg4_url=item["media_url"],
                # The MP4 doubles as its own animated preview (Telegram accepts video/mp4 thumbnails).
                thumbnail_url=item["media_url"],
                thumbnail_mime_type="video/mp4",
                title=(item.get("title") or "GIF")[:60],
            )
        )

    # next_offset makes Telegram ask for the next batch when the user scrolls to the bottom.
    next_offset = "" if (ended or not results) else str(next_page)
    await safe_answer(inline_query, results, cache_time=60 if results else 3, next_offset=next_offset)


async def safe_answer(inline_query, results, cache_time: int, next_offset: str = "") -> None:
    try:
        await inline_query.answer(
            results, cache_time=cache_time, is_personal=False, next_offset=next_offset
        )
    except TelegramError as exc:
        log.info("Could not answer inline query: %s", exc)  # usually "query is too old"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Web server (Render health check + Telegram webhook)
# --------------------------------------------------------------------------- #
async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": "gifreels-search"})


async def process_update_safely(app: Application, update: Update) -> None:
    try:
        await app.process_update(update)
    except Exception:
        log.exception("Could not process Telegram update")


async def telegram_webhook(request: web.Request) -> web.Response:
    app: Application = request.app["telegram_app"]
    try:
        update = Update.de_json(await request.json(), app.bot)
    except Exception:
        return web.Response(status=400, text="bad request")

    # Reply instantly so Telegram never times out or retries while we search.
    task = asyncio.create_task(process_update_safely(app, update))
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return web.Response(text="ok")


async def keep_alive(session: aiohttp.ClientSession) -> None:
    """Ping our own public URL so Render's free plan doesn't sleep."""
    while True:
        await asyncio.sleep(600)
        try:
            async with session.get(f"{RENDER_URL}/health") as resp:
                await resp.read()
        except Exception as exc:
            log.warning("Keep-alive ping failed: %s", exc)


async def main() -> None:
    if not RENDER_URL.startswith("https://"):
        raise RuntimeError(
            "Render did not provide RENDER_EXTERNAL_URL. Deploy this as a Render Web Service."
        )

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=VERIFY_TIMEOUT),
        connector=aiohttp.TCPConnector(limit=60, ttl_dns_cache=300),
        headers={"User-Agent": TELEGRAM_UA},
    )

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.bot_data["http"] = session
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", start))
    telegram_app.add_handler(CommandHandler("search", search_command))
    telegram_app.add_handler(CommandHandler("debug", debug_command))
    telegram_app.add_handler(CallbackQueryHandler(result_callback, pattern=r"^(send|page):"))
    telegram_app.add_handler(InlineQueryHandler(inline_search))
    telegram_app.add_error_handler(on_error)

    secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
    webhook_path = f"/telegram/{secret}"

    await telegram_app.initialize()
    await telegram_app.start()

    web_app = web.Application()
    web_app["telegram_app"] = telegram_app
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    web_app.router.add_post(webhook_path, telegram_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await telegram_app.bot.set_webhook(
        url=f"{RENDER_URL}{webhook_path}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        max_connections=40,
    )
    log.info("GifReels bot started; webhook configured.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    keep_alive_task = asyncio.create_task(keep_alive(session)) if KEEP_ALIVE else None
    try:
        await stop.wait()
    finally:
        # Don't delete the webhook: during a Render redeploy the old instance shuts down
        # after the new one registered it, which would break the bot.
        if keep_alive_task:
            keep_alive_task.cancel()
        await runner.cleanup()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
