"""
Telegram Media Bot (Pyrogram + FFmpeg)
--------------------------------------
Features
  /poster <movie name>   -> TMDB + IMDb (OMDb) poster/cover/backdrop
  Send a video/file      -> menu: Rename | MKV<->MP4 | 360/480/720p | Portrait 9:16 | Set thumbnail
  Send a photo           -> saved as your custom thumbnail (used on every upload)
  /thumb  /delthumb      -> view / delete saved thumbnail
  /rename <new name>     -> reply to a file with this to rename instantly

Install:
  pip install -U pyrogram tgcrypto aiohttp aiofiles
  apt install ffmpeg
Env vars:
  API_ID, API_HASH, BOT_TOKEN, TMDB_KEY, OMDB_KEY (free keys from themoviedb.org / omdbapi.com)
Run:
  python media_bot.py
"""
import os, asyncio, time, shutil, json
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                            CallbackQuery, Message)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
TMDB_KEY = os.environ.get("TMDB_KEY", "")
OMDB_KEY = os.environ.get("OMDB_KEY", "")

WORK = "work"
THUMBS = "thumbs"
os.makedirs(WORK, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)

app = Client("media_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             workers=16, max_concurrent_transmissions=4)

# one ffmpeg job at a time per user, max 2 jobs globally (keeps the bot fast)
JOB_SLOTS = asyncio.Semaphore(2)
PENDING_RENAME = {}   # user_id -> message_id of file awaiting new name

QUALITIES = {"360": 360, "480": 480, "720": 720}


# ───────────────────────── helpers ─────────────────────────
def thumb_path(uid):
    p = f"{THUMBS}/{uid}.jpg"
    return p if os.path.exists(p) else None


async def run(cmd):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, err.decode(errors="ignore")


async def probe(path):
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
        stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    d = json.loads(out or "{}")
    v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
    return int(float(d.get("format", {}).get("duration", 0))), v.get("width", 0), v.get("height", 0)


def progress_cb(msg, label):
    last = {"t": 0}

    async def cb(cur, total):
        if time.time() - last["t"] < 4:
            return
        last["t"] = time.time()
        try:
            await msg.edit_text(f"{label} {cur * 100 / total:.0f}%")
        except Exception:
            pass
    return cb


def menu(mid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"rn|{mid}")],
        [InlineKeyboardButton("🔁 To MP4", callback_data=f"mp4|{mid}"),
         InlineKeyboardButton("🔁 To MKV", callback_data=f"mkv|{mid}")],
        [InlineKeyboardButton("360p", callback_data=f"q360|{mid}"),
         InlineKeyboardButton("480p", callback_data=f"q480|{mid}"),
         InlineKeyboardButton("720p", callback_data=f"q720|{mid}")],
        [InlineKeyboardButton("🎞 All 3 qualities", callback_data=f"qall|{mid}")],
        [InlineKeyboardButton("📱 Portrait 9:16", callback_data=f"port|{mid}")],
    ])


# ───────────────────────── poster (TMDB + IMDb/OMDb) ─────────────────────────
async def tmdb_search(session, name):
    url = "https://api.themoviedb.org/3/search/multi"
    async with session.get(url, params={"api_key": TMDB_KEY, "query": name}) as r:
        data = await r.json()
    res = [x for x in data.get("results", []) if x.get("media_type") in ("movie", "tv")]
    return res[0] if res else None


async def omdb_lookup(session, name):
    if not OMDB_KEY:
        return None
    async with session.get("http://www.omdbapi.com/",
                           params={"apikey": OMDB_KEY, "t": name}) as r:
        d = await r.json()
    return d if d.get("Response") == "True" else None


