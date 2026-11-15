"""API for the Dailies Chrome extension.

The extension runs in your own Chrome, where you're already signed in to YouTube (like Internet
Download Manager). It sends Dailies your youtube.com session cookies whenever Chrome refreshes them,
so the server never needs its own sign-in, and it queues the video you're watching with a click.

Requests carry `Authorization: Bearer <key>`. The key is created on the YouTube page; only its hash
is stored. google.com cookies are never accepted: they would unlock the whole Google account.
"""
import hashlib
import hmac
import io
import re
import secrets
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import __version__, browser, config, db, folders, worker, ytdl

router = APIRouter()
EXT_DIR = Path(__file__).resolve().parent.parent / "extension"
PREFIX = "/api/ext/"
MAX_COOKIES = 300

VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")


# ---- key ---------------------------------------------------------------------

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    key = "dly_" + secrets.token_urlsafe(32)
    db.set_setting("ext_key_hash", _hash(key))
    db.set_setting("ext_key_created", time.strftime("%Y-%m-%d %H:%M"))
    return key


def revoke():
    db.run("DELETE FROM settings WHERE key IN ('ext_key_hash', 'ext_key_created')")


def has_key() -> bool:
    return bool(db.setting("ext_key_hash"))


def key_ok(headers) -> bool:
    auth = headers.get("authorization", "")
    want = db.setting("ext_key_hash")
    if not want or not auth.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(_hash(auth[7:].strip()), want)


# ---- cookies -----------------------------------------------------------------

def _clean(value, limit=4096) -> str:
    return str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")[:limit]


def write_cookies(cookies: list[dict]) -> int:
    """Chrome extension cookie objects -> Netscape cookies.txt with youtube.com cookies only."""
    lines = ["# Netscape HTTP Cookie File", "# Sent by the Dailies Chrome extension"]
    n = 0
    for c in cookies[:MAX_COOKIES]:
        domain = _clean(c.get("domain"), 255)
        if not re.fullmatch(r"\.?([\w-]+\.)*youtube\.com", domain) or not c.get("name"):
            continue
        host_only = bool(c.get("hostOnly"))
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        expires = int(c.get("expirationDate") or 0)
        lines.append("\t".join([
            ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
            "FALSE" if host_only else "TRUE",
            _clean(c.get("path"), 1024) or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(expires),
            _clean(c.get("name"), 256),
            _clean(c.get("value")),
        ]))
        n += 1
    tmp = config.COOKIES_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o600)
    tmp.replace(config.COOKIES_FILE)
    return n


def sync_state() -> dict:
    st = browser.status()
    return {"signed_in": st["signed_in"] and st["mode"] == "file", "mode": st["mode"],
            "synced_at": db.setting("ext_synced_at") or None, "detail": st["detail"]}


@router.post(PREFIX + "cookies")
async def cookies(request: Request):
    body = await request.json()
    items = body.get("cookies") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return JSONResponse({"error": "expected {cookies: [...]}"}, status_code=400)
    items = [c for c in items if isinstance(c, dict)]
    names = {c.get("name") for c in items if str(c.get("domain", "")).lstrip(".").endswith("youtube.com")}
    if not names & browser.AUTH_COOKIES:
        # signed out in Chrome: keep whatever Dailies had rather than replacing it with a logged-out set
        return {"ok": False, "message": "Chrome isn't signed in to YouTube. Sign in on youtube.com first.",
                **sync_state()}
    first = db.setting("auth_mode") != "file" or not db.setting("ext_synced_at")
    n = write_cookies(items)
    db.set_setting("auth_mode", "file")
    db.set_setting("ext_synced_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    if first:
        db.log("info", f"YouTube sign-in now comes from the Chrome extension ({n} cookies)")
    from .main import resume_if_login_paused
    resume_if_login_paused()
    return {"ok": True, "count": n, **sync_state()}


# ---- info + download ---------------------------------------------------------

@router.get(PREFIX + "info")
def info():
    return {
        "version": __version__,
        "profiles": [{"id": p["id"], "name": p["name"]} for p in db.q("SELECT id, name FROM profiles ORDER BY id")],
        "folders": [{"id": f["id"], "name": f["name"]} for f in folders.all_folders()],
        "youtube": sync_state(),
    }


def _is_youtube(url: str) -> bool:
    return bool(re.match(r"https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", url))


@router.post(PREFIX + "download")
async def download(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)
    url = str(body.get("url", "")).strip()
    if not _is_youtube(url):
        return JSONResponse({"ok": False, "message": "That isn't a YouTube link."}, status_code=400)
    pid = body.get("profile_id")
    profile = db.one("SELECT * FROM profiles WHERE id=?", (pid,)) if pid else None
    profile = profile or db.one("SELECT * FROM profiles ORDER BY id")
    fid = body.get("folder_id") or None
    if fid and not db.one("SELECT 1 FROM folders WHERE id=?", (fid,)):
        fid = None

    kind = ytdl.detect_kind(url)
    if kind == "video" and (m := VIDEO_ID.search(url)):
        vid = m.group(1)
        url = f"https://www.youtube.com/watch?v={vid}"  # drop &list=, &t= etc.
        old = db.one("SELECT m.*, s.profile_id, s.folder_id FROM media m JOIN sources s ON s.id=m.source_id "
                     "WHERE m.video_id=? ORDER BY m.id DESC", (vid,))
        if old and (old["profile_id"], old["folder_id"]) == (profile["id"], fid):
            if old["status"] == "downloaded":
                return {"ok": True, "message": f"Already downloaded: {old['title']}", "status": "downloaded"}
            if old["status"] in ("queued", "downloading"):
                return {"ok": True, "message": f"Already in the queue: {old['title']}", "status": old["status"]}
            top = db.one("SELECT COALESCE(MAX(priority), 0) p FROM media WHERE status='queued'")["p"]
            db.run("UPDATE media SET status='queued', error=NULL, priority=? WHERE id=?", (top + 1, old["id"]))
            worker.kick()
            return {"ok": True, "message": f"Queued again: {old['title']}{_paused_note()}", "status": "queued"}
    title = str(body.get("title") or "").strip()[:300]
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id, folder_id, auto_name) VALUES (?, ?, ?, ?, ?, ?)",
                 (title or url.rsplit("/", 1)[-1], url, kind, profile["id"], fid, 0 if title else 1))
    db.log("info", f"Chrome extension added {title or url} ({profile['name']})")
    worker.index_now(sid)
    what = {"video": "Queued", "playlist": "Added playlist", "channel": "Added channel"}.get(kind, "Added")
    return {"ok": True, "status": "queued", "source_id": sid,
            "message": f"{what}: {title or url}" + ("" if kind == "video" else ". New uploads download on its schedule.")
            + _paused_note()}


def _paused_note() -> str:
    return " Downloads are stopped; start them on the Today page." if worker.state["paused"] else ""


# ---- extension download (load unpacked) --------------------------------------

def extension_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(EXT_DIR.rglob("*")):
            if f.is_file():
                z.write(f, "dailies-extension/" + f.relative_to(EXT_DIR).as_posix())
    return buf.getvalue()


@router.get("/extension/dailies-extension.zip")
def extension_download():
    return Response(extension_zip(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="dailies-extension.zip"'})
