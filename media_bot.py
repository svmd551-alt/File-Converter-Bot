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
  SESSION_STRING (optional, Telegram Premium account) -> files up to ~4 GB via Saved Messages
Run:
  python media_bot.py
"""
import os, re, math, asyncio, time, shutil, json, uuid, urllib.request, urllib.error
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler

try:                                   # faster event loop = faster transfers
    import uvloop
    uvloop.install()
except Exception:
    pass
from pyrogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                            CallbackQuery, Message)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_API_URL = os.environ.get("BOT_API_URL", "https://api.telegram.org").rstrip("/")
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()   # Premium user account (for 4 GB)

WORK = os.path.abspath("work")
THUMBS = os.path.abspath("thumbs")
shutil.rmtree(WORK, ignore_errors=True)      # clear leftovers of a crashed/restarted run (frees disk)
os.makedirs(WORK, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)

# Telegram bots: 2 GB per file. A Premium *user* account (SESSION_STRING): ~4 GB (4000 MiB).
MAX_BYTES = 2000 * 1024 * 1024
MAX_BYTES_USER = 4000 * 1024 * 1024
THUMB_MAX_BYTES = 190 * 1024                 # Telegram: thumbnail must be < 200 KB
THUMB_MAX_SIDE = 320                         # Telegram: thumbnail sides must be <= 320 px

# TRANSMISSIONS = how many files may transfer at the same time (NOT per-file speed;
# per-file upload speed comes from fast_upload.py: UPLOAD_CONNS / UPLOAD_WORKERS)
CONC = int(os.environ.get("TRANSMISSIONS", "12"))
app = Client("media_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             workers=32, max_concurrent_transmissions=CONC, sleep_threshold=60)
user = (Client("media_user", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING,
               workers=16, max_concurrent_transmissions=CONC, sleep_threshold=60)
        if SESSION_STRING else None)

# multi-connection upload (much faster for big files; falls back to stock Pyrogram if anything fails)
if os.environ.get("FAST_UPLOAD", "1") != "0":
    try:
        import fast_upload
        fast_upload.install(Client)
        print(f"[fast_upload] on: {fast_upload.CONNS} connections x {fast_upload.PER_CONN} parts", flush=True)
    except Exception as e:
        print(f"[fast_upload] not enabled: {e!r}", flush=True)

# ── speed settings (override with env vars on Render) ──
def effective_cpus():
    """CPU cores this container may really use (cgroup quota aware).
    os.cpu_count() returns the HOST's cores inside Docker, which is wrong on small plans."""
    try:
        n = float(len(os.sched_getaffinity(0)))
    except Exception:
        n = float(os.cpu_count() or 2)
    try:                                              # cgroup v2
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            n = min(n, int(quota) / int(period))
    except Exception:
        try:                                          # cgroup v1
            quota = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
            period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
            if quota > 0:
                n = min(n, quota / period)
        except Exception:
            pass
    return max(n, 0.1)


CPU = effective_cpus()
THREADS = os.environ.get("FFMPEG_THREADS") or str(max(1, math.ceil(CPU)))
print(f"[cpu] usable CPU cores: {CPU:.2f} -> ffmpeg threads: {THREADS}", flush=True)
PRESET = os.environ.get("X264_PRESET", "ultrafast")   # ultrafast = fastest, bigger file
CRF = os.environ.get("X264_CRF", "26")                # higher = faster/smaller, lower quality
PORTRAIT_W, PORTRAIT_H = (int(x) for x in os.environ.get("PORTRAIT_RES", "720x1280").split("x"))
# 1 heavy ffmpeg job at a time gets ALL cores (faster than 2 jobs fighting for CPU)
JOB_SLOTS = asyncio.Semaphore(1 if CPU < 6 else 2)
# cover jobs (download + upload): limit parallel big files so the disk never fills up
COVER_SLOTS = asyncio.Semaphore(int(os.environ.get("COVER_JOBS", "2")))
TASKS = set()
VENC = ["-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-threads", THREADS,
        "-pix_fmt", "yuv420p", "-sn"]
AENC = ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
ACTIVE, CANCELLED = {}, set()   # running ffmpeg processes / cancelled jobs (by status message id)
PENDING_RENAME = {}   # user_id -> message_id of file awaiting new name

QUALITIES = {"360": 360, "480": 480, "720": 720}


# ───────────────────────── helpers ─────────────────────────
def thumb_path(uid):
    p = f"{THUMBS}/{uid}.jpg"
    return p if os.path.exists(p) else None