@app.on_message(filters.command("poster"))
async def poster(_, m: Message):
    if len(m.command) < 2:
        return await m.reply("Usage: `/poster Inception 2010`")
    name = m.text.split(None, 1)[1]
    async with aiohttp.ClientSession() as s:
        tm, om = await asyncio.gather(tmdb_search(s, name), omdb_lookup(s, name))
    if not tm and not om:
        return await m.reply("❌ Not found.")

    title = (tm or {}).get("title") or (tm or {}).get("name") or om.get("Title")
    year = ((tm or {}).get("release_date") or (tm or {}).get("first_air_date") or om.get("Year", ""))[:4]
    rating = om.get("imdbRating") if om else None
    caption = f"🎬 **{title}** ({year})"
    if rating:
        caption += f"\n⭐ IMDb: {rating}"
    if tm and tm.get("overview"):
        caption += f"\n\n{tm['overview'][:600]}"

    poster_url = f"https://image.tmdb.org/t/p/original{tm['poster_path']}" if tm and tm.get("poster_path") else (om or {}).get("Poster")
    back_url = f"https://image.tmdb.org/t/p/original{tm['backdrop_path']}" if tm and tm.get("backdrop_path") else None

    if poster_url and poster_url != "N/A":
        await m.reply_photo(poster_url, caption=caption)
    if back_url:
        await m.reply_photo(back_url, caption="🖼 Cover / Backdrop")


# ───────────────────────── thumbnail ─────────────────────────
@app.on_message(filters.photo & filters.private)
async def save_thumb(_, m: Message):
    p = await m.download(file_name=f"{THUMBS}/{m.from_user.id}_raw.jpg")
    # Telegram thumbnails must be JPEG, <=320px, <200KB
    code, err = await run(["ffmpeg", "-y", "-i", p, "-vf",
                           "scale='min(320,iw)':-2", "-q:v", "4",
                           f"{THUMBS}/{m.from_user.id}.jpg"])
    os.remove(p)
    await m.reply("✅ Thumbnail saved. It will be applied to all your uploads.")


@app.on_message(filters.command("thumb") & filters.private)
async def show_thumb(_, m):
    t = thumb_path(m.from_user.id)
    await (m.reply_photo(t, caption="Your thumbnail") if t else m.reply("No thumbnail saved. Send a photo."))


@app.on_message(filters.command("delthumb") & filters.private)
async def del_thumb(_, m):
    t = thumb_path(m.from_user.id)
    if t:
        os.remove(t)
    await m.reply("🗑 Thumbnail deleted.")


# ───────────────────────── file received → menu ─────────────────────────
@app.on_message((filters.video | filters.document) & filters.private)
async def got_file(_, m: Message):
    await m.reply("What do you want to do?", reply_markup=menu(m.id), quote=True)


@app.on_message(filters.command("rename") & filters.reply & filters.private)
async def quick_rename(c, m: Message):
    if len(m.command) < 2:
        return await m.reply("Usage: reply to a file with `/rename New Name.mkv`")
    await do_rename(c, m.reply_to_message, m.text.split(None, 1)[1], m)


@app.on_message(filters.text & filters.private & ~filters.command(["poster", "thumb", "delthumb", "rename", "start"]))
async def text_in(c, m: Message):
    uid = m.from_user.id
    if uid in PENDING_RENAME:
        orig = await c.get_messages(m.chat.id, PENDING_RENAME.pop(uid))
        await do_rename(c, orig, m.text.strip(), m)


@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(
        "🎬 **Media Bot**\n\n"
        "• `/poster <movie>` – IMDb/TMDB poster + cover\n"
        "• Send a **photo** – saved as your thumbnail\n"
        "• Send a **video/file** – rename, MKV↔MP4, 360/480/720p, portrait 9:16\n"
        "• `/thumb`, `/delthumb` – manage thumbnail")


