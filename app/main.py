"""Dailies - self-hosted YouTube archiver with in-browser YouTube sign-in."""
import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, browser, config, db, ext, folders, worker, ytdl

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["version"] = __version__
templates.env.globals["host_path"] = folders.host_path


@asynccontextmanager
async def lifespan(app):
    db.init()
    if config.SCHEDULER:
        worker.start()
    yield
    worker.stop()
    browser.stop()


app = FastAPI(title="Dailies", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(ext.router)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
if Path(browser.novnc_dir()).is_dir():
    app.mount("/novnc", StaticFiles(directory=browser.novnc_dir(), html=True), name="novnc")


# ---- optional password -------------------------------------------------------

def _token() -> str:
    return hmac.new(config.secret_key(), b"dailies:" + config.PASSWORD.encode(), hashlib.sha256).hexdigest()


def authed(cookies) -> bool:
    return not config.PASSWORD or hmac.compare_digest(cookies.get("dailies", ""), _token())


def same_origin(headers) -> bool:
    """Reject requests a different website makes on your behalf (CSRF / cross-site WebSocket).
    Browsers always send Origin on cross-site POSTs and WebSockets; tools like curl send none."""
    origin = headers.get("origin") or headers.get("referer")
    if not origin:
        return True  # not sent by a browser page (curl, scripts); "null" origins fall through and fail
    from urllib.parse import urlsplit
    host = urlsplit(origin).netloc.lower()
    allowed = {h.strip().lower() for h in (headers.get("x-forwarded-host", ""), headers.get("host", "")) if h}
    return host in allowed


SECURITY_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",            # no clickjacking; the noVNC iframe is same-origin
    "Content-Security-Policy": "frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(ext.PREFIX):  # Chrome extension: its own key, sent from a chrome-extension:// origin
        ip = request.client.host if request.client else "?"
        if login_locked(ip):
            resp = JSONResponse({"error": "too many wrong keys, try again in 10 minutes"}, status_code=429)
        elif ext.key_ok(request.headers):
            resp = await call_next(request)
        else:
            import time
            _failures.setdefault(ip, []).append(time.time())
            resp = JSONResponse({"error": "wrong or missing extension key"}, status_code=401)
        resp.headers.update(SECURITY_HEADERS)
        return resp
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request.headers):
        db.log("warn", f"Blocked a {request.method} to {path} from another website ({request.headers.get('origin')})")
        resp = JSONResponse({"error": "cross-site request blocked"}, status_code=403)
    elif path.startswith(("/static", "/login", "/api/health")) or authed(request.cookies):
        resp = await call_next(request)
    elif path.startswith("/api"):
        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
    else:
        resp = RedirectResponse("/login", status_code=303)
    resp.headers.update(SECURITY_HEADERS)
    return resp


# login throttle: 5 wrong passwords per address -> locked for 10 minutes
_failures: dict[str, list[float]] = {}
LOCK_AFTER, LOCK_SECONDS = 5, 600


def login_locked(ip: str) -> bool:
    import time
    now = time.time()
    _failures[ip] = [t for t in _failures.get(ip, []) if now - t < LOCK_SECONDS]
    return len(_failures[ip]) >= LOCK_AFTER


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error, "no_password": False})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    import time
    ip = request.client.host if request.client else "?"
    if login_locked(ip):
        return RedirectResponse("/login?error=locked", status_code=303)
    if not config.PASSWORD or not hmac.compare_digest(password.encode(), config.PASSWORD.encode()):
        _failures.setdefault(ip, []).append(time.time())
        db.log("warn", f"Wrong web UI password from {ip}")
        return RedirectResponse("/login?error=1", status_code=303)
    _failures.pop(ip, None)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("dailies", _token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("dailies")
    return resp


# ---- helpers -----------------------------------------------------------------

def page(request, name, **ctx):
    ctx.setdefault("auth", browser.status())
    ctx["no_password"] = not config.PASSWORD
    ctx["nav"] = name.split(".")[0]
    return templates.TemplateResponse(request, name, ctx)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    """Say what broke instead of a bare "Internal Server Error", and keep it in the activity log."""
    import html
    import traceback
    msg = f"{type(exc).__name__}: {exc}"
    try:
        db.log("error", f"{request.method} {request.url.path} failed: {msg}"[:2000])
    except Exception:
        pass
    traceback.print_exception(exc)
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/static/style.css"><title>Dailies error</title>'
        f'<main><h1>Something went wrong</h1><p class="lede">{html.escape(request.url.path)} failed '
        f'with:</p><pre class="banner warn">{html.escape(msg)}</pre><p><a href="/">Back to Today</a></p></main>',
        status_code=500)


