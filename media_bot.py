import os
import re
import json
import time
import math
import shutil
import asyncio
import logging
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

# Required environment variables: API_ID, API_HASH, BOT_TOKEN
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

WORK = Path(os.environ.get("WORK_DIR", "work")).resolve()
THUMBS = Path(os.environ.get("THUMB_DIR", "thumbs")).resolve()
WORK.mkdir(parents=True, exist_ok=True)
THUMBS.mkdir(parents=True, exist_ok=True)

MAX_BOT_FILE = int(os.environ.get("MAX_BOT_FILE_MB", "2000")) * 1024 * 1024
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")
FFMPEG_PRESET = os.environ.get("X264_PRESET", "veryfast")
FFMPEG_CRF = os.environ.get("X264_CRF", "23")
PROGRESS_INTERVAL = max(2, int(os.environ.get("PROGRESS_INTERVAL", "4")))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("media-bot")

app = Client(
    "media_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=16,
    max_concurrent_transmissions=max(1, int(os.environ.get("TRANSMISSIONS", "2"))),
    sleep_threshold=60,
)

DEFAULT_SETTINGS = {"auto_cover": True}
SETTINGS_FILE = THUMBS / "settings.json"
ACTIVE = {}       # (user_id, status_message_id) -> asyncio subprocess
JOBS = set()
QUALITY = {"360": 360, "480": 480, "720": 720}
THUMB_MAX_BYTES = 190 * 1024
THUMB_MAX_SIDE = 320


def safe_name(name, fallback="file"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    if not name:
        name = fallback
    stem, ext = os.path.splitext(name)
    return stem[:120] + ext[:12]


def human(n):
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def time_text(seconds):
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def progress_bar(pct, width=12):
    filled = max(0, min(width, int(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def status_text(label, pct, extra=""):
    return f"{label}\n`[{progress_bar(pct)}]` **{pct:.1f}%**\n{extra}"


def user_thumb(uid):
    p = THUMBS / f"{uid}.jpg"
    return p if p.exists() else None


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_settings(uid):
    return {**DEFAULT_SETTINGS, **load_settings().get(str(uid), {})}


def save_setting(uid, key, value):
    data = load_settings()
    current = {**DEFAULT_SETTINGS, **data.get(str(uid), {})}
    current[key] = value
    data[str(uid)] = current
    temp = SETTINGS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(SETTINGS_FILE)
    return current


def main_menu(mid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"rename|{mid}")],
        [InlineKeyboardButton("🔁 MP4", callback_data=f"convert|mp4|{mid}"),
         InlineKeyboardButton("🔁 MKV", callback_data=f"convert|mkv|{mid}")],
        [InlineKeyboardButton("360p", callback_data=f"quality|360|{mid}"),
         InlineKeyboardButton("480p", callback_data=f"quality|480|{mid}"),
         InlineKeyboardButton("720p", callback_data=f"quality|720|{mid}")],
        [InlineKeyboardButton("🎞 All qualities", callback_data=f"allquality|{mid}")],
        [InlineKeyboardButton("📱 Portrait 9:16", callback_data=f"portrait|{mid}")],
    ])


def settings_keyboard(uid):
    enabled = get_settings(uid)["auto_cover"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"⚡ Auto cover: {'ON ✅' if enabled else 'OFF ❌'}",
            callback_data="setting|auto_cover"
        )
    ]])


async def safe_edit(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except FloodWait as e:
        await asyncio.sleep(getattr(e, "value", 5))
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    except Exception:
        pass


async def run_process(cmd, status, label, duration=0, key=None):
    """Run a subprocess and report FFmpeg progress. Returns (return_code, stderr)."""
    cmd = list(cmd)
    if cmd and Path(cmd[0]).name == "ffmpeg":
        cmd[1:1] = ["-nostdin", "-hide_banner", "-progress", "pipe:1", "-nostats"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if key:
        ACTIVE[key] = proc
    stderr_chunks = []

    async def read_stderr():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)
            if sum(map(len, stderr_chunks)) > 16000:
                stderr_chunks[:] = [b"".join(stderr_chunks)[-12000:]]

    err_task = asyncio.create_task(read_stderr())
    started = time.monotonic()
    last_edit = 0
    out_time = 0
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "ignore").strip()
            if text.startswith("out_time_ms=") or text.startswith("out_time_us="):
                try:
                    out_time = int(text.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                now = time.monotonic()
                if duration > 0 and now - last_edit >= PROGRESS_INTERVAL:
                    last_edit = now
                    pct = min(99.5, out_time * 100 / duration)
                    elapsed = max(now - started, 0.1)
                    speed = out_time / elapsed
                    eta = (duration - out_time) / speed if speed > 0 else 0
                    await safe_edit(status, status_text(
                        label, pct,
                        f"🎞 {time_text(out_time)} / {time_text(duration)}\n"
                        f"🚀 {speed:.2f}x speed\n⏳ ETA {time_text(eta)}"
                    ), cancel_keyboard(key))
        await proc.wait()
        await err_task
        return proc.returncode, b"".join(stderr_chunks).decode("utf-8", "ignore")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        if key:
            ACTIVE.pop(key, None)


def cancel_keyboard(key):
    if not key:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Cancel", callback_data=f"cancel|{key[0]}|{key[1]}")
    ]])