# ───────────────────────── actions ─────────────────────────
async def send_back(c, chat_id, path, uid, status, caption, as_video=True):
    dur, w, h = (0, 0, 0)
    if as_video:
        dur, w, h = await probe(path)
    cb = progress_cb(status, "⬆️ Uploading")
    t = thumb_path(uid)
    if as_video and path.lower().endswith((".mp4", ".mkv")):
        await c.send_video(chat_id, path, caption=caption, duration=dur, width=w, height=h,
                           thumb=t, supports_streaming=True, progress=cb)
    else:
        await c.send_document(chat_id, path, caption=caption, thumb=t, progress=cb)


async def do_rename(c, orig: Message, new_name, reply_to):
    media = orig.video or orig.document
    if not media:
        return await reply_to.reply("❌ No file found.")
    ext = os.path.splitext(media.file_name or "")[1] or ".mkv"
    if not os.path.splitext(new_name)[1]:
        new_name += ext
    status = await reply_to.reply("⬇️ Downloading…")
    d = f"{WORK}/{orig.id}_{int(time.time())}"
    os.makedirs(d, exist_ok=True)
    src = await orig.download(file_name=f"{d}/src", progress=progress_cb(status, "⬇️ Downloading"))
    dst = f"{d}/{new_name}"
    os.rename(src, dst)
    try:
        await send_back(c, orig.chat.id, dst, orig.from_user.id, status, f"`{new_name}`")
        await status.delete()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def ffmpeg_cmd(src, dst, mode):
    base = ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
    if mode in ("mp4", "mkv"):
        # remux (no re-encode) = very fast. MP4 can't hold most subs, so drop them.
        if mode == "mp4":
            return ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst]
        return ["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", dst]
    if mode.startswith("q"):
        h = QUALITIES[mode[1:]]
        return ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
                "-vf", f"scale=-2:{h}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst]
    if mode == "port":
        # 1080x1920 portrait, blurred background + centered original (no cropping)
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,boxblur=25:5[bg];"
              "[b]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
              "[bg][fg]overlay=(W-w)/2:(H-h)/2")
        return ["ffmpeg", "-y", "-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst]


@app.on_callback_query()
async def cb(c: Client, q: CallbackQuery):
    action, mid = q.data.split("|")
    msg = await c.get_messages(q.message.chat.id, int(mid))
    media = msg.video or msg.document
    if not media:
        return await q.answer("File not found", show_alert=True)

    if action == "rn":
        PENDING_RENAME[q.from_user.id] = int(mid)
        await q.message.edit_text("✏️ Send the new file name now.")
        return

    modes = ["q360", "q480", "q720"] if action == "qall" else [action]
    status = await q.message.edit_text("⬇️ Downloading…")
    d = f"{WORK}/{mid}_{int(time.time())}"
    os.makedirs(d, exist_ok=True)
    try:
        src = await msg.download(file_name=f"{d}/src", progress=progress_cb(status, "⬇️ Downloading"))
        base = os.path.splitext(media.file_name or "video")[0]
        for mode in modes:
            ext = "mkv" if mode == "mkv" else "mp4"
            suffix = {"port": "portrait"}.get(mode, mode)
            dst = f"{d}/{base}_{suffix}.{ext}"
            await status.edit_text(f"⚙️ Converting ({mode})…")
            async with JOB_SLOTS:
                code, err = await run(ffmpeg_cmd(src, dst, mode))
            if code != 0 or not os.path.exists(dst):
                await status.edit_text(f"❌ FFmpeg failed:\n`{err[-500:]}`")
                return
            await send_back(c, q.message.chat.id, dst, q.from_user.id, status, f"`{os.path.basename(dst)}`")
            os.remove(dst)
        await status.delete()
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def health_server():
    """Tiny web server so Render web services see an open PORT (and UptimeRobot can ping it)."""
    from aiohttp import web
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_):
        return web.Response(text="Media bot is running ✅")

    web_app = web.Application()
    web_app.add_routes(routes)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()


async def main():
    from pyrogram import idle
    await health_server()
    print("Bot started")
    await idle()   # app.run() handles start()/stop() around this


if __name__ == "__main__":
    app.run(main())
