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

from . import __version__, browser, config, db, worker, ytdl

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["version"] = __version__


@asynccontextmanager
async def lifespan(app):
    db.init()
    if config.SCHEDULER:
        worker.start()
    yield
    worker.stop()
    browser.stop()


app = FastAPI(title="Dailies", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
if Path(browser.novnc_dir()).is_dir():
    app.mount("/novnc", StaticFiles(directory=browser.novnc_dir(), html=True), name="novnc")


# ---- optional password -------------------------------------------------------

def _token() -> str:
    return hmac.new(config.secret_key(), b"dailies:" + config.PASSWORD.encode(), hashlib.sha256).hexdigest()


def authed(cookies) -> bool:
    return not config.PASSWORD or hmac.compare_digest(cookies.get("dailies", ""), _token())


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/static", "/login", "/api/health")) or authed(request.cookies):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login(password: str = Form(...)):
    if not config.PASSWORD or not hmac.compare_digest(password, config.PASSWORD):
        return RedirectResponse("/login?error=1", status_code=303)
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
    ctx["nav"] = name.split(".")[0]
    return templates.TemplateResponse(request, name, ctx)


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
        worker.state.update(paused=False, pause_reason="")
        db.run("UPDATE media SET status='queued', error=NULL WHERE status='failed' AND error LIKE '%sign in%'")
        worker.kick()


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
    return vals


# ---- pages -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return page(
        request, "dashboard.html",
        counts=counts(),
        sources=db.q("SELECT COUNT(*) n FROM sources")[0]["n"],
        queue=db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id "
                   "WHERE m.status IN ('queued','downloading') "
                   "ORDER BY m.status='downloading' DESC, m.upload_date DESC LIMIT 15"),
        recent=db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id "
                    "WHERE m.status='downloaded' ORDER BY m.downloaded DESC LIMIT 10"),
        failed=db.q("SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id "
                    "WHERE m.status='failed' ORDER BY m.id DESC LIMIT 5"),
        paused=worker.state["paused"], pause_reason=worker.state["pause_reason"],
    )


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    rows = db.q("""SELECT s.*, p.name profile,
        SUM(m.status='downloaded') done, SUM(m.status IN ('queued','downloading')) queued,
        SUM(m.status='failed') failed, COUNT(m.id) total
        FROM sources s JOIN profiles p ON p.id=s.profile_id LEFT JOIN media m ON m.source_id=s.id
        GROUP BY s.id ORDER BY s.name COLLATE NOCASE""")
    return page(request, "sources.html", sources=rows, profiles=db.q("SELECT * FROM profiles ORDER BY id"),
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
    sql, args = "SELECT * FROM media WHERE source_id=?", [sid]
    if status:
        sql += " AND status=?"
        args.append(status)
    media = db.q(sql + " ORDER BY upload_date IS NULL, upload_date DESC, id DESC LIMIT 500", args)
    return page(request, "source.html", s=s, media=media, counts=counts(sid), status=status,
                profiles=db.q("SELECT * FROM profiles ORDER BY id"), indexing=worker.state["indexing"] == sid,
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


@app.post("/sources/{sid}/delete")
async def delete_source(request: Request, sid: int):
    form = await request.form()
    if form.get("files"):
        for m in db.q("SELECT * FROM media WHERE source_id=? AND filepath IS NOT NULL", (sid,)):
            ytdl.delete_files(m)
    db.run("DELETE FROM sources WHERE id=?", (sid,))
    return back("/sources")


@app.get("/media", response_class=HTMLResponse)
def media(request: Request, status: str = "", q: str = ""):
    sql = "SELECT m.*, s.name source FROM media m JOIN sources s ON s.id=m.source_id WHERE 1=1"
    args = []
    if status:
        sql += " AND m.status=?"
        args.append(status)
    if q:
        sql += " AND m.title LIKE ?"
        args.append(f"%{q}%")
    rows = db.q(sql + " ORDER BY COALESCE(m.downloaded, m.found) DESC LIMIT 500", args)
    return page(request, "media.html", media=rows, status=status, q=q, counts=counts())


@app.post("/media/{mid}/{action}")
async def media_action(request: Request, mid: int, action: str):
    m = db.one("SELECT * FROM media WHERE id=?", (mid,))
    if m is None:
        return back("/media")
    if action in ("queue", "retry"):
        db.run("UPDATE media SET status='queued', error=NULL WHERE id=?", (mid,))
        worker.kick()
    elif action == "skip":
        db.run("UPDATE media SET status='skipped' WHERE id=?", (mid,))
    elif action == "delete":
        ytdl.delete_files(m)
        db.run("UPDATE media SET status='deleted', filepath=NULL WHERE id=?", (mid,))
    return back(request.headers.get("referer") or "/media")


@app.post("/media/retry-failed")
def retry_failed():
    db.run("UPDATE media SET status='queued', error=NULL WHERE status='failed'")
    worker.kick()
    return back("/")


@app.post("/queue/{action}")
def queue_control(action: str):
    worker.state["paused"] = action == "pause"
    worker.state["pause_reason"] = ""
    worker.kick()
    return back("/")


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
def youtube(request: Request, msg: str = "", ok: str = ""):
    return page(request, "youtube.html", msg=msg, ok=ok, has_file=config.COOKIES_FILE.exists(),
                diag=browser.diagnostics())


@app.post("/youtube/browser/start")
def browser_start():
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
    dest = config.CONFIG_DIR / "exported-cookies.txt"
    try:
        browser.export_cookies_txt(dest)
    except Exception as exc:
        return yt(f"Export failed: {exc}", False)
    return FileResponse(dest, filename="youtube-cookies.txt", media_type="text/plain")


@app.post("/youtube/verify")
async def verify():
    ok, detail = await asyncio.to_thread(ytdl.verify_login)
    return yt(detail, ok)


@app.websocket("/vnc/ws")
async def vnc_proxy(ws: WebSocket):
    """Bridge noVNC (WebSocket) to x11vnc (TCP, localhost only)."""
    if not authed(ws.cookies) or not browser.running():
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
def settings(request: Request):
    import yt_dlp.version
    vals = {k: db.setting(k) for k in db.DEFAULT_SETTINGS}
    return page(request, "settings.html", s=vals, ytdlp=yt_dlp.version.__version__,
                downloads=config.DOWNLOADS_DIR,
                log=db.q("SELECT * FROM log ORDER BY id DESC LIMIT 200"))


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
