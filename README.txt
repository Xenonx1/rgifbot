GIFREELS TELEGRAM BOT (18+)

- Searches gifreels.com (no DuckDuckGo anymore).
- Inline mode: @YourBot latina -> animated previews -> tap one -> it is sent to that chat.
- /search <words> in a private chat: paged list, tap to send.
- /debug <words>: shows which gifreels URL worked, how many GIFs were found/usable, and why others failed.

HOW SEARCH WORKS
Tries, in order: GIFREELS_SEARCH_URL (if you set it), https://gifreels.com/tag/<words-with-dashes>,
https://gifreels.com/search?q=<words>. GIFs are played from the site's MP4 files
(https://p5rn.com/cdn/storage/production/gifreels/post/<id>/gif.mp4).

RENDER
- Build: pip install -r requirements.txt   Start: python main.py   Health: /health
- Env: BOT_TOKEN (required). Optional: GIFREELS_SEARCH_URL, MAX_GIF_MB (default 5, use 1 for strict),
  KEEP_ALIVE=0, LOG_LEVEL.
- Deploy: replace repo files with ALL files here (main.py, gifreels.py, requirements.txt, render.yaml,
  .python-version), push, then Manual Deploy > Clear build cache & deploy.
- BotFather: /setinline must be enabled.

ENDLESS SCROLL (inline)
Each inline answer loads several site pages (?page=2, 3, ...) and tells Telegram where to continue
(next_offset), so more GIFs load as you scroll. If the site uses a different paging parameter, set
GIFREELS_PAGE_PARAM (default "page"). /debug <words> shows how many gif links each page returned.
