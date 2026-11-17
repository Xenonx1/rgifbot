import asyncio
import logging
import uuid
import httpx
from telegram import Update, InlineQueryResultGif
from telegram.ext import Application, InlineQueryHandler, ContextTypes
from duckduckgo_search import DDGS

# Configuration - Replace with your actual credentials
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
GOOGLE_API_KEY = "YOUR_GOOGLE_API_KEY"
GOOGLE_CSE_ID = "YOUR_GOOGLE_CSE_ID"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def search_ddg_gifs(query: str, max_results: int = 25) -> list[dict]:
    """Fetches GIFs from DuckDuckGo with SafeSearch turned off."""
    results = []
    try:
        with DDGS() as ddgs:
            ddg_gen = ddgs.images(
                keywords=query,
                region="wt-wt",
                safesearch="off",
                type_image="gif",
                max_results=max_results,
            )
            for item in ddg_gen:
                gif_url = item.get("image")
                thumb_url = item.get("thumbnail") or gif_url
                if gif_url and gif_url.startswith("http"):
                    results.append({
                        "gif_url": gif_url,
                        "thumb_url": thumb_url,
                        "title": item.get("title", "GIF"),
                        "width": int(item.get("width", 0)) or None,
                        "height": int(item.get("height", 0)) or None,
                    })
    except Exception as e:
        logger.error(f"DuckDuckGo search error: {e}")
    return results


async def search_google_gifs(query: str, max_results: int = 10) -> list[dict]:
    """Fetches GIFs from Google Custom Search JSON API with SafeSearch turned off."""
    if not GOOGLE_API_KEY or GOOGLE_API_KEY == "YOUR_GOOGLE_API_KEY":
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "searchType": "image",
        "fileType": "gif",
        "safe": "off",
        "num": min(max_results, 10),  # Google API max is 10 per request
    }

    results = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("items", []):
                    gif_url = item.get("link")
                    image_info = item.get("image", {})
                    thumb_url = image_info.get("thumbnailLink") or gif_url
                    if gif_url and gif_url.startswith("http"):
                        results.append({
                            "gif_url": gif_url,
                            "thumb_url": thumb_url,
                            "title": item.get("title", "GIF"),
                            "width": int(image_info.get("width", 0)) or None,
                            "height": int(image_info.get("height", 0)) or None,
                        })
            else:
                logger.warning(f"Google API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Google search error: {e}")
    return results


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming inline queries and returns a grid of clickable GIFs."""
    query = update.inline_query.query.strip()
    if not query:
        return

    # Run DuckDuckGo (in thread) and Google Search concurrently
    ddg_task = asyncio.to_thread(search_ddg_gifs, query, 25)
    google_task = search_google_gifs(query, 10)

    ddg_results, google_results = await asyncio.gather(ddg_task, google_task)

    # Deduplicate URLs while combining results (up to Telegram's 50-item limit)
    seen_urls = set()
    inline_results = []

    for item in google_results + ddg_results:
        gif_url = item["gif_url"]
        if gif_url in seen_urls:
            continue
        seen_urls.add(gif_url)

        kwargs = {
            "id": str(uuid.uuid4()),
            "gif_url": gif_url,
            "thumbnail_url": item["thumb_url"],
            "title": item["title"][:60],
        }
        if item["width"]:
            kwargs["gif_width"] = item["width"]
        if item["height"]:
            kwargs["gif_height"] = item["height"]

        inline_results.append(InlineQueryResultGif(**kwargs))

        if len(inline_results) >= 50:
            break

    try:
        # cache_time=60 caches results briefly on Telegram servers for faster loading
        await update.inline_query.answer(inline_results, cache_time=60, is_personal=True)
    except Exception as e:
        logger.error(f"Failed to answer inline query: {e}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(InlineQueryHandler(inline_query_handler))
    print("Bot is running... Type @YourBotUsername <search term> in any Telegram chat.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
