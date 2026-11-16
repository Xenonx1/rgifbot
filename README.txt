REDGIFS TELEGRAM BOT — SETUP GUIDE
==================================

WHAT THIS BOT DOES
- /search keyword: searches RedGIFs and shows selectable results.
- Selecting a result downloads and sends the MP4 into the chat.
- Sending a RedGIFs /watch/ or /ifr/ URL downloads and sends that video.
- Supports private chats and groups.
- Includes a simple 18+ self-confirmation prompt.

IMPORTANT LIMITATIONS
- RedGIFs does not provide a stable public API. The Python wrapper used here
  relies on RedGIFs' current website API, which can change without notice.
- The bot only handles publicly accessible content. It does not bypass logins,
  paywalls, access controls, age restrictions, or geographic restrictions.
- Telegram's cloud Bot API has upload limits. This project caps uploads at
  approximately 49 MB and tries SD quality if HD is too large.
- Render's free web service can sleep after 15 minutes without inbound traffic.
  The first Telegram request after sleeping may be delayed while it wakes.
- The bot does not store a permanent video library. Temporary downloads are
  deleted after sending. Search state and age confirmation are in memory and
  reset when the service restarts.
- Use only if you are legally permitted to access the content; 18+ only.
  Respect creators' rights and RedGIFs' terms.

PART 1 — CREATE YOUR TELEGRAM BOT
1. Open Telegram and search for @BotFather.
2. Send /newbot.
3. Choose a display name, then a username ending in "bot".
4. BotFather gives you a token. Copy it privately.
   NEVER post your token in a group or send it to anyone.
5. In BotFather, send /setjoingroups and enable groups if you want group use.
6. If you want the bot to detect ordinary RedGIFs links in groups (not only
   commands), use /setprivacy, select your bot, then choose Disable.
   After changing privacy, remove and re-add the bot to the group.

PART 2 — PUT THE PROJECT ON GITHUB
1. Download and extract redgifs_telegram_bot.zip.
2. Sign in at https://github.com and create a new repository.
3. Name it redgifs-telegram-bot. Keep it Private if preferred.
4. Choose "uploading an existing file" or "Add file" > "Upload files".
5. Upload bot.py, requirements.txt, render.yaml, and .gitignore.
   README.txt is optional.
6. Commit the files.

PART 3 — DEPLOY ON RENDER (FREE)
1. Sign in at https://render.com using your GitHub account.
2. Click New + > Blueprint.
3. Connect the redgifs-telegram-bot repository.
4. Render reads render.yaml. Review the service and click Apply.
5. In the service's Environment page, add:
   BOT_TOKEN = the token from BotFather
   The Blueprint generates WEBHOOK_SECRET and sets WEBHOOK_PATH for you.
   If WEBHOOK_SECRET is not present, create a random value containing only
   letters, numbers, underscores or hyphens (at least 32 characters).
   Do not change RENDER_EXTERNAL_URL; Render supplies it automatically.
6. Save changes and wait for the deploy to finish.
7. Open the service URL shown by Render. A browser page may show "Not Found";
   that is normal because this app is a Telegram webhook, not a website.
8. Open Render Logs. Look for "Starting webhook server".
9. Open your bot in Telegram, press Start, confirm 18+, then test:
   /search nature
   and a public RedGIFs URL.

IF YOU PREFER MANUAL RENDER SETUP
- New + > Web Service > connect the GitHub repository.
- Runtime: Python
- Build command: pip install -r requirements.txt
- Start command: python bot.py
- Instance type: Free
- Add environment variables BOT_TOKEN and WEBHOOK_SECRET.
- Add WEBHOOK_PATH = redgifs-hook.
- Deploy. Render automatically provides RENDER_EXTERNAL_URL.

PART 4 — GROUP SETUP
1. Add your bot to the group.
2. Make sure it has permission to send messages and videos.
3. Use /start in a private chat and confirm 18+.
4. Use /search keyword in the group.
5. For automatic link detection in group messages, disable privacy through
   @BotFather as explained in Part 1. Otherwise Telegram may only deliver
   commands and messages that explicitly mention/reply to the bot.

TROUBLESHOOTING
- "Missing BOT_TOKEN": add BOT_TOKEN in Render > Environment.
- "WEBHOOK_SECRET is missing": add WEBHOOK_SECRET and redeploy.
- "RENDER_EXTERNAL_URL is missing": deploy as a Render Web Service, not a
  background worker or static site.
- Search fails: RedGIFs may have changed its undocumented website API or may
  be temporarily unavailable. Check Render Logs and try again later.
- Video too large: the bot tries SD quality. If SD is still over the limit,
  it sends an error rather than crashing.
- Bot is slow on first use: Render free services sleep when idle; cold start
  delay is expected.
- Bot ignores normal group links: disable Group Privacy in @BotFather and
  re-add the bot to the group.

SECURITY
- Never commit your Telegram token to GitHub.
- Do not share the Render Environment page or bot token.
- If the token is exposed, use @BotFather /revoke and update BOT_TOKEN in Render.