def cover_path(uid):
    """HD cover photo (for Telegram's video-cover feature). Falls back to the small thumbnail."""
    p = f"{THUMBS}/{uid}_cover.jpg"
    return p if os.path.exists(p) else thumb_path(uid)


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
    """Download / upload progress. Edits run in the background so they never slow the transfer."""
    st = {"t": 0, "t0": time.time(), "busy": False, "block": 0}

    async def edit(text):
        try:
            await msg.edit_text(text)
        except FloodWait as e:
            st["block"] = time.time() + (getattr(e, "value", None) or getattr(e, "x", 5))
        except Exception:
            pass
        finally:
            st["busy"] = False

    async def cb(cur, total):
        now = time.time()
        if st["busy"] or now < st["block"] or (now - st["t"] < 5 and cur < total):
            return
        st["t"] = now
        total = total or cur or 1
        pct = cur * 100 / total
        el = max(now - st["t0"], 0.001)
        speed = cur / el
        eta = (total - cur) / speed if speed else 0
        st["busy"] = True
        t = asyncio.create_task(edit(render(label, pct,
            f"📦 {human(cur)} / {human(total)}\n🚀 {human(speed)}/s\n⏳ ETA {fmt_time(eta)}")))
        TASKS.add(t)
        t.add_done_callback(TASKS.discard)
    return cb


async def retry(fn, tries=4):
    """Run an async call; retry on network errors, wait out FloodWait."""
    attempt = floods = 0
    while True:
        try:
            return await fn()
        except FloodWait as e:
            floods += 1
            if floods > 10:
                raise
            await asyncio.sleep((getattr(e, "value", None) or getattr(e, "x", 5)) + 1)
        except asyncio.CancelledError:
            raise
        except Exception:
            attempt += 1
            if attempt >= tries:
                raise
            await asyncio.sleep(3 * attempt)


def safe_name(name, fallback):
    """Strip path characters and keep the name short enough for any filesystem."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .") or fallback
    base, ext = os.path.splitext(name)
    return base[:120] + ext[:10]


def enough_disk(size):
    try:
        return shutil.disk_usage(WORK).free > size * 1.05 + 100 * 1024 * 1024
    except Exception:
        return True


async def make_thumb(src, dst):
    """Make a Telegram-valid thumbnail: JPEG, max 320x320, under 200 KB. Tries lower quality until it fits."""
    tmp = dst + ".tmp.jpg"
    for q in (3, 6, 10, 15, 22, 31):
        code, _ = await run(["ffmpeg", "-y", "-i", src, "-frames:v", "1",
                             "-vf", f"scale={THUMB_MAX_SIDE}:{THUMB_MAX_SIDE}:force_original_aspect_ratio=decrease",
                             "-pix_fmt", "yuvj420p", "-q:v", str(q), tmp])
        if code == 0 and os.path.exists(tmp) and os.path.getsize(tmp) < THUMB_MAX_BYTES:
            os.replace(tmp, dst)
            return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


async def make_cover(src, dst):
    """HD JPEG for the video cover: fits inside 1280x1280 (never upscaled), under ~4 MB."""
    tmp = dst + ".tmp.jpg"
    for q in (2, 5, 9, 15, 24):
        code, _ = await run(["ffmpeg", "-y", "-i", src, "-frames:v", "1",
                             "-vf", "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease",
                             "-pix_fmt", "yuvj420p", "-q:v", str(q), tmp])
        if code == 0 and os.path.exists(tmp) and os.path.getsize(tmp) < 4 * 1024 * 1024:
            os.replace(tmp, dst)
            return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def _multipart(fields, files):
    boundary = "----cover" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                   + str(v).encode() + b"\r\n")
    for k, (fname, data, ctype) in files.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                   f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(out)


def _bot_api_post(method, fields, files, timeout=180):
    boundary, body = _multipart(fields, files)
    req = urllib.request.Request(f"{BOT_API_URL}/bot{BOT_TOKEN}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:                  # Bot API sends errors as JSON with HTTP 400
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}


async def add_cover_by_file_id(msg, uid):
    """Telegram 'video cover': resend the SAME video (by file_id) with a new cover.
    Nothing is downloaded or uploaded, so it is instant and works for ANY video size."""
    cp = cover_path(uid)
    with open(cp, "rb") as fh:
        cover = fh.read()
    cap = msg.caption
    plain = str(cap) if cap else ""
    html = getattr(cap, "html", plain) if cap else ""
    err = "unknown error"
    for use_html in ((True, False) if cap else (False,)):
        fields = {"chat_id": msg.chat.id, "video": msg.video.file_id, "cover": "attach://cover_file"}
        if cap:
            fields["caption"] = html if use_html else plain
            if use_html:
                fields["parse_mode"] = "HTML"
        data = await asyncio.to_thread(_bot_api_post, "sendVideo", fields,
                                       {"cover_file": ("cover.jpg", cover, "image/jpeg")})
        if data.get("ok"):
            return True, ""
        err = data.get("description", "unknown error")
        if "parse entities" not in err.lower():
            break
    return False, err


async def good_thumb(uid):
    """Return the user's thumbnail path, repairing old/invalid ones (wrong size/dimensions) first."""
    p = thumb_path(uid)
    if not p:
        return None
    try:
        _, w, h = await probe(p)
        ok = 0 < w <= THUMB_MAX_SIDE and 0 < h <= THUMB_MAX_SIDE and os.path.getsize(p) < THUMB_MAX_BYTES
    except Exception:
        ok = False
    if not ok and not await make_thumb(p, p):
        return None
    return p


