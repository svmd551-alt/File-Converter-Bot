"""
Fast multi-connection upload for Pyrogram 2.x
---------------------------------------------
Stock Pyrogram uploads a big file over ONE connection (4 workers), which Telegram throttles.
This opens several connections to the same data centre and sends the 512 KB parts in parallel.

 * Only used for files > 10 MB. Smaller files, and anything unexpected, use stock Pyrogram.
 * If the fast path fails for any reason it falls back to stock Pyrogram automatically.
 * Every part is retried; a part that cannot be sent makes the upload fail loudly
   (stock Pyrogram silently logs part errors, which can produce broken files).

Env vars:
  FAST_UPLOAD     1 (default) / 0 to switch off
  UPLOAD_CONNS    connections per upload (default 8)
  UPLOAD_WORKERS  parts in flight per connection (default 2)
"""
import asyncio
import contextlib
import inspect
import math
import os

PART = 512 * 1024                 # Telegram's maximum part size
BIG = 10 * 1024 * 1024            # below this stock Pyrogram is fine
CONNS = max(1, int(os.environ.get("UPLOAD_CONNS", "8")))
PER_CONN = max(1, int(os.environ.get("UPLOAD_WORKERS", "2")))
RETRIES = 5


async def _parallel(client, raw, Session, path, progress, progress_args):
    size = os.path.getsize(path)
    total = math.ceil(size / PART)
    fid = client.rnd_id()
    dc = await client.storage.dc_id()
    key = await client.storage.auth_key()
    test = await client.storage.test_mode()

    sessions = [Session(client, dc, key, test, is_media=True) for _ in range(max(1, min(CONNS, total)))]
    results = await asyncio.gather(*(s.start() for s in sessions), return_exceptions=True)
    started = []
    for s, r in zip(sessions, results):
        if isinstance(r, Exception):
            with contextlib.suppress(Exception):
                await s.stop()
        else:
            started.append(s)
    if not started:
        raise RuntimeError("could not open any upload connection")

    queue = asyncio.Queue(maxsize=len(started) * PER_CONN * 2)
    state = {"done": 0, "err": None}

    async def worker(session):
        while True:
            item = await queue.get()
            if item is None:
                return
            if state["err"]:
                continue                                  # keep draining so the producer never blocks
            part, chunk = item
            rpc = raw.functions.upload.SaveBigFilePart(
                file_id=fid, file_part=part, file_total_parts=total, bytes=chunk)
            sent = False
            for attempt in range(RETRIES):
                try:
                    await session.invoke(rpc)
                    sent = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if attempt == RETRIES - 1:
                        state["err"] = e
                    else:
                        await asyncio.sleep(1 + attempt)
            if not sent:
                continue
            state["done"] += len(chunk)
            if progress:
                try:
                    r = progress(min(state["done"], size), size, *progress_args)
                    if inspect.isawaitable(r):
                        await r
                except Exception:
                    pass

    workers = [asyncio.create_task(worker(s)) for s in started for _ in range(PER_CONN)]
    try:
        parts = 0
        with open(path, "rb") as fp:
            while True:
                if state["err"]:
                    raise state["err"]
                chunk = fp.read(PART)
                if not chunk:
                    break
                await queue.put((parts, chunk))
                parts += 1
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
        if state["err"]:
            raise state["err"]
        if parts != total or state["done"] != size:
            raise RuntimeError(f"upload incomplete ({parts}/{total} parts, {state['done']}/{size} bytes)")
        return raw.types.InputFileBig(id=fid, parts=total, name=os.path.basename(path))
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(*(s.stop() for s in started), return_exceptions=True)


def install(Client):
    """Replace Client.save_file with the parallel version (with automatic fallback)."""
    from pyrogram import raw
    from pyrogram.session import Session

    original = Client.save_file

    async def fast_save_file(self, path, file_id=None, file_part=0, progress=None, progress_args=()):
        if (file_id is not None or file_part or not isinstance(path, (str, os.PathLike))
                or not os.path.isfile(path) or os.path.getsize(path) <= BIG):
            return await original(self, path, file_id, file_part, progress, progress_args)

        me = getattr(self, "me", None)
        limit_mib = 4000 if getattr(me, "is_premium", False) else 2000
        if os.path.getsize(path) > limit_mib * 1024 * 1024:
            raise ValueError(f"Maximum file size is {limit_mib} MiB")

        sem = getattr(self, "save_file_semaphore", None) or contextlib.nullcontext()
        try:
            async with sem:
                return await _parallel(self, raw, Session, path, progress, progress_args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[fast_upload] failed ({e!r}); using stock Pyrogram upload", flush=True)
            return await original(self, path, file_id, file_part, progress, progress_args)

    Client.save_file = fast_save_file
