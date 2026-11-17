import asyncio
import hashlib
import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from aiohttp import web
from ddgs import DDGS
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultGif,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_URL", os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
PAGE_SIZE = 10
INLINE_PAGE_SIZE = 25
SEARCH_TIMEOUT = 18

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gifbot")


def _valid_http_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 4000:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)



async def _is_real_gif(client: httpx.AsyncClient, url: str) -> bool:
    """Check the actual response bytes; search engines can label static images as GIFs."""
    if not _valid_http_url(url):
        return False
    try:
        async with client.stream(
            "GET",
            url,
            headers={
                "Range": "bytes=0-15",
                "User-Agent": "Mozilla/5.0 (compatible; TelegramGifSearchBot/1.0)",
            },
        ) as response:
            if response.status_code not in (200, 206):
                return False
            content_type = response.headers.get("content-type", "").lower()
            first_bytes = b""
            async for chunk in response.aiter_bytes():
                first_bytes += chunk
                if len(first_bytes) >= 6:
                    break
            # GIF87a and GIF89a are the file signatures. MIME alone is not trusted.
            # The byte signature is authoritative; some CDNs mislabel MIME types.
            return first_bytes[:6] in (b"GIF87a", b"GIF89a")
    except Exception:
        return False


async def keep_real_gifs(items: list[dict], limit: int = 25) -> list[dict]:
    """Filter dead URLs, HTML pages, thumbnails, and static images before Telegram sees them."""
    timeout = httpx.Timeout(6.0, connect=4.0)
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, limits=limits
    ) as client:
        semaphore = asyncio.Semaphore(10)

        async def check(item: dict) -> tuple[dict, bool]:
            async with semaphore:
                return item, await _is_real_gif(client, item["gif_url"])

        checked = await asyncio.gather(*(check(item) for item in items))
    return [item for item, valid in checked if valid][:limit]


def search_ddg(query: str, limit: int = 25) -> list[dict]:
    """DuckDuckGo image search. Provider results are not guaranteed to stay stable."""
    found = []
    try:
        with DDGS(timeout=SEARCH_TIMEOUT) as ddgs:
            rows = ddgs.images(
                query,
                region="wt-wt",
                safesearch="off",
                type_image="gif",
                max_results=limit,
            )
            for row in rows:
                image_url = row.get("image")
                thumb = row.get("thumbnail") or image_url
                if _valid_http_url(image_url):
                    found.append({
                        "gif_url": image_url,
                        "thumb_url": thumb if _valid_http_url(thumb) else image_url,
                        "title": (row.get("title") or "GIF")[:180],
                        "width": row.get("width"),
                        "height": row.get("height"),
                        "source": "DuckDuckGo",
                    })
    except Exception:
        log.exception("DuckDuckGo search failed")
    return found