async def ffprobe(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        return max(0.0, float(out.decode().strip()))
    except (ValueError, TypeError):
        return 0.0


async def make_thumbnail(source, destination):
    tmp = str(destination) + ".tmp.jpg"
    for quality in (4, 8, 12, 18, 25, 31):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-frames:v", "1",
            "-vf", f"scale={THUMB_MAX_SIDE}:{THUMB_MAX_SIDE}:force_original_aspect_ratio=decrease",
            "-pix_fmt", "yuvj420p", "-q:v", str(quality), tmp,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, _ = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) < THUMB_MAX_BYTES:
            os.replace(tmp, destination)
            return True
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


async def download_media(client, media_message, destination, status, label):
    size = getattr(media_message.video or media_message.document or media_message.audio, "file_size", 0) or 0
    started = time.monotonic()
    state = {"last": 0.0}

    async def callback(current, total):
        now = time.monotonic()
        if now - state["last"] < PROGRESS_INTERVAL and current < total:
            return
        state["last"] = now
        pct = current * 100 / (total or size or 1)
        elapsed = max(now - started, 0.1)
        speed = current / elapsed
        eta = ((total or size) - current) / speed if speed else 0
        await safe_edit(status, status_text(
            label, pct, f"📦 {human(current)} / {human(total or size)}\n"
            f"🚀 {human(speed)}/s\n⏳ ETA {time_text(eta)}"
        ))
    return await client.download_media(media_message, file_name=str(destination), progress=callback)


async def upload_result(client, chat_id, source, original, status, thumb=None):
    size = os.path.getsize(source)
    if size > MAX_BOT_FILE:
        raise ValueError(
            f"Output is {human(size)}, larger than configured bot upload limit "
            f"{human(MAX_BOT_FILE)}. Telegram bot file limits depend on the API/server."
        )
    caption = original.caption or ""
    ext = Path(source).suffix.lower()
    started = time.monotonic()
    state = {"last": 0.0}

    async def callback(current, total):
        now = time.monotonic()
        if now - state["last"] < PROGRESS_INTERVAL and current < total:
            return
        state["last"] = now
        elapsed = max(now - started, 0.1)
        speed = current / elapsed
        eta = (total - current) / speed if speed else 0
        await safe_edit(status, status_text(
            "📤 Uploading result", current * 100 / (total or size or 1),
            f"📦 {human(current)} / {human(total or size)}\n"
            f"🚀 {human(speed)}/s\n⏳ ETA {time_text(eta)}"
        ))
    if ext in (".mp4", ".mov", ".m4v", ".webm"):
        kwargs = {"caption": caption, "progress": callback}
        if thumb and Path(thumb).exists():
            kwargs["thumb"] = str(thumb)
        await client.send_video(chat_id, str(source), **kwargs)
    else:
        kwargs = {"caption": caption, "progress": callback}
        if thumb and Path(thumb).exists():
            kwargs["thumb"] = str(thumb)
        await client.send_document(chat_id, str(source), **kwargs)


def find_media(message):
    if message.video:
        return message.video
    if message.document:
        return message.document
    if message.audio:
        return message.audio
    return None


async def get_source_message(client, chat_id, mid):
    try:
        return await client.get_messages(chat_id, mid)
    except Exception:
        return None


@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message):
    await message.reply_text(
        "🎬 **Telegram Media Tools**\n\n"
        "Send me a video or document to open the tools menu.\n"
        "Send a photo to save it as your custom thumbnail.\n\n"
        "/thumb — view saved thumbnail\n"
        "/delthumb — delete saved thumbnail\n"
        "/settings — auto-cover settings\n"
        "Reply to a file with `/rename New Name.mp4` to rename it."
    )


@app.on_message(filters.command("settings") & filters.private)
async def settings_cmd(_, message):
    await message.reply_text("⚙️ **Settings**\nAuto cover adds your saved thumbnail to newly sent videos.", reply_markup=settings_keyboard(message.from_user.id))


@app.on_callback_query(filters.regex(r"^setting\|"))
async def settings_callback(_, query):
    uid = query.from_user.id
    current = get_settings(uid)["auto_cover"]
    save_setting(uid, "auto_cover", not current)
    await query.message.edit_reply_markup(settings_keyboard(uid))
    await query.answer("Settings saved")


