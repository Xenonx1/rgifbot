import asyncio
import hashlib
import logging
import os
import re
import signal
import time
from collections import Counter
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from ddgs import DDGS
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultGif,
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

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

DDG_REGION = os.getenv("DDG_REGION", "us-en")
DDG_TIMEOUT = 10                 # seconds for the DuckDuckGo request itself
CANDIDATES = int(os.getenv("DDG_CANDIDATES", "80"))

SMALL_GIF_BYTES = 1024 * 1024    # Telegram's documented inline GIF limit (1 MB)
MAX_GIF_BYTES = int(float(os.getenv("MAX_GIF_MB", "5")) * 1024 * 1024)
DOWNLOAD_LIMIT = 20 * 1024 * 1024

VERIFY_CONCURRENCY = 30
VERIFY_TIMEOUT = 4               # seconds per GIF check
INLINE_BUDGET = 7.0              # total seconds for an inline query
SEARCH_BUDGET = 15.0             # total seconds for /search
INLINE_TARGET = 30
SEARCH_TARGET = 40
ENOUGH_SMALL = 15                # skip the 2nd DDG pass if we already have this many <=1MB GIFs

PAGE_SIZE = 10
CACHE_TTL = 600
CACHE_MAX = 200
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "1") != "0"

# Telegram's own fetcher identifies like this; some hosts treat it differently from a browser.
VERIFY_UA = "TelegramBot (like TwitterBot)"
GIF_MAGIC = (b"GIF87a", b"GIF89a")
NOT_GIF_EXT = (".jpg", ".jpeg", ".png", ".webp", ".svg", ".bmp", ".avif", ".ico", ".mp4", ".webm")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ddg-gif-bot")

DDG_SEM = asyncio.Semaphore(2)   # DuckDuckGo rate-limits aggressively
CACHE: dict[str, tuple[float, list[dict]]] = {}
INFLIGHT: dict[str, asyncio.Task] = {}
BACKGROUND: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- #
# DuckDuckGo search (images, GIF filter, SafeSearch OFF, DuckDuckGo backend only)
# --------------------------------------------------------------------------- #
def is_http_url(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(("https://", "http://"))
        and len(value) < 2000
    )


