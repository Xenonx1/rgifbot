DUCKDUCKGO-ONLY TELEGRAM GIF BOT

Features
- DuckDuckGo Images only.
- Requests GIF image format (type_image="gif").
- SafeSearch parameter is set to "off".
- Uses the original image and thumbnail URLs directly in Telegram inline results.
- No Google API, no proxy, no rehosting, no GIF conversion.
- Inline search: @YourBotUsername cats
- Private search: /search cats

RENDER DEPLOYMENT
1. Create a GitHub repository and upload main.py, requirements.txt, render.yaml.
2. In Render choose New + > Blueprint and connect the repository.
3. Render asks for BOT_TOKEN. Enter the token from @BotFather.
4. Deploy. Render supplies RENDER_EXTERNAL_URL automatically.
5. Build command: pip install -r requirements.txt
6. Start command: python main.py
7. Health check path: /health

TELEGRAM INLINE MODE
1. Open @BotFather.
2. Send /setinline.
3. Select this bot.
4. Set a placeholder such as "Search GIFs".
5. In any chat type @YourBotUsername followed by search words.

ENVIRONMENT VARIABLES
Required:
BOT_TOKEN = token copied from @BotFather

Automatically supplied by Render:
RENDER_EXTERNAL_URL
PORT

Optional:
LOG_LEVEL = INFO (set DEBUG when diagnosing)

No Google API key, Google CSE ID, or WEBHOOK_URL is required.

IMPORTANT
The bot passes DuckDuckGo's original image URLs directly to Telegram. DuckDuckGo's GIF filter and SafeSearch-off setting are search parameters, not a guarantee that every result is a playable GIF or that every host permits Telegram to fetch it. If a source URL is blocked or is not a direct GIF file, Telegram may show a blank preview or fail to send it. This implementation intentionally does not proxy, download, transform, or replace the original media.


STRICT SEARCH SETTINGS
- Search backend is forced to DuckDuckGo only (not auto/Bing).
- SafeSearch is explicitly set to off.
- DuckDuckGo image type is explicitly set to gif.
- Each candidate URL is fetched and checked for a real multi-frame GIF before it is shown.
- This strict verification can reduce the number of results because some hosts block automated requests or do not serve the original GIF directly.

Render environment variables:
Required: BOT_TOKEN
Automatic on Render: RENDER_EXTERNAL_URL, PORT
Optional: LOG_LEVEL (INFO or DEBUG)
No Google API key or search engine ID is needed.

IMPORTANT PREVIEW NOTE:
This version does not download or reject source URLs before sending them to Telegram. It trusts DuckDuckGo's GIF image-type filter and passes the original image and thumbnail URLs directly to Telegram. Telegram can still show a blank preview when a source host blocks Telegram or the returned URL is not a directly playable GIF.