def back(url="/"):
    return RedirectResponse(url, status_code=303)


def yt(msg: str, ok=None):
    from urllib.parse import urlencode
    params = {"msg": msg}
    if ok is not None:
        params["ok"] = int(bool(ok))
    return back("/youtube?" + urlencode(params))


def resume_if_login_paused():
    if worker.state["pause_reason"] and browser.status()["signed_in"]:
        db.run("UPDATE media SET status='queued', error=NULL WHERE status='failed' AND error LIKE '%sign in%'")
        worker.set_paused(False)


def media_where(status="", q="", source_id=None) -> tuple[str, list]:
    """Filter shared by the media lists and "Delete all shown". No status = everything except deleted."""
    sql, args = " WHERE 1=1", []
    if source_id is not None:
        sql += " AND m.source_id=?"
        args.append(source_id)
    if status:
        sql += " AND m.status=?"
        args.append(status)
    else:
        sql += " AND m.status!='deleted'"
    if q:
        sql += " AND m.title LIKE ?"
        args.append(f"%{q}%")
    return sql, args


def delete_media(rows) -> int:
    """Delete files + mark deleted (the record stays so the next check doesn't download it again)."""
    n = 0
    for m in rows:
        if m["status"] in ("downloading", "deleted"):
            continue
        ytdl.delete_files(m)
        db.run("UPDATE media SET status='deleted', filepath=NULL, filesize=NULL, refresh=0 WHERE id=?", (m["id"],))
        n += 1
    return n


def deletable(where: str, args: list) -> int:
    return db.one("SELECT COUNT(*) n FROM media m" + where + " AND m.status NOT IN ('downloading','deleted')", args)["n"]


def counts(source_id=None):
    sql = "SELECT status, COUNT(*) n FROM media"
    args = ()
    if source_id is not None:
        sql += " WHERE source_id=?"
        args = (source_id,)
    return {r["status"]: r["n"] for r in db.q(sql + " GROUP BY status", args)}


SOURCE_FIELDS = {
    "name": str, "url": str, "kind": str, "profile_id": int, "interval_hours": float, "max_items": int,
    "download_after": str, "title_filter": str, "title_exclude": str, "min_duration": int, "keep_days": int,
    "folder_id": int,  # 0/blank = automatic (default folder for the profile)
}
SOURCE_FLAGS = ("enabled", "auto_download", "include_shorts", "include_live")
PROFILE_FIELDS = {"name": str, "quality": str, "container": str, "output": str, "subtitles": str,
                  "sponsorblock": str}
PROFILE_FLAGS = ("prefer_h264", "embed_thumbnail", "write_thumbnail", "write_nfo", "embed_metadata",
                 "embed_chapters", "auto_subs")


async def form_values(request, fields, flags):
    form = await request.form()
    vals = {}
    for k, cast in fields.items():
        if k in form:
            raw = str(form[k]).strip()
            try:
                vals[k] = cast(raw) if raw or cast is str else 0
            except ValueError:
                vals[k] = 0
    for k in flags:
        vals[k] = 1 if form.get(k) else 0
    if "folder_id" in vals and not vals["folder_id"]:
        vals["folder_id"] = None
    return vals


