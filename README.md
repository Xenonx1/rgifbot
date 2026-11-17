# Telegram GIF Search Bot

Supports:
- Inline search: `@YourBotUsername funny cats` in any Telegram chat.
- Private chat search: `/search funny cats`.
- DuckDuckGo image search for GIFs with SafeSearch set to off.
- Google Custom Search image results with `fileType=gif` and `safe=off` (optional; requires API credentials).
- Clickable private-chat result buttons and page navigation.

## Files
- `main.py` — bot application
- `requirements.txt` — Python dependencies
- `render.yaml` — Render Blueprint configuration

## 1. Create the Telegram bot
1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and follow the prompts.
3. Copy the bot token. Keep it private.
4. Open `@BotFather`, run `/setinline`, select your bot, and enable inline mode. Set a placeholder such as `Search GIFs` when asked for the inline placeholder.

## 2. Deploy on Render
1. Create a GitHub repository and upload these three files to its root.
2. Sign in to Render and choose **New + → Blueprint**.
3. Connect the GitHub repository and deploy the Blueprint.
4. Render will ask for `BOT_TOKEN`, `GOOGLE_API_KEY`, and `GOOGLE_CSE_ID`.
5. Enter the bot token. Render automatically supplies the service's public URL to the app.
6. Google credentials are optional. Leave the two Google values blank to use DuckDuckGo only.
7. Save the environment variables and deploy.

If creating the service manually instead of using the Blueprint, use:
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Health check path: `/health`
- Environment variables: `BOT_TOKEN`; optionally `GOOGLE_API_KEY`, `GOOGLE_CSE_ID`. The app reads Render's automatically supplied `RENDER_EXTERNAL_URL` for its webhook URL. If deploying elsewhere, set `WEBHOOK_URL` to the public HTTPS base URL.

## 3. Google image search (optional)
Google Custom Search requires a Google API key and a Programmable Search Engine ID (CSE ID). Add them as `GOOGLE_API_KEY` and `GOOGLE_CSE_ID`. Google API availability, pricing, quotas, and SafeSearch behavior are controlled by Google and may change. `safe=off` is requested by this code, but the provider may still apply its own restrictions.

## 4. Test
- Open the bot and send `/start`.
- Send `/search funny cats`.
- In any chat, type `@YourBotUsername funny cats` and choose a result.
- If inline results do not appear, check that `/setinline` was completed with BotFather.

## Important limitations
- Search engines do not guarantee that every returned URL is a directly playable animated GIF. Some hosts block Telegram or return a preview/redirect; those individual results may fail to send.
- DuckDuckGo's public search interface is not a guaranteed production API and can change or rate-limit requests.
- Render's free web services can spin down after inactivity. The first request after a spin-down may be delayed. Free-tier availability and limits can change.
- The bot does not download or rehost GIFs; Telegram fetches the media from the source URL.
- Store credentials only in Render environment variables. Never commit your bot token or API keys to GitHub.