async def run_ffmpeg(cmd, duration, status, label, key=None):
    """Run ffmpeg and live-update the status message (bar, speed, ETA)."""
    cmd = cmd[:1] + ["-nostdin", "-progress", "pipe:1", "-nostats"] + cmd[1:]
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    err_buf = bytearray()
    if key:
        ACTIVE[key] = p
    kb = (InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel", callback_data=f"cx|{key}")]])
          if key else None)

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
                slow = ("\n\n⚠️ Server is slow for this job. Cancel and convert ONE quality, "
                        "or use a stronger CPU plan.") if (el > 90 and spd < 0.5 and eta > 7200) else ""
                try:
                    await status.edit_text(render(label, pct,
                        f"🎞 {fmt_time(done)} / {fmt_time(duration)}\n"
                        f"🚀 {spd:.1f}x speed\n⏳ ETA {fmt_time(eta)}") + slow, reply_markup=kb)
                except Exception:
                    pass
    await p.wait()
    await err_task
    ACTIVE.pop(key, None)
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
async def store_thumb(m: Message):
    """Save a photo (or an image sent as file) as the user's cover."""
    uid = m.from_user.id
    raw = await m.download(file_name=f"{THUMBS}/{uid}_raw")
    try:
        ok = await make_thumb(raw, f"{THUMBS}/{uid}.jpg")
        if ok:
            await make_cover(raw, f"{THUMBS}/{uid}_cover.jpg")      # HD copy for the instant-cover method
    finally:
        if raw and os.path.exists(raw):
            os.remove(raw)
    await m.reply("✅ **Cover saved!**\nNow send your video and I will add it automatically." if ok
                  else "❌ Could not use this image. Please send a different photo.")


@app.on_message(filters.photo & filters.private)
async def save_thumb(_, m: Message):
    await store_thumb(m)


@app.on_message(filters.command(["show_cover", "thumb"]) & filters.private)
async def show_thumb(_, m):
    t = thumb_path(m.from_user.id)
    await (m.reply_photo(t, caption="🖼 Your current cover") if t
           else m.reply("No cover saved yet. Send me a photo first."))


@app.on_message(filters.command(["del_cover", "delthumb"]) & filters.private)
async def del_thumb(_, m):
    for p in (thumb_path(m.from_user.id), f"{THUMBS}/{m.from_user.id}_cover.jpg"):
        if p and os.path.exists(p):
            os.remove(p)
    await m.reply("🗑 Cover deleted.")


