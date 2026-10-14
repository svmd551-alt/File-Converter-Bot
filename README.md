# Telegram Media Bot – Render Deploy

## Files
media_bot.py · requirements.txt · Dockerfile · render.yaml · .env.example

## Get keys
- API_ID / API_HASH: https://my.telegram.org -> API development tools
- BOT_TOKEN: @BotFather -> /newbot
- TMDB_KEY: https://www.themoviedb.org/settings/api (free)
- OMDB_KEY: https://www.omdbapi.com/apikey.aspx (free, for IMDb rating)

## Deploy on Render
1. Push all files to a GitHub repo.
2. Render Dashboard -> New -> Blueprint (uses render.yaml)
   (or New -> Web Service -> Docker, and add the env vars manually).
3. Fill in the 5 environment variables when asked.
4. Deploy. Logs should show "Bot started".
5. Optional: ping your Render URL every 5 min with UptimeRobot if using the free plan so it doesn't sleep.

## Notes
- Render disk is temporary: saved thumbnails are lost on redeploy/restart.
  Add a persistent Disk mounted at /app/thumbs (paid plans) to keep them.
- Video conversion is CPU heavy; use at least the Starter plan for 480p/720p/portrait jobs.
- Free plan has only 512 MB RAM / limited disk, so large files (>500 MB) may fail.

## Speed tuning (optional env vars)
- X264_PRESET: ultrafast (fastest) | superfast (default) | veryfast | faster
- X264_CRF: 24 (better quality) ... 28 (faster, smaller). Default 26.
- PORTRAIT_RES: 720x1280 (default, fast) or 1080x1920.
