import asyncio
import hashlib
import logging
import os
from typing import Any

from ddgs import DDGS
from aiohttp import web
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
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
SEARCH_LIMIT = 40
PAGE_SIZE = 10

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ddg-gif-bot")


def is_http_url(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(("https://", "http://"))
        and len(value) < 4000
    )


def search_duckduckgo(query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """Search DuckDuckGo Images for GIF format, with SafeSearch disabled."""
    results = []
    seen = set()

    try:
        with DDGS(timeout=15) as ddgs:
            items = ddgs.images(
                query,
                region="wt-wt",
                safesearch="off",
                type_image="gif",
                max_results=limit,
            )

            for item in items:
                gif_url = item.get("image")
                thumbnail_url = item.get("thumbnail") or gif_url

                if not is_http_url(gif_url) or gif_url in seen:
                    continue

                seen.add(gif_url)
                results.append({
                    "gif_url": gif_url,
                    "thumbnail_url": thumbnail_url if is_http_url(thumbnail_url) else gif_url,
                    "title": (item.get("title") or "GIF")[:100],
                })

                if len(results) >= limit:
                    break

    except Exception:
        log.exception("DuckDuckGo image search failed")

    return results


async def get_results(query: str) -> list[dict]:
    return await asyncio.to_thread(search_duckduckgo, query, SEARCH_LIMIT)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "DuckDuckGo GIF Search Bot\n\n"
        "Private search: /search cats\n"
        "Inline search: type @YourBotUsername cats in any Telegram chat.\n\n"
        "Only DuckDuckGo is used. Image search requests GIF format and SafeSearch off."
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /search cats")
        return

    status = await update.effective_message.reply_text("Searching DuckDuckGo for GIFs…")
    results = await get_results(query)
    context.user_data["ddg_results"] = {"query": query, "items": results}

    if not results:
        await status.edit_text(
            "DuckDuckGo returned no GIF results for that search. Try different keywords."
        )
        return

    await show_page(status, context, 0)


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
            InlineKeyboardButton(
                f"{index + 1}. {title[:35]}",
                callback_data=f"send:{index}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    await message.edit_text(
        f"DuckDuckGo GIF results for: {query}\n"
        f"Showing {first + 1}–{first + len(current)} of {len(items)}.\n"
        "Tap a result to send it.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    await callback.answer()
    data = callback.data or ""

    if data.startswith("page:"):
        try:
            await show_page(callback.message, context, int(data.split(":", 1)[1]))
        except (ValueError, AttributeError):
            await callback.answer("Page unavailable. Search again.", show_alert=True)
        return

    if data.startswith("send:"):
        try:
            index = int(data.split(":", 1)[1])
            item = context.user_data["ddg_results"]["items"][index]
        except (ValueError, IndexError, KeyError):
            await callback.answer("Result unavailable. Search again.", show_alert=True)
            return

        try:
            # Pass the original DuckDuckGo image URL to Telegram unchanged.
            await context.bot.send_animation(
                chat_id=callback.message.chat_id,
                animation=item["gif_url"],
                caption=item.get("title", "GIF")[:900],
            )
        except Exception:
            log.exception("Telegram could not send selected GIF URL")
            await callback.answer(
                "Telegram could not load this source URL. Try another result.",
                show_alert=True,
            )


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.inline_query.query or "").strip()

    if not query:
        await update.inline_query.answer([], cache_time=2)
        return

    items = await get_results(query)
    results = []

    for item in items[:50]:
        # Use the search result image URL and thumbnail URL directly.
        # No proxy, conversion, rehosting, or extra filtering is applied.
        result_id = hashlib.sha256(item["gif_url"].encode()).hexdigest()[:32]
        results.append(
            InlineQueryResultGif(
                id=result_id,
                gif_url=item["gif_url"],
                thumbnail_url=item["thumbnail_url"],
                title=item["title"][:60],
            )
        )

    await update.inline_query.answer(
        results,
        cache_time=10,
        is_personal=True,
    )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": "duckduckgo-gif-search"})


async def telegram_webhook(request: web.Request) -> web.Response:
    app: Application = request.app["telegram_app"]
    try:
        payload = await request.json()
        update = Update.de_json(payload, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")
    except Exception:
        log.exception("Could not process Telegram webhook update")
        return web.Response(status=500, text="error")


async def main() -> None:
    if not RENDER_URL.startswith("https://"):
        raise RuntimeError(
            "Render did not provide RENDER_EXTERNAL_URL. Deploy this as a Render Web Service."
        )

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", start))
    telegram_app.add_handler(CommandHandler("search", search_command))
    telegram_app.add_handler(
        CallbackQueryHandler(result_callback, pattern=r"^(send|page):")
    )
    telegram_app.add_handler(InlineQueryHandler(inline_search))

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
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await telegram_app.bot.set_webhook(
        url=f"{RENDER_URL}{webhook_path}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    log.info("DuckDuckGo GIF bot started; webhook configured.")

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.bot.delete_webhook()
        await runner.cleanup()
        await telegram_app.stop()
        await telegram_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