async def apply_cover(c, msg: Message, limit=MAX_BYTES, uid=None):
    """Re-upload the video/file with the user's cover. No FFmpeg re-encode = no quality loss.
    Works for any size Telegram allows: queue, disk check, retries, FloodWait handling."""
    uid = uid or msg.from_user.id
    media = msg.video or msg.document
    size = media.file_size or 0
    if not await good_thumb(uid):
        return await msg.reply("🖼 No cover found. Send me a **photo** (to the bot) first, then try again.",
                               quote=True)
    # FAST PATH (any size, no download/upload): resend the same video by file_id with the new cover
    if c is app and msg.video:
        st = await msg.reply("⚡ Adding cover (no upload needed)…", quote=True)
        ok, err = await add_cover_by_file_id(msg, uid)
        if ok:
            return await st.delete()
        print(f"[cover] file_id method failed: {err}", flush=True)
        if size > limit:                                  # cannot fall back to re-upload for huge files
            return await st.edit_text(f"❌ Could not add the cover: `{err}`")
        await st.delete()                                 # small enough -> fall back to re-upload below
    if size > limit:
        mb = size // 1024 // 1024
        if c is app and user:
            tip = ("\n\n💡 Forward this file to your **Saved Messages** — my Premium helper will add "
                   "the cover (up to ~4 GB).")
        elif c is app:
            tip = ("\n\n💡 To upload files up to ~4 GB, add the Premium helper: set `SESSION_STRING` "
                   "(see README, needs a Telegram Premium account).")
        else:
            tip = ""
        who = "A bot can upload at most 2000 MB" if c is app else "Telegram's limit is about 4000 MB"
        return await msg.reply(f"❌ This file is {mb} MB. {who}.{tip}", quote=True)
    name = safe_name(media.file_name, f"video_{msg.id}.mp4")
    status = await msg.reply("⏳ Queued… starting soon.", quote=True)
    d = f"{WORK}/{msg.id}_{int(time.time())}"
    os.makedirs(d, exist_ok=True)
    try:
        async with COVER_SLOTS:
            if not enough_disk(size):
                return await status.edit_text("❌ Server disk is full right now. Please try again in a few minutes.")
            await status.edit_text("⬇️ Starting download…")
            path = await retry(lambda: msg.download(file_name=f"{d}/{name}",
                                                    progress=progress_cb(status, "⬇️ Downloading")))
            if not path:
                return await status.edit_text("❌ Download failed. Please send the file again.")
            thumb = await good_thumb(uid)
            kw = dict(caption=msg.caption or "", caption_entities=msg.caption_entities,
                      parse_mode=enums.ParseMode.DISABLED, thumb=thumb)
            if msg.video:
                await retry(lambda: c.send_video(
                    msg.chat.id, path, duration=media.duration or 0, width=media.width or 0,
                    height=media.height or 0, supports_streaming=True,
                    progress=progress_cb(status, "⬆️ Uploading"), **kw))
            else:
                await retry(lambda: c.send_document(
                    msg.chat.id, path, file_name=name,
                    progress=progress_cb(status, "⬆️ Uploading"), **kw))
            await status.delete()
    except Exception as e:
        try:
            await status.edit_text(f"❌ Failed: `{e}`")
        except Exception:
            pass
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── file received ─────────────────────────
@app.on_message((filters.video | filters.document) & filters.private)
async def got_file(c, m: Message):
    uid = m.from_user.id
    if m.document and (m.document.mime_type or "").startswith("image/"):
        return await store_thumb(m)             # poster sent as a file -> use it as cover
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
        "Cover works on videos of ANY size (instant, nothing is uploaded again).\n\n"
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
    t = await good_thumb(uid)
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
                                             f"⚙️ Converting {', '.join(str(h) + 'p' for h in targets)}",
                                             key=status.id)
            if status.id in CANCELLED:
                CANCELLED.discard(status.id)
                return await status.edit_text("🛑 Conversion cancelled.")
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
                                             f"⚙️ Converting ({mode})", key=status.id)
            if status.id in CANCELLED:
                CANCELLED.discard(status.id)
                return await status.edit_text("🛑 Conversion cancelled.")
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


@app.on_callback_query(filters.regex(r"^cx\|"))
async def cancel_job(c: Client, q: CallbackQuery):
    key = int(q.data.split("|")[1])
    p = ACTIVE.get(key)
    if not p or p.returncode is not None:
        return await q.answer("Nothing to cancel.", show_alert=True)
    CANCELLED.add(key)
    p.kill()
    await q.answer("Cancelling…")


# ───────────────── Premium helper (files up to ~4 GB via Saved Messages) ─────────────────
if user:
    async def _saved(_, c, m):
        return bool(c.me and m.chat and m.chat.id == c.me.id)
    saved = filters.create(_saved)

    async def user_forwarded(c, m: Message):
        await apply_cover(c, m, MAX_BYTES_USER, c.me.id)

    async def user_cover_cmd(c, m: Message):
        t = m.reply_to_message
        if not t or not (t.video or t.document):
            return await m.reply("Reply to a video/file with /cover")
        await apply_cover(c, t, MAX_BYTES_USER, c.me.id)

    # forward a video to Saved Messages -> cover added automatically
    user.add_handler(MessageHandler(user_forwarded,
                     saved & filters.forwarded & (filters.video | filters.document)))
    # or upload a video to Saved Messages and reply to it with /cover
    user.add_handler(MessageHandler(user_cover_cmd, saved & filters.command("cover") & filters.reply))


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
    if user:
        await user.start()
        um = await user.get_me()
        print(f"Helper account started: {um.first_name} (Premium: {bool(um.is_premium)})", flush=True)
        if not um.is_premium:
            print("WARNING: helper account is not Premium -> 4 GB uploads will fail (2 GB max).", flush=True)
    await idle()
    if user:
        await user.stop()
    await app.stop()


if __name__ == "__main__":
    app.run(main())