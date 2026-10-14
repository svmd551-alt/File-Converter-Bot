"""
Telegram Media Bot (Pyrogram + FFmpeg)
--------------------------------------
Features
  Send a video/file      -> menu: Rename | MKV<->MP4 | 360/480/720p | Portrait 9:16 | Set thumbnail
  Send a photo           -> saved as your custom thumbnail (used on every upload)
  /thumb  /delthumb      -> view / delete saved thumbnail
  /rename <new name>     -> reply to a file with this to rename instantly

Install:
  pip install -U pyrogram tgcrypto aiohttp aiofiles
  apt install ffmpeg
Env vars:
  API_ID, API_HASH, BOT_TOKEN
Run:
  python media_bot.py
"""
import os, asyncio, time, shutil, json
from pyrogram import Client, filters, enums
from pyrogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                            CallbackQuery, Message)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

WORK = "work"
THUMBS = "thumbs"
os.makedirs(WORK, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)

app = Client("media_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             workers=32, max_concurrent_transmissions=8)

# ── speed settings (override with env vars on Render) ──
CPU = os.cpu_count() or 2
PRESET = os.environ.get("X264_PRESET", "ultrafast")   # ultrafast = fastest, bigger file
CRF = os.environ.get("X264_CRF", "26")                # higher = faster/smaller, lower quality
PORTRAIT_W, PORTRAIT_H = (int(x) for x in os.environ.get("PORTRAIT_RES", "720x1280").split("x"))
# 1 heavy ffmpeg job at a time gets ALL cores (faster than 2 jobs fighting for CPU)
JOB_SLOTS = asyncio.Semaphore(1 if CPU <= 4 else 2)
VENC = ["-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-threads", "0",
        "-pix_fmt", "yuv420p", "-sn"]
AENC = ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
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


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def fmt_time(sec):
    sec = int(max(sec, 0))
    h, r = divmod(sec, 3600)
    m, s_ = divmod(r, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m:02d}:{s_:02d}"


def bar(pct, n=12):
    f = int(n * pct / 100)
    return "█" * f + "░" * (n - f)


def render(label, pct, extra=""):
    return f"{label}\n`[{bar(pct)}]` **{pct:.1f}%**\n{extra}"


def progress_cb(msg, label):
    """Download / upload progress: bar, size, speed, ETA."""
    st = {"t": 0, "t0": time.time()}

    async def cb(cur, total):
        now = time.time()
        if now - st["t"] < 3 and cur < total:
            return
        st["t"] = now
        total = total or cur or 1
        pct = cur * 100 / total
        el = max(now - st["t0"], 0.001)
        speed = cur / el
        eta = (total - cur) / speed if speed else 0
        try:
            await msg.edit_text(render(label, pct,
                f"📦 {human(cur)} / {human(total)}\n🚀 {human(speed)}/s\n⏳ ETA {fmt_time(eta)}"))
        except Exception:
            pass
    return cb


async def run_ffmpeg(cmd, duration, status, label):
    """Run ffmpeg and live-update the status message (bar, speed, ETA)."""
    cmd = cmd[:1] + ["-nostdin", "-progress", "pipe:1", "-nostats"] + cmd[1:]
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    err_buf = bytearray()

    async def read_err():
        while True:
            chunk = await p.stderr.read(4096)
            if not chunk:
                break
            err_buf.extend(chunk)
            del err_buf[:-4000]

    err_task = asyncio.create_task(read_err())
    t0, last = time.time(), 0
    while True:
        line = await p.stdout.readline()
        if not line:
            break
        line = line.decode(errors="ignore").strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                done = int(line.split("=")[1]) / 1_000_000
            except ValueError:
                continue
            now = time.time()
            if duration and now - last >= 3:
                last = now
                pct = min(done * 100 / duration, 99.9)
                el = max(now - t0, 0.001)
                spd = done / el
                eta = (duration - done) / spd if spd else 0
                try:
                    await status.edit_text(render(label, pct,
                        f"🎞 {fmt_time(done)} / {fmt_time(duration)}\n"
                        f"🚀 {spd:.1f}x speed\n⏳ ETA {fmt_time(eta)}"))
                except Exception:
                    pass
    await p.wait()
    await err_task
    return p.returncode, err_buf.decode(errors="ignore")


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


# ───────────────────────── settings (per user) ─────────────────────────
SETTINGS_FILE = f"{THUMBS}/settings.json"
DEFAULTS = {"auto": True}


def load_settings():
    try:
        return json.load(open(SETTINGS_FILE))
    except Exception:
        return {}


def get_set(uid):
    return {**DEFAULTS, **load_settings().get(str(uid), {})}


def toggle_set(uid, key):
    data = load_settings()
    cur = {**DEFAULTS, **data.get(str(uid), {})}
    cur[key] = not cur[key]
    data[str(uid)] = cur
    json.dump(data, open(SETTINGS_FILE, "w"))
    return cur


def settings_kb(uid):
    st = get_set(uid)
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"⚡ Auto cover on video: {'ON ✅' if st['auto'] else 'OFF ❌'}", callback_data="set|auto")]])