@app.on_message(filters.photo & filters.private)
async def save_thumb(_, message):
    uid = message.from_user.id
    status = await message.reply_text("🖼 Saving thumbnail…")
    raw = WORK / f"{uid}_{message.id}_thumb_source"
    try:
        downloaded = await message.download(file_name=str(raw))
        if not downloaded:
            raise RuntimeError("Telegram photo download failed")
        target = THUMBS / f"{uid}.jpg"
        if await make_thumbnail(downloaded, target):
            await status.edit_text("✅ **Thumbnail saved.** Send a video to use it.")
        else:
            await status.edit_text("❌ Could not process that photo. Try another image.")
    except Exception as e:
        log.exception("Thumbnail save failed")
        await status.edit_text(f"❌ Thumbnail error: `{str(e)[:250]}`")
    finally:
        try:
            if raw.exists():
                raw.unlink()
        except OSError:
            pass


@app.on_message(filters.command(["thumb", "show_cover"]) & filters.private)
async def show_thumb(_, message):
    thumb = user_thumb(message.from_user.id)
    if thumb:
        await message.reply_photo(str(thumb), caption="🖼 Your saved thumbnail")
    else:
        await message.reply_text("No thumbnail saved. Send me a photo first.")


@app.on_message(filters.command(["delthumb", "del_cover"]) & filters.private)
async def delete_thumb(_, message):
    thumb = user_thumb(message.from_user.id)
    if thumb:
        try:
            thumb.unlink()
        except OSError:
            pass
    await message.reply_text("🗑 Thumbnail deleted.")


@app.on_message(filters.command("rename") & filters.private)
async def rename_command(client, message):
    if not message.reply_to_message:
        return await message.reply_text("Reply to a video/document with `/rename NewName.mp4`.")
    media = find_media(message.reply_to_message)
    if not media:
        return await message.reply_text("Reply to a video or document.")
    new_name = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    if not new_name:
        return await message.reply_text("Usage: reply to a file with `/rename NewName.mp4`.")
    await process_file(client, message.reply_to_message, "rename", new_name=new_name)


@app.on_message((filters.video | filters.document | filters.audio) & filters.private)
async def media_received(client, message):
    media = find_media(message)
    if not media:
        return
    if get_settings(message.from_user.id)["auto_cover"] and message.video and user_thumb(message.from_user.id):
        # Bot API does not support changing an already uploaded file's cover in place.
        # Re-upload with a thumbnail to apply the user's saved image.
        await process_file(client, message, "cover")
    else:
        await message.reply_text("Choose an action:", reply_markup=main_menu(message.id))