def _int(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def ddg_images(query: str, type_image: str | None, limit: int) -> tuple[list[dict], str | None]:
    """Blocking DuckDuckGo image search. Returns (items, error_text)."""
    error = None
    for attempt in range(2):
        try:
            items = DDGS(timeout=DDG_TIMEOUT).images(
                query,
                region=DDG_REGION,
                safesearch="off",          # SafeSearch OFF
                type_image=type_image,     # "gif" filter (only a hint; we verify below)
                max_results=limit,
                backend="duckduckgo",      # never fall back to Bing etc.
            )
            return list(items or []), None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("DuckDuckGo search failed (attempt %d): %s", attempt + 1, error)
            if "no results" in error.lower():
                break
            time.sleep(1.0)
    return [], error


def to_candidate(item: dict, order: int) -> dict | None:
    url = item.get("image")
    if not is_http_url(url):
        return None
    path = urlparse(url).path.lower()
    if path.endswith(NOT_GIF_EXT):
        return None
    return {
        "gif_url": url,
        "title": (item.get("title") or "GIF")[:100],
        "width": _int(item.get("width")),
        "height": _int(item.get("height")),
        "order": order,
        "gif_ext": path.endswith(".gif"),
    }


# --------------------------------------------------------------------------- #
# GIF verification: only keep URLs that really serve a GIF file
# --------------------------------------------------------------------------- #
async def verify_gif(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    cand: dict,
    fails: Counter,
) -> dict | None:
    try:
        async with sem:
            async with session.get(
                cand["gif_url"],
                headers={
                    "Range": "bytes=0-2047",
                    "Accept": "image/gif,image/*;q=0.8,*/*;q=0.5",
                },
                max_redirects=3,
            ) as resp:
                if resp.status not in (200, 206):
                    fails[f"http_{resp.status}"] += 1
                    return None
                try:
                    head = await resp.content.readexactly(6)
                except asyncio.IncompleteReadError:
                    fails["empty"] += 1
                    return None
                if head not in GIF_MAGIC:
                    fails["not_a_gif"] += 1
                    return None

                size = None
                if resp.status == 206:
                    match = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                    if match:
                        size = int(match.group(1))
                elif resp.content_length is not None:
                    size = resp.content_length

                if size is not None and size > MAX_GIF_BYTES:
                    fails["too_big"] += 1
                    return None

                if size is None:
                    rank = 1
                elif size <= SMALL_GIF_BYTES:
                    rank = 0
                else:
                    rank = 2
                return {**cand, "size": size, "rank": rank}
    except asyncio.TimeoutError:
        fails["timeout"] += 1
    except aiohttp.ClientError:
        fails["connection_error"] += 1
    except Exception:
        fails["other_error"] += 1
    return None


async def verify_many(
    session: aiohttp.ClientSession,
    cands: list[dict],
    deadline: float,
    target: int,
    fails: Counter,
) -> list[dict]:
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    tasks = [asyncio.create_task(verify_gif(session, sem, c, fails)) for c in cands]
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
                result = task.result()
                if result:
                    found.append(result)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return found


async def find_gifs(
    session: aiohttp.ClientSession,
    query: str,
    budget: float,
    target: int,
    stats: list | None = None,
) -> list[dict]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    seen: set[str] = set()
    verified: list[dict] = []
    fails: Counter = Counter()

    # Pass 1: DuckDuckGo's own GIF filter. Pass 2: plain "<query> gif" as a fallback.
    passes = [(query, "gif"), (f"{query} gif", None)]
    for pass_no, (q, kind) in enumerate(passes):
        remaining = deadline - loop.time()
        if remaining < 2.0:
            break

        try:
            async with DDG_SEM:
                items, error = await asyncio.wait_for(
                    asyncio.to_thread(ddg_images, q, kind, CANDIDATES),
                    timeout=remaining - 1.0,
                )
        except asyncio.TimeoutError:
            items, error = [], "DuckDuckGo search timed out"

        cands = []
        for index, item in enumerate(items):
            cand = to_candidate(item, index)
            if cand and cand["gif_url"] not in seen:
                seen.add(cand["gif_url"])
                cand["pass"] = pass_no
                cands.append(cand)
        cands.sort(key=lambda c: not c["gif_ext"])  # stable: .gif URLs first

        found = await verify_many(session, cands, deadline, target - len(verified), fails)
        verified.extend(found)

        if stats is not None:
            stats.append({
                "query": q,
                "type": kind,
                "raw": len(items),
                "candidates": len(cands),
                "verified": len(found),
                "small": sum(1 for f in found if f["rank"] == 0),
                "error": error,
            })

        small = sum(1 for v in verified if v["rank"] == 0)
        if small >= ENOUGH_SMALL or len(verified) >= target:
            break

    verified.sort(key=lambda v: (v["rank"], v["pass"], v["order"]))
    log.info("search %r -> %d GIFs (verify failures: %s)", query, len(verified), dict(fails))
    if stats is not None:
        stats.append({"fails": dict(fails)})
    return verified[:50]


async def get_gifs(
    session: aiohttp.ClientSession, query: str, budget: float, target: int
) -> list[dict]:
    key = query.casefold()
    hit = CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    task = INFLIGHT.get(key)
    if task is None:
        task = asyncio.create_task(find_gifs(session, query, budget, target))
        INFLIGHT[key] = task
        task.add_done_callback(lambda _t, k=key: INFLIGHT.pop(k, None))
    results = await asyncio.shield(task)

    if results:
        if len(CACHE) >= CACHE_MAX:
            CACHE.pop(next(iter(CACHE)))
        CACHE[key] = (time.monotonic(), results)
    return results


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "DuckDuckGo GIF Search Bot\n\n"
        "Private search: /search cats\n"
        "Inline search: type @YourBotUsername cats in any Telegram chat.\n\n"
        "DuckDuckGo only. SafeSearch is OFF. Only real GIF files are returned."
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /search cats")
        return

    status = await update.effective_message.reply_text("Searching DuckDuckGo for GIFs…")
    session = context.application.bot_data["http"]
    results = await get_gifs(session, query, SEARCH_BUDGET, SEARCH_TARGET)
    context.user_data["ddg_results"] = {"query": query, "items": results}

    if not results:
        await status.edit_text(
            "No working GIFs found for that search. Try different keywords.\n"
            "(Admin tip: /debug " + query + " shows what went wrong.)"
        )
        return

    await show_page(status, context, 0)


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /debug cats")
        return

    status = await update.effective_message.reply_text("Running diagnostics…")
    session = context.application.bot_data["http"]
    stats: list = []
    results = await find_gifs(session, query, SEARCH_BUDGET, SEARCH_TARGET, stats)

    lines = [f"Query: {query}", f"Final GIFs: {len(results)}"]
    for entry in stats:
        if "fails" in entry:
            lines.append(f"Verify failures: {entry['fails'] or 'none'}")
        else:
            lines.append(
                f"Search '{entry['query']}' (type={entry['type']}): "
                f"raw {entry['raw']}, tested {entry['candidates']}, "
                f"real GIFs {entry['verified']} (<=1MB: {entry['small']})"
            )
            if entry["error"]:
                lines.append(f"  DDG error: {entry['error']}")
    if results:
        lines.append(f"First: {results[0]['gif_url'][:150]}")
    await status.edit_text("\n".join(lines)[:3900], disable_web_page_preview=True)


async def show_page(message, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    state = context.user_data.get("ddg_results", {})
    items = state.get("items", [])
    query = state.get("query", "")
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))

    first = page * PAGE_SIZE
    current = items[first:first + PAGE_SIZE]
    keyboard = []

    for offset, item in enumerate(current):
        index = first + offset
        title = item.get("title") or f"GIF {index + 1}"
        keyboard.append([
            InlineKeyboardButton(f"{index + 1}. {title[:35]}", callback_data=f"send:{index}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    try:
        await message.edit_text(
            f"DuckDuckGo GIF results for: {query}\n"
            f"Showing {first + 1}–{first + len(current)} of {len(items)}.\n"
            "Tap a result to send it.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except TelegramError as exc:
        log.debug("edit_text skipped: %s", exc)


async def download_gif(session: aiohttp.ClientSession, url: str) -> bytes | None:
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
        return data if data[:6] in GIF_MAGIC else None
    except Exception:
        return None


async def send_gif(context: ContextTypes.DEFAULT_TYPE, chat_id: int, item: dict) -> bool:
    caption = (item.get("title") or "GIF")[:900]
    try:
        await context.bot.send_animation(chat_id=chat_id, animation=item["gif_url"], caption=caption)
        return True
    except TelegramError as exc:
        log.info("Telegram could not fetch the URL (%s); uploading the file instead", exc)

    # Fallback: the source host blocks Telegram's fetcher, so download and upload it ourselves.
    data = await download_gif(context.application.bot_data["http"], item["gif_url"])
    if not data:
        return False
    try:
        await context.bot.send_animation(
            chat_id=chat_id,
            animation=InputFile(data, filename="duckduckgo.gif"),
            caption=caption,
        )
        return True
    except TelegramError:
        log.exception("Uploading the downloaded GIF failed")
        return False


async def result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    kind, _, arg = (callback.data or "").partition(":")
    state = context.user_data.get("ddg_results")

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
        ok = await send_gif(context, update.effective_chat.id, items[number])
        if not ok:
            await context.bot.send_message(
                update.effective_chat.id,
                "Telegram could not load that GIF. Try another result.",
            )


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    query = (inline_query.query or "").strip()

    if len(query) < 2:
        await safe_answer(inline_query, [], cache_time=2)
        return

    session = context.application.bot_data["http"]
    items = await get_gifs(session, query, INLINE_BUDGET, INLINE_TARGET)

    results = []
    for item in items[:50]:
        result_id = hashlib.sha256(item["gif_url"].encode()).hexdigest()[:32]
        extra = {}
        if item.get("width") and item.get("height"):
            extra = {"gif_width": item["width"], "gif_height": item["height"]}
        results.append(
            InlineQueryResultGif(
                id=result_id,
                gif_url=item["gif_url"],
                # The file is verified to be a real GIF, so it doubles as its own thumbnail.
                thumbnail_url=item["gif_url"],
                thumbnail_mime_type="image/gif",
                title=item["title"][:60],
                **extra,
            )
        )

    await safe_answer(inline_query, results, cache_time=60 if results else 3)


async def safe_answer(inline_query, results, cache_time: int) -> None:
    try:
        await inline_query.answer(results, cache_time=cache_time, is_personal=False)
    except TelegramError as exc:
        # Usually "query is too old": the user kept typing, nothing to do.
        log.info("Could not answer inline query: %s", exc)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Web server (Render health check + Telegram webhook)
# --------------------------------------------------------------------------- #
async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": "duckduckgo-gif-search"})


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

    # Return immediately so Telegram never times out or retries while we search.
    task = asyncio.create_task(process_update_safely(app, update))
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return web.Response(text="ok")


async def keep_alive(session: aiohttp.ClientSession) -> None:
    """Ping our own public URL so Render's free plan doesn't put the service to sleep."""
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
        headers={"User-Agent": VERIFY_UA},
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

    # A private webhook path avoids exposing the bot token in the URL.
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
    log.info("DuckDuckGo GIF bot started; webhook configured.")

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
        # Do NOT delete the webhook here: during a Render redeploy the old instance
        # shuts down after the new one registered its webhook, which would break the bot.
        if keep_alive_task:
            keep_alive_task.cancel()
        await runner.cleanup()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
