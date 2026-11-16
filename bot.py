import asyncio
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from redgifs.aio import API as RedGifsAPI
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultMpeg4Gif,
    InputFile,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("redgifs-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "redgifs-hook").strip("/")
PORT = int(os.getenv("PORT", "10000"))

MAX_UPLOAD_BYTES = 49 * 1024 * 1024
RESULTS_PER_PAGE = 8
INLINE_RESULTS_PER_PAGE = 20

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not WEBHOOK_SECRET:
    raise RuntimeError("Missing WEBHOOK_SECRET environment variable.")

redgifs = None
redgifs_lock = None
redgifs_logged_in = False

REDGIFS_LINK_RE = re.compile(
    r"https?://(?:www\.)?redgifs\.com/(?:watch|ifr)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


async def get_redgifs():
    global redgifs, redgifs_lock, redgifs_logged_in

    if redgifs_lock is None:
        redgifs_lock = asyncio.Lock()

    async with redgifs_lock:
        if redgifs is None:
            redgifs = RedGifsAPI()

        if not redgifs_logged_in:
            await redgifs.login()
            redgifs_logged_in = True

    return redgifs


def age_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("I am 18 or older", callback_data="age:yes")]]
    )


async def require_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("age_ok"):
        return True

    message = update.effective_message
    if message:
        await message.reply_text(
            "This bot may return mature RedGIFs content. You must be 18 or older "
            "and comply with the laws and RedGIFs terms that apply to you. "
            "Confirm below to continue.",
            reply_markup=age_keyboard(),
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Welcome to the RedGIFs bot.\n\n"
        "• Search: /search cats\n"
        "• Inline search: @YourBotUsername cats\n"
        "• Download: send a RedGIFs watch/ifr link\n"
        "• Help: /help\n\n"
        "This bot only handles publicly accessible RedGIFs media. "
        "It does not bypass access controls or age restrictions. "
        "18+ only.",
        reply_markup=age_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_age(update, context):
        return

    await update.effective_message.reply_text(
        "Commands:\n"
        "/search keyword — search RedGIFs\n"
        "Inline: @YourBotUsername keyword\n"
        "/help — show this help\n\n"
        "To download, send a link such as:\n"
        "https://www.redgifs.com/watch/VIDEOID\n\n"
        "Use /search in a group. To let the bot detect ordinary links in groups, "
        "disable Group Privacy for the bot in @BotFather and re-add the bot."
    )


def result_keyboard(ids, page, has_previous, has_next):
    rows = []
    for i, gif_id in enumerate(ids):
        rows.append(
            [
                InlineKeyboardButton(
                    f"▶ Result {i + 1} · {gif_id[:22]}",
                    callback_data=f"pick:{i}",
                )
            ]
        )

    nav = []
    if has_previous:
        nav.append(
            InlineKeyboardButton("◀ Previous", callback_data=f"page:{page-1}")
        )
    if has_next:
        nav.append(
            InlineKeyboardButton("Next ▶", callback_data=f"page:{page+1}")
        )
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


async def show_search_page(
    query: str,
    page: int,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit_message=None,
):
    api = await get_redgifs()
    result = await api.search(query, count=RESULTS_PER_PAGE, page=page)
    gifs = result.gifs or []
    ids = [gif.id for gif in gifs if getattr(gif, "id", None)]

    context.user_data["search_query"] = query
    context.user_data["search_page"] = page
    context.user_data["search_ids"] = ids

    if not ids:
        body = f"No video results found for: {query}"
        if edit_message:
            await edit_message.edit_text(body)
        else:
            await update.effective_message.reply_text(body)
        return

    body = (
        f"RedGIFs search: {query}\n"
        f"Page {page} · showing {len(ids)} results\n\n"
        "Choose a result below to send the video into this chat."
    )
    keyboard = result_keyboard(
        ids,
        page,
        page > 1,
        page < (getattr(result, "pages", page) or page),
    )

    if edit_message:
        await edit_message.edit_text(body, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(body, reply_markup=keyboard)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_age(update, context):
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /search your keywords")
        return
    if len(query) > 100:
        await update.effective_message.reply_text(
            "Please keep the search under 100 characters."
        )
        return

    status = await update.effective_message.reply_text("Searching RedGIFs…")
    try:
        await show_search_page(query, 1, update, context, edit_message=status)
    except Exception:
        log.exception("Search failed")
        await status.edit_text(
            "Search failed. RedGIFs may be temporarily unavailable; try again later."
        )


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return silent MP4 animation previews in Telegram's inline GIF-style grid."""
    inline_query = update.inline_query
    if inline_query is None:
        return

    # Require users to open the bot and confirm their age before inline searching.
    if not context.user_data.get("age_ok"):
        gate_result = InlineQueryResultArticle(
            id="age-confirmation-required",
            title="Open the bot and confirm you are 18+",
            input_message_content=InputTextMessageContent(
                "Please open the RedGIFs bot, send /start, and confirm that you "
                "are 18 or older before using inline search."
            ),
        )
        await inline_query.answer(
            [gate_result], cache_time=0, is_personal=True
        )
        return

    search_text = (inline_query.query or "").strip()
    if not search_text:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    if len(search_text) > 100:
        search_text = search_text[:100]

    page = 1
    if inline_query.offset and inline_query.offset.isdigit():
        page = max(1, int(inline_query.offset))

    try:
        api = await get_redgifs()
        result = await api.search(
            search_text,
            count=INLINE_RESULTS_PER_PAGE,
            page=page,
        )

        inline_results = []
        for gif in (result.gifs or []):
            gif_id = getattr(gif, "id", None)
            urls = getattr(gif, "urls", None)
            if not gif_id or urls is None:
                continue

            # RedGIFs supplies MP4 media URLs and still-image thumbnail URLs.
            media_url = getattr(urls, "hd", None) or getattr(urls, "sd", None)
            thumbnail_url = (
                getattr(urls, "thumbnail", None)
                or getattr(urls, "poster", None)
            )

            if not media_url or not thumbnail_url:
                continue
            if not media_url.startswith("https://") or not thumbnail_url.startswith(
                "https://"
            ):
                continue

            result_id = hashlib.sha1(str(gif_id).encode("utf-8")).hexdigest()[:32]

            inline_results.append(
                InlineQueryResultMpeg4Gif(
                    id=result_id,
                    mpeg4_url=media_url,
                    thumbnail_url=thumbnail_url,
                )
            )

        total_pages = getattr(result, "pages", page) or page
        next_offset = str(page + 1) if page < total_pages else ""

        await inline_query.answer(
            inline_results,
            cache_time=10,
            is_personal=True,
            next_offset=next_offset,
        )

    except Exception:
        log.exception("Inline search failed")
        # Always answer the inline query so Telegram does not keep waiting.
        try:
            await inline_query.answer([], cache_time=1, is_personal=True)
        except Exception:
            log.exception("Could not send empty inline-search response")


async def download_gif(gif, api):
    """Download HD first, then SD if HD fails or exceeds Telegram's upload limit."""
    urls = gif.urls
    candidates = [getattr(urls, "hd", None), getattr(urls, "sd", None)]
    candidates = [url for url in candidates if url]

    if not candidates:
        raise RuntimeError("RedGIFs did not provide a downloadable video URL.")

    last_error = None
    for media_url in candidates:
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix="redgifs_", suffix=".mp4")
            os.close(fd)
            await api.download(media_url, path)

            size = Path(path).stat().st_size
            if size <= 0:
                raise RuntimeError("Downloaded file was empty.")

            if size > MAX_UPLOAD_BYTES:
                Path(path).unlink(missing_ok=True)
                last_error = RuntimeError(
                    "Video is larger than Telegram's upload limit."
                )
                continue

            return path

        except Exception as exc:
            last_error = exc
            if path:
                Path(path).unlink(missing_ok=True)

    raise RuntimeError(str(last_error or "Could not download this video."))


async def send_gif(chat, gif, api, reply_to_message_id=None):
    path = await download_gif(gif, api)
    try:
        caption = f"RedGIFs · @{gif.username}\n{gif.urls.web_url}"
        with open(path, "rb") as media:
            await chat.send_video(
                video=InputFile(media, filename=f"{gif.id}.mp4"),
                caption=caption[:1024],
                supports_streaming=True,
                reply_to_message_id=reply_to_message_id,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )
    finally:
        Path(path).unlink(missing_ok=True)


async def handle_redgifs_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_age(update, context):
        return

    message_text = update.effective_message.text or ""
    match = REDGIFS_LINK_RE.search(message_text)
    if not match:
        return

    gif_id = match.group(1)
    status = await update.effective_message.reply_text("Fetching RedGIFs video…")

    try:
        api = await get_redgifs()
        gif = await api.get_gif(gif_id)
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )
        await send_gif(
            update.effective_chat,
            gif,
            api,
            update.effective_message.message_id,
        )
        await status.delete()

    except Exception:
        log.exception("Link download failed")
        await status.edit_text(
            "I couldn't download that video. Check that the link is public and valid, "
            "or the file may exceed Telegram's upload limit."
        )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "age:yes":
        context.user_data["age_ok"] = True
        await query.edit_message_text(
            "Age confirmation saved for this bot session. Use /search keywords "
            "or type @YourBotUsername followed by keywords in any chat."
        )
        return

    if not context.user_data.get("age_ok"):
        await query.answer(
            "Please confirm that you are 18 or older first.",
            show_alert=True,
        )
        return

    if data.startswith("page:"):
        try:
            page = int(data.split(":", 1)[1])
            search_query = context.user_data.get("search_query")
            if not search_query:
                await query.edit_message_text(
                    "Please start a new search with /search keywords."
                )
                return

            await show_search_page(
                search_query,
                page,
                update,
                context,
                edit_message=query.message,
            )

        except Exception:
            log.exception("Pagination failed")
            await query.edit_message_text(
                "Couldn't load that page. Please try /search again."
            )
        return

    if data.startswith("pick:"):
        try:
            index = int(data.split(":", 1)[1])
            ids = context.user_data.get("search_ids", [])

            if index < 0 or index >= len(ids):
                await query.answer(
                    "That result has expired. Search again.",
                    show_alert=True,
                )
                return

            status = await query.message.reply_text("Preparing selected video…")
            api = await get_redgifs()
            gif = await api.get_gif(ids[index])

            await context.bot.send_chat_action(
                chat_id=query.message.chat_id,
                action=ChatAction.UPLOAD_VIDEO,
            )
            await send_gif(
                query.message.chat,
                gif,
                api,
                query.message.message_id,
            )
            await status.delete()

        except Exception:
            log.exception("Selected result download failed")
            await query.message.reply_text(
                "Couldn't send that video. Try another result."
            )
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram error: %s", context.error)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(InlineQueryHandler(inline_search))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_redgifs_link)
    )
    app.add_error_handler(error_handler)

    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not external_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing. Deploy this project as a Render Web Service."
        )

    webhook_url = f"{external_url}/{WEBHOOK_PATH}"
    log.info("Starting webhook server on port %s", PORT)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