async def process_file(client, original, action, target=None, new_name=None):
    uid = original.from_user.id if original.from_user else 0
    media = find_media(original)
    if not media:
        return
    if media.file_size and media.file_size > MAX_BOT_FILE:
        return await original.reply_text(
            f"❌ This file is {human(media.file_size)}. Configured maximum is {human(MAX_BOT_FILE)}. "
            "A bot token alone may not support larger files."
        )
    job_id = (uid, original.id)
    status = await original.reply_text("⏳ Preparing…")
    input_path = WORK / f"{uid}_{original.id}_input"
    output_path = None
    try:
        await download_media(client, original, input_path, status, "📥 Downloading")
        if not input_path.exists():
            raise RuntimeError("Download did not create a file")
        thumb = user_thumb(uid)
        if action == "cover":
            if not thumb:
                return await status.edit_text("🖼 No saved thumbnail. Send a photo first.")
            output_path = WORK / f"{uid}_{original.id}_cover{Path(getattr(media, 'file_name', '') or 'video.mp4').suffix or '.mp4'}"
            shutil.copy2(input_path, output_path)
            await safe_edit(status, "📤 Sending video with your saved thumbnail…")
            await upload_result(client, original.chat.id, output_path, original, status, str(thumb))
        elif action == "rename":
            old = getattr(media, "file_name", None) or f"file_{original.id}"
            ext = Path(old).suffix
            name = safe_name(new_name, "renamed_file")
            if not Path(name).suffix and ext:
                name += ext
            output_path = WORK / name
            if output_path.resolve() == input_path.resolve():
                output_path = WORK / f"renamed_{name}"
            shutil.copy2(input_path, output_path)
            await upload_result(client, original.chat.id, output_path, original, status, str(thumb) if thumb else None)
        else:
            duration = await ffprobe(input_path)
            original_name = getattr(media, "file_name", None) or f"video_{original.id}.mp4"
            stem = Path(original_name).stem
            if action == "convert":
                ext = target
                output_path = WORK / f"{stem}.{ext}"
                if ext == "mp4":
                    codec_args = ["-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF, "-threads", FFMPEG_THREADS, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
                else:
                    codec_args = ["-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF, "-threads", FFMPEG_THREADS, "-pix_fmt", "yuv420p", "-c:a", "aac"]
                cmd = ["ffmpeg", "-y", "-i", str(input_path), *codec_args, str(output_path)]
                label = f"🔄 Converting to {ext.upper()}"
                code, err = await run_process(cmd, status, label, duration, job_id)
                if code:
                    raise RuntimeError(err[-1200:] or "FFmpeg conversion failed")
            elif action == "quality":
                height = QUALITY[target]
                output_path = WORK / f"{stem}_{height}p.mp4"
                vf = f"scale=-2:min({height}\\,ih)"
                cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vf", vf,
                       "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
                       "-threads", FFMPEG_THREADS, "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)]
                code, err = await run_process(cmd, status, f"🎬 Creating {height}p", duration, job_id)
                if code:
                    raise RuntimeError(err[-1200:] or "FFmpeg quality conversion failed")
            elif action == "portrait":
                output_path = WORK / f"{stem}_portrait_9x16.mp4"
                vf = "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"
                cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vf", vf,
                       "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
                       "-threads", FFMPEG_THREADS, "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)]
                code, err = await run_process(cmd, status, "📱 Creating portrait 9:16", duration, job_id)
                if code:
                    raise RuntimeError(err[-1200:] or "FFmpeg portrait conversion failed")
            elif action == "allquality":
                for height in (360, 480, 720):
                    out = WORK / f"{stem}_{height}p.mp4"
                    vf = f"scale=-2:min({height}\\,ih)"
                    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vf", vf,
                           "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
                           "-threads", FFMPEG_THREADS, "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
                    code, err = await run_process(cmd, status, f"🎬 Creating {height}p", duration, job_id)
                    if code:
                        raise RuntimeError(err[-1200:] or f"{height}p conversion failed")
                    await upload_result(client, original.chat.id, out, original, status, str(thumb) if thumb else None)
                    try:
                        out.unlink()
                    except OSError:
                        pass
                await status.edit_text("✅ All quality versions sent.")
                return
            else:
                raise ValueError("Unknown action")
            await upload_result(client, original.chat.id, output_path, original, status, str(thumb) if thumb else None)
        await safe_edit(status, "✅ Done.")
    except asyncio.CancelledError:
        await safe_edit(status, "🛑 Job cancelled.")
    except Exception as e:
        log.exception("Media job failed")
        await safe_edit(status, f"❌ **Failed:** `{str(e)[:1200]}`")
    finally:
        ACTIVE.pop(job_id, None)
        for path in (input_path, output_path):
            try:
                if path and Path(path).exists():
                    Path(path).unlink()
            except OSError:
                pass


@app.on_callback_query(filters.regex(r"^(rename|convert|quality|allquality|portrait)\|"))
async def action_callback(client, query: CallbackQuery):
    try:
        parts = query.data.split("|")
        action = parts[0]
        if action == "rename":
            mid = int(parts[1])
            await query.answer()
            await query.message.reply_text(
                f"Reply to the original file with `/rename NewName.ext`.\n"
                f"Original message ID: `{mid}`"
            )
            return
        if action in ("convert", "quality"):
            target, mid = parts[1], int(parts[2])
        else:
            mid = int(parts[1])
            target = None
        original = await get_source_message(client, query.message.chat.id, mid)
        if not original or not find_media(original):
            return await query.answer("Original file was not found. Send it again.", show_alert=True)
        await query.answer("Starting…")
        task_action = {"convert": "convert", "quality": "quality", "allquality": "allquality", "portrait": "portrait"}[action]
        task = asyncio.create_task(process_file(client, original, task_action, target=target))
        JOBS.add(task)
        task.add_done_callback(JOBS.discard)
    except Exception as e:
        log.exception("Callback failed")
        await query.answer(f"Could not start: {str(e)[:150]}", show_alert=True)


@app.on_callback_query(filters.regex(r"^cancel\|"))
async def cancel_callback(_, query: CallbackQuery):
    try:
        _, uid, mid = query.data.split("|")
        key = (int(uid), int(mid))
        proc = ACTIVE.get(key)
        if proc and proc.returncode is None:
            proc.terminate()
            await query.answer("Cancelling…")
        else:
            await query.answer("No active FFmpeg job.", show_alert=True)
    except Exception:
        await query.answer("Could not cancel this job.", show_alert=True)


async def main():
    log.info("Starting Telegram Media Bot")
    await app.start()
    log.info("Bot started")
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()


if __name__ == "__main__":
    app.run()