# ---- pages -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return page(
        request, "dashboard.html",
        counts=counts(),
        sources=db.q("SELECT COUNT(*) n FROM sources")[0]["n"],
        queue=db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id "
                   "WHERE m.status IN ('queued','downloading','stopped') "
                   "ORDER BY m.status='downloading' DESC, m.status='stopped', m.priority DESC, "
                   "m.upload_date IS NULL, m.upload_date DESC LIMIT 15"),
        stopped=db.one("SELECT COUNT(*) n FROM media WHERE status='stopped'")["n"],
        recent=db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id "
                    "WHERE m.status='downloaded' ORDER BY m.downloaded DESC LIMIT 10"),
        failed=db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id "
                    "WHERE m.status='failed' ORDER BY m.id DESC LIMIT 5"),
        paused=worker.state["paused"], pause_reason=worker.state["pause_reason"],
        refreshing=db.one("SELECT COUNT(*) n FROM media WHERE refresh=1 AND status='downloaded'")["n"],
    )


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    rows = db.q("""SELECT s.*, p.name profile,
        SUM(m.status='downloaded') done, SUM(m.status IN ('queued','downloading')) queued,
        SUM(m.status='failed') failed, COUNT(m.id) total
        FROM sources s JOIN profiles p ON p.id=s.profile_id LEFT JOIN media m ON m.source_id=s.id
        GROUP BY s.id ORDER BY s.name COLLATE NOCASE""")
    return page(request, "sources.html", sources=rows, profiles=db.q("SELECT * FROM profiles ORDER BY id"),
                folders=folders.all_folders(), folder_names={f["id"]: f["name"] for f in folders.all_folders()},
                feeds=ytdl.FEEDS, indexing=worker.state["indexing"], auth=browser.status())


@app.post("/sources")
async def add_source(request: Request):
    vals = await form_values(request, SOURCE_FIELDS, ())
    url = vals.get("url", "")
    if not url:
        return back("/sources")
    vals["kind"] = vals.get("kind") or ytdl.detect_kind(url)
    if not vals.get("name"):  # placeholder until the first index fetches the real title
        vals["name"] = url.rstrip("/").rsplit("/", 1)[-1].lstrip("@") or "Source"
        vals["auto_name"] = 1
    vals.setdefault("profile_id", db.one("SELECT id FROM profiles ORDER BY id")["id"])
    cols = ", ".join(vals)
    sid = db.run(f"INSERT INTO sources({cols}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
    worker.index_now(sid)
    return back(f"/sources/{sid}")


@app.post("/sources/feed/{key}")
def add_feed(key: str):
    name, url = ytdl.FEEDS[key]
    pid = db.one("SELECT id FROM profiles ORDER BY id")["id"]
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES (?, ?, 'feed', ?)", (name, url, pid))
    worker.index_now(sid)
    return back(f"/sources/{sid}")


@app.get("/sources/{sid}", response_class=HTMLResponse)
def source_detail(request: Request, sid: int, status: str = ""):
    s = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    if s is None:
        return back("/sources")
    where, args = media_where(status, source_id=sid)
    media = db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id" + where +
                 " ORDER BY m.upload_date IS NULL, m.upload_date DESC, m.id DESC LIMIT 500", args)
    return page(request, "source.html", s=s, media=media, counts=counts(sid), status=status, total=deletable(where, args),
                profiles=db.q("SELECT * FROM profiles ORDER BY id"), indexing=worker.state["indexing"] == sid,
                folders=folders.all_folders(),
                dest=folders.for_source(s, db.one("SELECT * FROM profiles WHERE id=?", (s["profile_id"],))),
                urls=ytdl.source_urls(s))


@app.post("/sources/{sid}/edit")
async def edit_source(request: Request, sid: int):
    vals = await form_values(request, SOURCE_FIELDS, SOURCE_FLAGS)
    sets = ", ".join(f"{k}=?" for k in vals)
    db.run(f"UPDATE sources SET {sets} WHERE id=?", (*vals.values(), sid))
    return back(f"/sources/{sid}")


