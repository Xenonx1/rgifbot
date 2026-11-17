DUCKDUCKGO-ONLY TELEGRAM GIF BOT (v2)

WHAT CHANGED
- Switched from the deprecated "duckduckgo-search" package to its maintained successor "ddgs".
- DuckDuckGo backend is forced (backend="duckduckgo"), SafeSearch is "off", GIF filter is on.
- Every result is checked before it is shown: the file must start with GIF87a/GIF89a magic bytes.
  Static images, HTML pages and dead/blocked links are dropped.
- GIFs of 1 MB or less (Telegram's inline limit) are ranked first. Larger ones (up to MAX_GIF_MB, default 5) come after.
- If DuckDuckGo's GIF filter returns too few, a second pass searches "<query> gif".
- Thumbnail = the verified GIF itself (mime image/gif), so previews are no longer blank.
- /search: if Telegram can't fetch the URL, the bot downloads the GIF and uploads it directly.
- Results cached 10 min; duplicate queries share one search; webhook replies instantly.
- Removed delete_webhook on shutdown (it could break the bot during Render redeploys).
- Self-ping every 10 min keeps the Render free plan awake (set KEEP_ALIVE=0 to disable).
- New /debug <query> command shows raw results, real GIFs found, and why others failed.

DEPLOY
1. Replace the files in your GitHub repo with these (keep the hidden .python-version).
2. Commit and push.
3. Render: Manual Deploy > Clear build cache & deploy.
4. Env var needed: BOT_TOKEN. Optional: MAX_GIF_MB (1 = strict), DDG_REGION, KEEP_ALIVE, LOG_LEVEL.
5. Test: /search cats, then /debug cats, then @YourBot cats inline.
6. Inline mode must be on: @BotFather > /setinline.

NOTE
Some source sites block Telegram's servers. Inline mode can't work around that; the checks only
reduce it. If something still fails, send me the /debug output and the Render log.
