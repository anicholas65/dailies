"""Background scheduler (indexing + retention) and the download queue worker."""
import datetime as dt
import threading
import time

from . import db, ytdl

_stop = threading.Event()
_wake = threading.Event()
_index_lock = threading.Lock()
state = {"indexing": None, "paused": False, "pause_reason": ""}


def due_sources():
    now = dt.datetime.utcnow()
    for s in db.q("SELECT * FROM sources WHERE enabled=1"):
        if not s["last_indexed"]:
            yield s
            continue
        last = dt.datetime.fromisoformat(s["last_indexed"])
        if now - last >= dt.timedelta(hours=s["interval_hours"]):
            yield s


def index_now(source_id: int):
    """Index one source in a background thread (used by the UI buttons)."""
    def go():
        with _index_lock:
            state["indexing"] = source_id
            try:
                ytdl.index_source(source_id)
            finally:
                state["indexing"] = None
        _wake.set()
    threading.Thread(target=go, daemon=True).start()


def scheduler_loop():
    last_retention = 0.0
    while not _stop.is_set():
        if db.setting("scheduler") == "1":
            for s in list(due_sources()):
                if _stop.is_set():
                    break
                with _index_lock:
                    state["indexing"] = s["id"]
                    try:
                        ytdl.index_source(s["id"])
                    finally:
                        state["indexing"] = None
                _wake.set()
            if time.time() - last_retention > 3600:
                ytdl.apply_retention()
                last_retention = time.time()
        _stop.wait(60)


def download_loop():
    while not _stop.is_set():
        row = None if state["paused"] else db.one(
            "SELECT m.id FROM media m JOIN sources s ON s.id=m.source_id "
            "WHERE m.status='queued' ORDER BY m.priority DESC, m.upload_date IS NULL, m.upload_date DESC, m.id LIMIT 1")
        job = ytdl.download
        if row is None and not state["paused"]:
            row = db.one("SELECT id FROM media WHERE refresh=1 AND status='downloaded' ORDER BY id LIMIT 1")
            job = ytdl.refresh
        if row is None:
            _wake.wait(30)
            _wake.clear()
            continue
        if job(row["id"]) == "login":
            # YouTube's bot check fails every request the same way - stop hammering it.
            set_paused(True, "YouTube wants you signed in (bot check). Sign in, then resume.")
            db.log("warn", "Queue paused: " + state["pause_reason"])
            continue
        try:
            pause = float(db.setting("sleep_seconds") or 0)
        except ValueError:
            pause = 0
        _stop.wait(pause)


def set_paused(paused: bool, reason: str = ""):
    """Stop/resume the queue. Remembered across restarts (the reason isn't)."""
    state.update(paused=paused, pause_reason=reason if paused else "")
    db.run("INSERT OR REPLACE INTO settings(key, value) VALUES ('queue_paused', ?)", ("1" if paused else "0",))
    _wake.set()


def stop_downloads():
    """Stop the queue now: the running download is cancelled and goes back to the front of the queue."""
    set_paused(True)
    for mid in list(ytdl.progress):
        ytdl.cancel[mid] = "queued"


def start():
    state["paused"] = db.setting("queue_paused") == "1"
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()
    threading.Thread(target=download_loop, name="downloader", daemon=True).start()


def stop():
    _stop.set()
    _wake.set()


def kick():
    _wake.set()