@app.post("/sources/{sid}/index")
def index_source(sid: int):
    worker.index_now(sid)
    return back(f"/sources/{sid}")


@app.post("/sources/{sid}/queue-all")
def queue_all(sid: int):
    db.run("UPDATE media SET status='queued' WHERE source_id=? AND status IN ('pending','failed')", (sid,))
    worker.kick()
    return back(f"/sources/{sid}")


@app.post("/sources/{sid}/refresh")
def refresh_source(sid: int):
    db.run("UPDATE media SET refresh=1 WHERE source_id=? AND status='downloaded'", (sid,))
    worker.kick()
    return back(f"/sources/{sid}")


@app.post("/media/refresh-all")
def refresh_all():
    db.run("UPDATE media SET refresh=1 WHERE status='downloaded'")
    worker.kick()
    return back("/media")


def remove_sources(ids, files: bool) -> int:
    """Delete sources (and their video records); with files=True also their downloaded files."""
    n = 0
    for sid in ids:
        if not db.one("SELECT 1 FROM sources WHERE id=?", (sid,)):
            continue
        for m in db.q("SELECT * FROM media WHERE source_id=?", (sid,)):
            if m["status"] == "downloading":
                ytdl.cancel[m["id"]] = "stopped"
            if files and m["filepath"]:
                ytdl.delete_files(m)
        db.run("DELETE FROM sources WHERE id=?", (sid,))
        n += 1
    return n


@app.post("/sources/bulk-delete")
async def sources_bulk_delete(request: Request):
    """Delete the ticked sources, or (scope=all) every source."""
    form = await request.form()
    if form.get("scope") == "all":
        ids = [r["id"] for r in db.q("SELECT id FROM sources")]
    else:
        ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    files = bool(form.get("files"))
    if n := remove_sources(ids, files):
        db.log("info", f"Deleted {n} source(s)" + (" and their downloaded files" if files else ""))
    return back("/sources")


@app.post("/sources/{sid}/delete")
async def delete_source(request: Request, sid: int):
    form = await request.form()
    name = db.one("SELECT name FROM sources WHERE id=?", (sid,))
    if remove_sources([sid], bool(form.get("files"))) and name:
        db.log("info", f"Deleted source {name['name']}" + (" and its downloaded files" if form.get("files") else ""))
    return back("/sources")


@app.get("/media", response_class=HTMLResponse)
def media(request: Request, status: str = "", q: str = ""):
    where, args = media_where(status, q)
    rows = db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id" + where +
                " ORDER BY COALESCE(m.downloaded, m.found) DESC LIMIT 500", args)
    return page(request, "media.html", media=rows, status=status, q=q, counts=counts(), total=deletable(where, args))


@app.post("/media/{mid}/{action}")
async def media_action(request: Request, mid: int, action: str):
    m = db.one("SELECT * FROM media WHERE id=?", (mid,))
    if m is None:
        return back("/media")
    if action in ("queue", "retry"):
        db.run("UPDATE media SET status='queued', error=NULL WHERE id=?", (mid,))
        worker.kick()
    elif action in ("start", "resume"):  # front of the queue, and make sure the queue is running
        top = db.one("SELECT COALESCE(MAX(priority), 0) p FROM media WHERE status='queued'")["p"]
        db.run("UPDATE media SET status='queued', error=NULL, priority=? WHERE id=?", (top + 1, mid))
        worker.set_paused(False)
    elif action == "stop":
        if m["status"] == "downloading":
            ytdl.cancel[mid] = "stopped"
        elif m["status"] == "queued":
            db.run("UPDATE media SET status='stopped', priority=0 WHERE id=?", (mid,))
    elif action == "skip":
        db.run("UPDATE media SET status='skipped' WHERE id=?", (mid,))
    elif action == "delete":
        if delete_media([m]):
            db.log("info", f"Deleted {m['title']}")
    return back(request.headers.get("referer") or "/media")