async def search_google(query: str, limit: int = 10) -> list[dict]:
    """Google Custom Search JSON API. Requires a configured API key and CSE ID."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "searchType": "image",
        "fileType": "gif",
        "safe": "off",
        "num": min(limit, 10),
    }
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()

        found = []
        for row in payload.get("items", []):
            image_url = row.get("link")
            image_info = row.get("image") or {}
            thumb = image_info.get("thumbnailLink") or image_url
            if _valid_http_url(image_url):
                found.append({
                    "gif_url": image_url,
                    "thumb_url": thumb if _valid_http_url(thumb) else image_url,
                    "title": (row.get("title") or "GIF")[:180],
                    "width": image_info.get("width"),
                    "height": image_info.get("height"),
                    "source": "Google",
                })
        return found
    except Exception:
        log.exception("Google image search failed")
        return []


async def search_all(query: str, limit: int = 35) -> list[dict]:
    ddg_task = asyncio.to_thread(search_ddg, query, 25)
    google_task = search_google(query, 10)
    ddg_rows, google_rows = await asyncio.gather(ddg_task, google_task)

    # Mix the providers so one provider does not dominate the first screen.
    combined = []
    for index in range(max(len(google_rows), len(ddg_rows))):
        if index < len(google_rows):
            combined.append(google_rows[index])
        if index < len(ddg_rows):
            combined.append(ddg_rows[index])

    unique, seen = [], set()
    for item in combined:
        url = item["gif_url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(item)
        if len(unique) >= limit:
            break
    # Search providers sometimes return a static thumbnail or an HTML page
    # despite the GIF filter. Only pass real GIF files to Telegram.
    return await keep_real_gifs(unique, limit=limit)


def _dimension(value: Any) -> int | None:
    try:
        number = int(value)
        return number if 1 <= number <= 10000 else None
    except (TypeError, ValueError):
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hi! I can search animated GIFs.\n\n"
        "Private search: /search your keywords\n"
        "Inline search: type @YourBotUsername your keywords in any chat.\n\n"
        "Enable inline mode for this bot through @BotFather first."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "Use: /search funny cats\n"
            "Or type @YourBotUsername funny cats in any Telegram chat."
        )
        return

    status = await update.effective_message.reply_text("Searching DuckDuckGo and Google…")
    results = await search_all(query, limit=50)
    context.user_data["gif_search"] = {"query": query, "results": results}

    if not results:
        await status.edit_text(
            "No directly accessible animated GIF files were found. Try different keywords. "
            "Some search results are static previews or block Telegram access. "
            "Google results also require a valid Google API key and Search Engine ID."
        )
        return

    await show_private_page(status, context, page=0)


async def show_private_page(message, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    state = context.user_data.get("gif_search") or {}
    results = state.get("results", [])
    query = state.get("query", "")
    total_pages = max(1, (len(results) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    state["page"] = page

    start_index = page * PAGE_SIZE
    current = results[start_index:start_index + PAGE_SIZE]
    rows = []
    for local_index, item in enumerate(current):
        absolute_index = start_index + local_index
        title = item.get("title") or f"GIF {absolute_index + 1}"
        label = f"{absolute_index + 1}. {title[:32]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"gif:{absolute_index}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"page:{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)

    text = (
        f"GIF results for: {query}\n"
        f"Showing {start_index + 1}–{start_index + len(current)} of {len(results)}.\n"
        "Tap a result to send that GIF."
    )
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def private_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("page:"):
        try:
            page = int(data.split(":", 1)[1])
            await show_private_page(query.message, context, page)
        except (ValueError, AttributeError):
            await query.answer("That page is no longer available.", show_alert=True)
        return

    if data.startswith("gif:"):
        try:
            index = int(data.split(":", 1)[1])
            results = context.user_data.get("gif_search", {}).get("results", [])
            item = results[index]
        except (ValueError, IndexError, KeyError):
            await query.answer("That result is no longer available. Search again.", show_alert=True)
            return

        try:
            await context.bot.send_animation(
                chat_id=query.message.chat_id,
                animation=item["gif_url"],
                caption=(item.get("title") or "GIF")[:900],
            )
        except Exception:
            log.exception("Telegram could not fetch/send selected GIF")
            await query.answer(
                "Telegram couldn't load this GIF from its source. Try another result.",
                show_alert=True,
            )


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = (update.inline_query.query or "").strip()
    if not query_text:
        await update.inline_query.answer(
            [],
            cache_time=2,
            switch_pm_text="Search GIFs",
            switch_pm_parameter="search",
        )
        return

    try:
        results = await search_all(query_text, limit=35)
        inline_results = []
        for item in results[:50]:
            kwargs = {
                "id": hashlib.sha256(item["gif_url"].encode()).hexdigest()[:32],
                "gif_url": item["gif_url"],
                "thumbnail_url": item["thumb_url"],
                "title": (item.get("title") or "GIF")[:60],
            }
            # Deliberately omit provider width/height: incorrect dimensions can
            # make Telegram's inline preview render with a broken aspect ratio.
            inline_results.append(InlineQueryResultGif(**kwargs))

        await update.inline_query.answer(
            inline_results,
            cache_time=15,
            is_personal=True,
        )
    except Exception:
        log.exception("Inline query handler failed")
        await update.inline_query.answer([], cache_time=1)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "telegram-gif-search-bot"})


async def telegram_webhook(request: web.Request) -> web.Response:
    application: Application = request.app["telegram_application"]
    try:
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
        return web.Response(text="ok")
    except Exception:
        log.exception("Webhook update processing failed")
        return web.Response(status=500, text="error")


async def run() -> None:
    if not WEBHOOK_BASE.startswith("https://"):
        raise RuntimeError(
            "Set WEBHOOK_URL to your deployed Render URL, e.g. https://your-service.onrender.com"
        )

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CallbackQueryHandler(private_result_callback, pattern=r"^(gif|page):"))
    application.add_handler(InlineQueryHandler(inline_query_handler))

    secret_path = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
    webhook_path = f"/telegram/{secret_path}"
    webhook_url = f"{WEBHOOK_BASE}{webhook_path}"

    await application.initialize()
    await application.start()

    web_app = web.Application()
    web_app["telegram_application"] = application
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    web_app.router.add_post(webhook_path, telegram_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await application.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    log.info("GIF search bot is running. Webhook: %s", webhook_url)

    try:
        await asyncio.Event().wait()
    finally:
        await application.bot.delete_webhook()
        await runner.cleanup()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