SETTINGS_TEXT = ("⚙️ **Settings**\n\n"
                 "**Auto cover ON** – send a video and the cover is added instantly.\n"
                 "**Auto cover OFF** – send a video and you get the tools menu "
                 "(rename, convert, qualities, portrait).")


@app.on_message(filters.command("settings") & filters.private)
async def settings_cmd(_, m: Message):
    await m.reply(SETTINGS_TEXT, reply_markup=settings_kb(m.from_user.id))


@app.on_callback_query(filters.regex(r"^set\|"))
async def settings_cb(_, q: CallbackQuery):
    toggle_set(q.from_user.id, q.data.split("|")[1])
    await q.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb(q.from_user.id))
    await q.answer("Saved")


# ───────────────────────── cover / thumbnail (MAIN FEATURE) ─────────────────────────
@app.on_message(filters.photo & filters.private)
async def save_thumb(_, m: Message):
    p = await m.download(file_name=f"{THUMBS}/{m.from_user.id}_raw.jpg")
    # Telegram thumbnails must be JPEG, <=320px, <200KB
    await run(["ffmpeg", "-y", "-i", p, "-vf", "scale='min(320,iw)':-2", "-q:v", "4",
               f"{THUMBS}/{m.from_user.id}.jpg"])
    os.remove(p)
    await m.reply("✅ **Cover saved!**\nNow send your video and I will add it automatically.")


@app.on_message(filters.command(["show_cover", "thumb"]) & filters.private)
async def show_thumb(_, m):
    t = thumb_path(m.from_user.id)
    await (m.reply_photo(t, caption="🖼 Your current cover") if t
           else m.reply("No cover saved yet. Send me a photo first."))


@app.on_message(filters.command(["del_cover", "delthumb"]) & filters.private)
async def del_thumb(_, m):
    t = thumb_path(m.from_user.id)
    if t:
        os.remove(t)
    await m.reply("🗑 Cover deleted.")


async def apply_cover(c, msg: Message):
    """Re-upload the user's video/file with their cover. No FFmpeg = no quality loss, very fast."""
    uid = msg.from_user.id
    media = msg.video or msg.document
    name = media.file_name or f"video_{msg.id}.mp4"
    status = await msg.reply("⬇️ Starting download…", quote=True)
    d = f"{WORK}/{msg.id}_{int(time.time())}"
    os.makedirs(d, exist_ok=True)
    try:
        path = await msg.download(file_name=f"{d}/{name}",
                                  progress=progress_cb(status, "⬇️ Downloading"))
        up = progress_cb(status, "⬆️ Uploading")
        kw = dict(caption=msg.caption or "", caption_entities=msg.caption_entities,
                  parse_mode=enums.ParseMode.DISABLED, thumb=thumb_path(uid), progress=up)
        if msg.video:
            await c.send_video(msg.chat.id, path, duration=media.duration, width=media.width,
                               height=media.height, supports_streaming=True, **kw)
        else:
            await c.send_document(msg.chat.id, path, file_name=name, **kw)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Failed: `{e}`")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── file received ─────────────────────────
@app.on_message((filters.video | filters.document) & filters.private)
async def got_file(c, m: Message):
    uid = m.from_user.id
    if get_set(uid)["auto"] and thumb_path(uid):
        return await apply_cover(c, m)          # main flow: instant cover
    hint = "" if thumb_path(uid) else "\n\n💡 Send a **photo** first to set your cover."
    await m.reply("What do you want to do?" + hint, reply_markup=menu(m.id), quote=True)


@app.on_message(filters.command("rename") & filters.reply & filters.private)
async def quick_rename(c, m: Message):
    if len(m.command) < 2:
        return await m.reply("Usage: reply to a file with `/rename New Name.mkv`")
    await do_rename(c, m.reply_to_message, m.text.split(None, 1)[1], m)


@app.on_message(filters.text & filters.private &
                ~filters.command(["thumb", "delthumb", "show_cover", "del_cover",
                                  "settings", "rename", "start"]))
async def text_in(c, m: Message):
    uid = m.from_user.id
    if uid in PENDING_RENAME:
        orig = await c.get_messages(m.chat.id, PENDING_RENAME.pop(uid))
        await do_rename(c, orig, m.text.strip(), m)


@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(
        "Welcome to 🎬 **Fast Thumbnail Bot**\n\n"
        "- Add a custom **cover/thumbnail** to your videos instantly!\n\n"
        "**How it works:** 🤔\n"
        "1. Send your **photo** (cover/thumbnail).\n"
        "2. Send your **video**, and the bot will add the cover automatically.\n\n"
        "✱ **Commands:**\n"
        "/settings - Customize bot settings\n"
        "/show_cover - View your current cover\n"
        "/del_cover - Delete your saved cover\n"
        "\n"
        "Extra tools (turn Auto cover OFF in /settings): rename, MKV↔MP4, "
        "360/480/720p, portrait 9:16.")


