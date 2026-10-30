# VOX Avengers: Doomsday Monitor

Vercel Python endpoint that checks the two official VOX Egypt movie pages and sends Telegram alerts when clickable screening-time links appear.

## Important
- Vercel Hobby Cron is limited to once per day. This project does not configure a Vercel Cron.
- For frequent checks, use cron-job.org to call the deployed endpoint every 5 minutes.
- The checker detects links whose visible text is a screening time. VOX can change its page structure, so verify any alert by opening the official booking link.
- It does not reserve seats or make purchases.

## Setup
1. In Telegram, message `@BotFather`, run `/newbot`, and copy the token.
2. Open your new bot and press Start. Send it a message.
3. Get your chat ID from `https://api.telegram.org/botYOUR_TOKEN/getUpdates`.
4. Create an Upstash Redis database and copy its REST URL and REST token.
5. Push this folder to a private GitHub repository and import it into Vercel.
6. Add these Vercel Environment Variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `MONITOR_SECRET` (use a long random secret).
7. Deploy to Production.

## Schedule
In cron-job.org create an HTTP GET job every 5 minutes:
- URL: `https://YOUR-PROJECT.vercel.app/api/check`
- Header: `x-monitor-secret: YOUR_MONITOR_SECRET`

## Manual test
Call `https://YOUR-PROJECT.vercel.app/api/check` with header `x-monitor-secret` set to the same secret.

## Duplicate state
The Redis key is `vox:doomsday:last_fingerprint`. Delete it to allow the current set of links to trigger a fresh alert.

Keep all tokens and secrets private. Never commit them to GitHub.
