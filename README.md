# Telegram Media Bot – Render Deploy

## Files
media_bot.py · requirements.txt · Dockerfile · render.yaml · .env.example

## Get keys
- API_ID / API_HASH: https://my.telegram.org -> API development tools
- BOT_TOKEN: @BotFather -> /newbot

## Deploy on Render
1. Push all files to a GitHub repo.
2. Render Dashboard -> New -> Blueprint (uses render.yaml)
   (or New -> Web Service -> Docker, and add the env vars manually).
3. Fill in the 3 environment variables when asked.
4. Deploy. Logs should show "Bot started".
5. Optional: ping your Render URL every 5 min with UptimeRobot if using the free plan so it doesn't sleep.

## Notes
- Render disk is temporary: saved thumbnails are lost on redeploy/restart.
  Add a persistent Disk mounted at /app/thumbs (paid plans) to keep them.
- Video conversion is CPU heavy; use at least the Starter plan for 480p/720p/portrait jobs.
- Free plan has only 512 MB RAM / limited disk, so large files (>500 MB) may fail.

## Speed tuning (optional env vars)
- X264_PRESET: ultrafast (default, fastest) | superfast | veryfast | faster
- X264_CRF: 24 (better quality) ... 28 (faster, smaller). Default 26.
- PORTRAIT_RES: 720x1280 (default, fast) or 1080x1920.

## Cover / poster on big videos
- Cover is added by re-uploading the same file (no re-encode, no quality loss), so any size Telegram allows works (bots: up to 2 GB per file).
- Poster is auto-converted to a valid Telegram thumbnail (JPEG, max 320x320, under 200 KB). A poster sent as a *file* also works.
- Retries on network errors, waits out Telegram flood limits, checks free disk, and queues big jobs.
- COVER_JOBS (default 2): how many cover jobs run at once. Use 1 on small disks.

## Speed
- Uploads use several connections at once (`fast_upload.py`): UPLOAD_CONNS (default 8) x UPLOAD_WORKERS (default 2). If it ever fails it falls back to the normal uploader by itself; set FAST_UPLOAD=0 to turn it off.
- Look for `[fast_upload] on` in the logs. If you see `[fast_upload] failed`, lower UPLOAD_CONNS (e.g. 4).
- uvloop is used for a faster event loop. TRANSMISSIONS only sets how many files transfer at the same time.
- Re-upload happens without re-encoding. Real speed is still limited by Telegram and the server's network.

## Files up to ~4 GB (needs a Telegram Premium account)
A bot itself can only do 2 GB. For bigger files a Premium *user* account does the work:
1. On your PC: `pip install pyrogram tgcrypto`, run `python gen_session.py`, log in, copy the printed text.
2. Render -> Environment -> add `SESSION_STRING` = that text. Redeploy.
3. Send your cover photo to the bot once (it is reused).
4. Forward the big video to your **Saved Messages** -> cover is added automatically.
   (Or upload it to Saved Messages and reply to it with `/cover`.)
Note: it uses your own account, so keep the session string secret.

## Why is converting (360/480/720p) slow?
Converting really re-encodes the video, so it needs CPU. Speed (`0.2x` = 5 hours of video takes 25 hours) depends only on the server's CPU, not on Telegram.
- Startup log shows `[cpu] usable CPU cores: ...` (the real quota of your plan). Around 0.5 cores = slow; 2-4+ cores = much faster.
- Convert ONE quality instead of "All 3" (less work). Every job has a 🛑 Cancel button.
- Cover, rename, To MP4 and To MKV do not re-encode, so they are fast on any plan.

## Cover on ANY video size (instant)
For normal videos the bot uses Telegram's *video cover* feature: it resends the SAME video (by `file_id`) with your poster as cover.
Nothing is downloaded or uploaded, so a 5 GB video takes about a second and there is no 2 GB / 4 GB limit.
- Send your poster photo to the bot again once, so it keeps an HD copy (older saves only have a small 320px copy).
- Needs a reasonably recent Telegram app to display the cover.
- Videos sent as a *file/document* (not as video) can't use this; they still use the re-upload method (2 GB limit, 4 GB with the Premium helper).
- If it fails, the bot shows the exact Telegram error text.

## Rename big files
Rename = download + upload again, so a bot can only rename files up to 2 GB. For 2-4 GB: put the file in your **Saved Messages** and reply to it with `/rename New Name.mkv` (needs the Premium helper). Any failure is now shown in the chat instead of freezing on "Downloading 100%".

## Direct links
Send a **direct file link** (a normal `https://.../file.mkv` URL that starts the download by itself) and the bot downloads it, adds your cover and sends it back.
- Fast: several connections at once (`LINK_CONNS`, default 6), resumes by itself after a network drop, has a 🛑 Cancel button.
- Safe: private/local addresses are refused (also on redirects). Set `LINK_ALLOWED_USERS=123,456` to let only those Telegram ids use links.
- Limit: 2000 MB for the bot. 2-4 GB: paste the link in your **Saved Messages** (Premium helper).
- Web pages, share pages (TeraBox and similar) are not direct files and are refused with a message.