# ───────────────────────── actions ─────────────────────────
async def send_back(c, chat_id, path, uid, status, caption, as_video=True):
    dur, w, h = (0, 0, 0)
    if as_video:
        dur, w, h = await probe(path)
    cb = progress_cb(status, f"⬆️ Uploading `{os.path.basename(path)[:40]}`")
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
    status = await reply_to.reply("⬇️ Starting download…")
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
    if mode == "mp4":      # remux video (instant), only audio -> aac for compatibility
        return ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?", "-c:v", "copy",
                *AENC, "-movflags", "+faststart", dst]
    if mode == "mkv":      # pure stream copy = instant
        return ["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", dst]
    if mode.startswith("q"):
        h = QUALITIES[mode[1:]]
        return ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
                "-vf", f"scale=-2:{h}:flags=fast_bilinear", *VENC, *AENC,
                "-movflags", "+faststart", dst]
    if mode == "port":
        W, H = PORTRAIT_W, PORTRAIT_H
        # background is blurred at TINY size then scaled up = much faster than blurring full-res
        vf = (f"split[a][b];[a]scale={W // 10}:{H // 10}:force_original_aspect_ratio=increase,"
              f"crop={W // 10}:{H // 10},boxblur=3:1,scale={W}:{H}:flags=fast_bilinear[bg];"
              f"[b]scale={W}:-2:flags=fast_bilinear[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
        return ["ffmpeg", "-y", "-i", src, "-map", "0:a?", "-filter_complex", vf,
                *VENC, *AENC, "-movflags", "+faststart", dst]


def ffmpeg_multi(src, outputs):
    """ONE pass: decode once, encode several qualities at the same time.
    outputs = [(height, dst_path), ...]"""
    n = len(outputs)
    labels = "".join(f"[v{i}]" for i in range(n))
    fc = f"[0:v:0]split={n}{labels};" + ";".join(
        f"[v{i}]scale=-2:{h}:flags=fast_bilinear[o{i}]" for i, (h, _) in enumerate(outputs))
    cmd = ["ffmpeg", "-y", "-i", src, "-filter_complex", fc]
    for i, (_, dst) in enumerate(outputs):
        cmd += ["-map", f"[o{i}]", "-map", "0:a?", *VENC, *AENC, "-movflags", "+faststart", dst]
    return cmd


@app.on_callback_query(filters.regex(r"^(rn|mp4|mkv|q360|q480|q720|qall|port)\|"))
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
    status = await q.message.edit_text("⬇️ Starting download…")
    d = f"{WORK}/{mid}_{int(time.time())}"
    os.makedirs(d, exist_ok=True)
    try:
        src = await msg.download(file_name=f"{d}/src", progress=progress_cb(status, "⬇️ Downloading"))
        base = os.path.splitext(media.file_name or "video")[0]
        outs = []   # list of dst paths to upload

        if action == "qall":
            _, _, src_h = await probe(src)
            targets = [h for h in (360, 480, 720) if h <= max(src_h, 360)]   # never upscale
            paths = [(h, f"{d}/{base}_{h}p.mp4") for h in targets]
            dur = (await probe(src))[0]
            await status.edit_text(f"⏳ Waiting for a free converter slot…")
            async with JOB_SLOTS:
                code, err = await run_ffmpeg(ffmpeg_multi(src, paths), dur, status,
                                             f"⚙️ Converting {', '.join(str(h) + 'p' for h in targets)}")
            if code != 0:
                return await status.edit_text(f"❌ FFmpeg failed:\n`{err[-500:]}`")
            outs = [p for _, p in paths]
        else:
            mode = modes[0]
            ext = "mkv" if mode == "mkv" else "mp4"
            suffix = {"port": "portrait"}.get(mode, mode)
            dst = f"{d}/{base}_{suffix}.{ext}"
            dur = (await probe(src))[0]
            await status.edit_text("⏳ Waiting for a free converter slot…")
            async with JOB_SLOTS:
                code, err = await run_ffmpeg(ffmpeg_cmd(src, dst, mode), dur, status,
                                             f"⚙️ Converting ({mode})")
            if code != 0 or not os.path.exists(dst):
                return await status.edit_text(f"❌ FFmpeg failed:\n`{err[-500:]}`")
            outs = [dst]

        os.remove(src)
        # upload all results in parallel
        sts = [status] + [await status.reply("⬆️ Preparing upload…") for _ in outs[1:]]
        await asyncio.gather(*(send_back(c, q.message.chat.id, p, q.from_user.id, st,
                                         f"`{os.path.basename(p)}`") for p, st in zip(outs, sts)))
        for st in sts:
            await st.delete()
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
    await app.start()          # app.run(coroutine) does NOT start the client by itself
    me = await app.get_me()
    print(f"Bot started as @{me.username}", flush=True)
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
