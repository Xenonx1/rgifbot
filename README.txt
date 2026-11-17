DUCKDUCKGO-ONLY TELEGRAM GIF BOT — RENDER COMPATIBILITY FIX

This revision changes the search dependency stack to avoid the malformed-headers failure seen in the Render logs.

SEARCH SETTINGS
- DuckDuckGo Images only.
- safesearch="off".
- type_image="gif".
- Original image and thumbnail URLs are passed directly to Telegram.
- No Google API, proxy, rehosting, conversion, or pre-download verification.

DEPENDENCIES / RUNTIME
- Python 3.12.10 (selected by .python-version)
- duckduckgo-search 8.1.1
- primp 1.3.1 (pinned to avoid an unbounded newer transport dependency)

RENDER
- Build command: pip install -r requirements.txt
- Start command: python main.py
- Health check: /health
- Required environment variable: BOT_TOKEN
- Render automatically supplies PORT and RENDER_EXTERNAL_URL.
- No Google API key, Google CSE ID, or WEBHOOK_URL.

DEPLOY
1. Replace the files in the GitHub repository with all files in this ZIP, including the hidden .python-version file.
2. Commit and push the changes.
3. In Render, use Manual Deploy > Clear build cache & deploy (important so old dependencies are not reused).
4. Confirm the build log installs duckduckgo-search==8.1.1 and primp==1.3.1.
5. Keep the start command as python main.py.
6. Test /search cats in a private chat first, then test @YourBotUsername cats inline.

TELEGRAM INLINE MODE
If inline mode has not already been enabled: open @BotFather, send /setinline, select the bot, and set a placeholder such as Search GIFs.

NOTE
The library asks DuckDuckGo for GIF image results with SafeSearch off. Search engines and source hosts can still return inaccurate file labels or block Telegram's media fetch, so a blank preview can still occur for an individual source URL. This fix addresses the DuckDuckGo request error in the supplied Render log; it cannot guarantee every source host is Telegram-compatible.