@app.post("/media/bulk-delete")
async def media_bulk_delete(request: Request):
    """Delete the ticked videos, or (scope=all) every video the current filter shows."""
    form = await request.form()
    if form.get("scope") == "all":
        sid = form.get("source_id")
        where, args = media_where(form.get("status", ""), form.get("q", ""), int(sid) if sid else None)
        rows = db.q("SELECT m.* FROM media m" + where, args)
    else:
        ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
        rows = db.q(f"SELECT * FROM media WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else []
    n = delete_media(rows)
    if n:
        db.log("info", f"Deleted {n} video(s) and their files")
    return back(request.headers.get("referer") or "/media")


@app.post("/media/retry-failed")
def retry_failed():
    db.run("UPDATE media SET status='queued', error=NULL WHERE status='failed'")
    worker.kick()
    return back("/")


@app.post("/queue/{action}")
def queue_control(request: Request, action: str):
    if action in ("stop", "pause"):
        worker.stop_downloads()
    elif action == "resume-stopped":
        db.run("UPDATE media SET status='queued' WHERE status='stopped'")
        worker.set_paused(False)
    else:  # start / resume
        worker.set_paused(False)
    return back(request.headers.get("referer") or "/")


@app.get("/profiles", response_class=HTMLResponse)
def profiles(request: Request):
    used = {r["profile_id"]: r["n"] for r in db.q("SELECT profile_id, COUNT(*) n FROM sources GROUP BY profile_id")}
    return page(request, "profiles.html", profiles=db.q("SELECT * FROM profiles ORDER BY id"), used=used,
                qualities=list(ytdl.QUALITY_FORMATS), video_containers=ytdl.VIDEO_CONTAINERS,
                audio_codecs=ytdl.AUDIO_CODECS)


@app.post("/profiles")
async def save_profile(request: Request):
    form = await request.form()
    vals = await form_values(request, PROFILE_FIELDS, PROFILE_FLAGS)
    pid = form.get("id")
    vals["output"] = vals.get("output", "")  # blank = default layout
    if vals.get("quality") not in ytdl.QUALITY_FORMATS:
        vals["quality"] = "1080"
    if vals["quality"] == "audio" and vals.get("container") not in ytdl.AUDIO_CODECS:
        vals["container"] = "mp3"
    elif vals["quality"] != "audio" and vals.get("container") not in ytdl.VIDEO_CONTAINERS:
        vals["container"] = "mp4"
    if pid:
        db.run(f"UPDATE profiles SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), int(pid)))
    else:
        db.run(f"INSERT INTO profiles({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
    return back("/profiles")


@app.post("/profiles/{pid}/delete")
def delete_profile(pid: int):
    if not db.one("SELECT 1 FROM sources WHERE profile_id=?", (pid,)):
        db.run("DELETE FROM profiles WHERE id=?", (pid,))
    return back("/profiles")


# ---- YouTube sign-in -----------------------------------------------------------

@app.get("/youtube", response_class=HTMLResponse)
def youtube(request: Request, msg: str = "", ok: str = "", new_key: str = ""):
    return page(request, "youtube.html", msg=msg, ok=ok, has_file=config.COOKIES_FILE.exists(),
                diag=browser.diagnostics(), new_key=new_key, ext_key=ext.has_key(),
                ext_created=db.setting("ext_key_created"), ext_synced=db.setting("ext_synced_at"),
                server=str(request.base_url).rstrip("/"))


NEEDS_PASSWORD = ("Set a web UI password first (Unraid: edit the Dailies container, Web UI password). "
                  "Without one, anyone on your network could use your signed-in Google account.")


@app.post("/youtube/extension/key", response_class=HTMLResponse)
def extension_key(request: Request):
    if not config.PASSWORD:
        return yt(NEEDS_PASSWORD, False)
    key = ext.new_key()  # shown once on this response, never put in a URL
    db.log("info", "New Chrome extension key created (any older key stops working)")
    return youtube(request, new_key=key)


@app.post("/youtube/extension/revoke")
def extension_revoke():
    ext.revoke()
    db.log("info", "Chrome extension key revoked")
    return yt("Extension key revoked. The extension can't reach Dailies until you create a new key.", True)


@app.post("/youtube/browser/start")
def browser_start():
    if not config.PASSWORD:
        return yt(NEEDS_PASSWORD, False)
    try:
        browser.start()
    except RuntimeError as exc:
        return yt(f"Couldn't start the sign-in browser: {exc}"[:1500], False)
    db.set_setting("auth_mode", "browser")
    return back("/youtube")


@app.post("/youtube/browser/stop")
def browser_stop():
    browser.stop()
    s = browser.status()
    resume_if_login_paused()
    return yt(s["detail"], s["signed_in"])


@app.post("/youtube/signout")
def signout():
    browser.wipe_profile()
    config.COOKIES_FILE.unlink(missing_ok=True)
    return yt("Signed out - browser profile and cookies.txt deleted.")


@app.post("/youtube/mode")
def set_mode(mode: str = Form(...)):
    if mode in ("browser", "file", "none"):
        db.set_setting("auth_mode", mode)
    return back("/youtube")


@app.post("/youtube/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    data = await file.read()
    text = data.decode("utf-8", "replace")
    if "youtube.com" not in text or "\t" not in text:
        return yt("That doesn't look like a Netscape cookies.txt with YouTube cookies.", False)
    config.COOKIES_FILE.write_bytes(data)
    config.COOKIES_FILE.chmod(0o600)
    db.set_setting("auth_mode", "file")
    db.log("info", "cookies.txt uploaded")
    resume_if_login_paused()
    return yt("cookies.txt saved.", True)


@app.get("/youtube/cookies.txt")
def export_cookies():
    if not config.PASSWORD:
        return yt(NEEDS_PASSWORD, False)
    from starlette.background import BackgroundTask
    dest = config.CONFIG_DIR / "exported-cookies.txt"
    try:
        browser.export_cookies_txt(dest)
    except Exception as exc:
        return yt(f"Export failed: {exc}", False)
    db.log("info", "Exported YouTube cookies.txt")
    return FileResponse(dest, filename="youtube-cookies.txt", media_type="text/plain",
                        background=BackgroundTask(dest.unlink, missing_ok=True))  # don't leave a copy behind


@app.post("/youtube/verify")
async def verify():
    ok, detail = await asyncio.to_thread(ytdl.verify_login)
    return yt(detail, ok)


@app.websocket("/vnc/ws")
async def vnc_proxy(ws: WebSocket):
    """Bridge noVNC (WebSocket) to x11vnc (TCP, localhost only)."""
    if not authed(ws.cookies) or not same_origin(ws.headers) or not config.PASSWORD or not browser.running():
        await ws.close(code=1008)
        return
    proto = ws.headers.get("sec-websocket-protocol", "")
    await ws.accept(subprotocol="binary" if "binary" in proto else None)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", config.VNC_PORT)
    except OSError:
        await ws.close(code=1011)
        return

    async def up():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") or (msg.get("text") or "").encode()
                writer.write(data)
                await writer.drain()
        except (WebSocketDisconnect, ConnectionError):
            pass

    async def down():
        try:
            while data := await reader.read(65536):
                await ws.send_bytes(data)
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            pass

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    writer.close()
    try:
        await ws.close()
    except RuntimeError:
        pass


# ---- settings / log / api --------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, msg: str = ""):
    import yt_dlp.version
    vals = {k: db.setting(k) for k in db.DEFAULT_SETTINGS}
    used = {r["folder_id"]: r["n"] for r in db.q("SELECT folder_id, COUNT(*) n FROM sources GROUP BY folder_id")}
    rows = [dict(f, check=folders.check(f["path"]), sources=used.get(f["id"], 0), host=folders.host_path(f["path"]))
            for f in folders.all_folders()]
    locations = folders.locations()
    return page(request, "settings.html", s=vals, ytdlp=yt_dlp.version.__version__,
                downloads=config.DOWNLOADS_DIR, folders=rows, layouts=folders.LAYOUTS, msg=msg,
                locations=locations, hosts={v: folders.host_path(v) for v in locations},
                default_folder=db.setting("default_folder"), default_audio_folder=db.setting("default_audio_folder"),
                log=db.q("SELECT * FROM log ORDER BY id DESC LIMIT 200"))


def folder_values(form) -> tuple[dict, str]:
    path = str(form.get("path", ""))
    if path in ("", "__custom"):
        path = str(form.get("path_custom", ""))
    if sub := str(form.get("subfolder", "")).strip().strip("/"):
        path = path.rstrip("/") + "/" + sub  # "new folder inside the chosen location"
    vals = {"name": str(form.get("name", "")).strip(), "path": path.strip().rstrip("/") or "/",
            "layout": str(form.get("layout", "shows"))}
    if vals["layout"] not in folders.LAYOUTS:
        vals["layout"] = "shows"
    if not vals["name"]:
        return vals, "Give the folder a name."
    if not vals["path"].startswith("/"):
        return vals, "Use a full path inside the container, e.g. /media/Movies."
    if reason := folders.forbidden(vals["path"]):
        return vals, reason
    return vals, ""


def settings_msg(msg: str):
    from urllib.parse import urlencode
    return back("/settings?" + urlencode({"msg": msg}) + "#folders" if msg else "/settings#folders")


@app.post("/settings/folders")
async def add_folder(request: Request):
    vals, err = folder_values(await request.form())
    if err:
        return settings_msg(err)
    db.run("INSERT INTO folders(name, path, layout) VALUES (?, ?, ?)", (vals["name"], vals["path"], vals["layout"]))
    return settings_msg(f"Added {vals['name']}.")


@app.post("/settings/folders/defaults")
async def folder_defaults(request: Request):
    form = await request.form()
    for k in ("default_folder", "default_audio_folder"):
        if db.one("SELECT 1 FROM folders WHERE id=?", (str(form.get(k, "")),)):
            db.set_setting(k, str(form[k]))
    return settings_msg("Saved the default folders.")


@app.post("/settings/folders/{fid}")
async def edit_folder(request: Request, fid: int):
    vals, err = folder_values(await request.form())
    if err:
        return settings_msg(err)
    db.run("UPDATE folders SET name=?, path=?, layout=? WHERE id=?", (vals["name"], vals["path"], vals["layout"], fid))
    return settings_msg(f"Saved {vals['name']}. Files already downloaded stay where they are until you run "
                        "Rename & refresh.")


@app.post("/settings/folders/{fid}/delete")
def delete_folder(fid: int):
    f = db.one("SELECT * FROM folders WHERE id=?", (fid,))
    if f is None:
        return settings_msg("")
    n = db.one("SELECT COUNT(*) n FROM sources WHERE folder_id=?", (fid,))["n"]
    if n:
        return settings_msg(f"{f['name']} is used by {n} source(s). Move them to another folder first.")
    if str(fid) in (db.setting("default_folder"), db.setting("default_audio_folder")):
        return settings_msg(f"{f['name']} is a default folder. Pick another default first.")
    db.run("DELETE FROM folders WHERE id=?", (fid,))
    return settings_msg(f"Removed {f['name']}. No files were deleted.")


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    for k in ("rate_limit", "sleep_seconds", "retries", "proxy"):
        if k in form:
            db.set_setting(k, str(form[k]).strip())
    db.set_setting("scheduler", "1" if form.get("scheduler") else "0")
    return back("/settings")


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}


@app.get("/api/status")
def api_status():
    return {"progress": {str(k): v for k, v in ytdl.progress.items()}, "counts": counts(),
            "indexing": worker.state["indexing"], "paused": worker.state["paused"],
            "browser": browser.running()}
