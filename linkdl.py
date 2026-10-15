"""
Direct-link downloader (plain http/https file URLs)
---------------------------------------------------
 * Only normal direct file links. Web pages (share pages, players...) are rejected.
 * Safe: refuses private / local network addresses (also on redirects), so the bot cannot be
   used to poke at the server's own network.
 * Fast: if the server supports HTTP Range, the file is fetched in parts over several
   connections; every part resumes by itself after a network drop.
 * Cancellable, size-capped, blocking code meant to run in a thread (asyncio.to_thread).

Env vars:
  LINK_CONNS          parallel connections per file (default 6)
  LINK_ALLOW_PRIVATE  1 = allow private/local addresses (only for testing / intranet)
"""
import concurrent.futures as cf
import ipaddress
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
CONNS = max(1, int(os.environ.get("LINK_CONNS", "6")))
ALLOW_PRIVATE = os.environ.get("LINK_ALLOW_PRIVATE") == "1"
PART = 16 * 1024 * 1024          # size of one ranged part
MIN_PARALLEL = 8 * 1024 * 1024   # smaller files use one connection
READ = 256 * 1024
RETRIES = 6
TIMEOUT = 30


class LinkError(Exception):
    """Problem with the link itself (message is safe to show to the user)."""


class Cancelled(Exception):
    pass


# ───────────────────────── safety ─────────────────────────
def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise LinkError("This is not a valid http/https link.")
    if p.username or p.password:
        raise LinkError("Links that contain a username/password are not allowed.")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise LinkError("I could not find that website.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            raise LinkError("This address is not allowed.")


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))     # every hop is checked
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Redirect())


def _open(url, headers=None):
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    return _opener.open(req, timeout=TIMEOUT)


# ───────────────────────── probe ─────────────────────────
def _filename(headers, url):
    cd = headers.get("Content-Disposition", "") or ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        name = m.group(1).strip() if m else ""
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))
    name = name.strip() or "file"
    if not os.path.splitext(name)[1]:
        ext = mimetypes.guess_extension((headers.get_content_type() or "").split(";")[0]) or ""
        name += {".mkv": ".mkv"}.get(ext, ext)
    return name


def probe(url):
    """Ask the server about the file. Returns dict(url, size, ranges, name, ctype)."""
    try:
        r = _open(url, {"Range": "bytes=0-0"})
    except urllib.error.HTTPError as e:
        raise LinkError(f"The website answered with error {e.code}.")
    except LinkError:
        raise
    except Exception as e:
        raise LinkError(f"Could not open the link ({type(e).__name__}).")
    try:
        h = r.headers
        ctype = (h.get_content_type() or "").lower()
        if ctype in ("text/html", "application/xhtml+xml"):
            raise LinkError("This link opens a web page, not a file. I need a DIRECT file link "
                            "(one that starts the download by itself).")
        size, ranges = None, False
        cr = h.get("Content-Range") or ""
        if r.status == 206 and "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
            size, ranges = int(cr.rsplit("/", 1)[1]), True
        elif (h.get("Content-Length") or "").isdigit():
            size = int(h["Content-Length"])
        return {"url": r.geturl(), "size": size, "ranges": ranges,
                "name": _filename(h, r.geturl()), "ctype": ctype}
    finally:
        r.close()


# ───────────────────────── download ─────────────────────────
def _fetch_part(url, path, start, end, state, lock, cancel):
    pos, attempt = start, 0
    with open(path, "r+b") as fh:
        while pos <= end:
            if cancel.is_set():
                raise Cancelled()
            before = pos
            try:
                r = _open(url, {"Range": f"bytes={pos}-{end}"})
                try:
                    if r.status != 206:
                        raise LinkError("The server does not support partial downloads.")
                    fh.seek(pos)
                    while pos <= end:
                        if cancel.is_set():
                            raise Cancelled()
                        buf = r.read(min(READ, end - pos + 1))
                        if not buf:
                            break
                        fh.write(buf)
                        pos += len(buf)
                        with lock:
                            state["done"] += len(buf)
                finally:
                    r.close()
                if pos <= end:
                    raise IOError("connection closed early")
            except (Cancelled, LinkError):
                raise
            except Exception:
                attempt = 0 if pos > before else attempt + 1
                if attempt >= RETRIES:
                    raise
                time.sleep(min(2 * (attempt + 1), 10))


def _single(info, path, max_bytes, state, lock, cancel):
    attempt = 0
    while True:
        if cancel.is_set():
            raise Cancelled()
        with lock:
            state["done"] = 0
        try:
            r = _open(info["url"])
            try:
                with open(path, "wb") as fh:
                    total = 0
                    while True:
                        if cancel.is_set():
                            raise Cancelled()
                        buf = r.read(READ)
                        if not buf:
                            break
                        total += len(buf)
                        if total > max_bytes:
                            raise LinkError("The file is bigger than the allowed size.")
                        fh.write(buf)
                        with lock:
                            state["done"] = total
            finally:
                r.close()
            if info["size"] and total != info["size"]:
                raise IOError("incomplete download")
            return
        except (Cancelled, LinkError):
            raise
        except Exception:
            attempt += 1
            if attempt >= 3:
                raise
            time.sleep(2 * attempt)


def download(info, path, max_bytes, state, cancel):
    """Blocking. state = {"done": 0, "total": n} is updated while running. cancel = threading.Event."""
    size = info["size"]
    state["total"] = size or 0
    lock = threading.Lock()
    if size and size > max_bytes:
        raise LinkError("The file is bigger than the allowed size.")
    if not (info["ranges"] and size and size >= MIN_PARALLEL):
        return _single(info, path, max_bytes, state, lock, cancel)

    with open(path, "wb") as fh:
        fh.truncate(size)
    parts = [(s, min(s + PART, size) - 1) for s in range(0, size, PART)]
    stop = threading.Event()

    class _Either:                                   # cancelled by the user OR another part failed
        def is_set(self):
            return cancel.is_set() or stop.is_set()

    either = _Either()
    with cf.ThreadPoolExecutor(max_workers=min(CONNS, len(parts))) as ex:
        futs = [ex.submit(_fetch_part, info["url"], path, a, b, state, lock, either) for a, b in parts]
        err = None
        for f in cf.as_completed(futs):
            try:
                f.result()
            except Exception as e:
                if err is None or (isinstance(err, Cancelled) and not isinstance(e, Cancelled)):
                    err = e
                stop.set()
        if err:
            if cancel.is_set():
                raise Cancelled()
            raise err
    if os.path.getsize(path) != size:
        raise IOError("incomplete download")
