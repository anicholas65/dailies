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
            "WHERE m.status='queued' ORDER BY m.upload_date IS NULL, m.upload_date DESC, m.id LIMIT 1")
        if row is None:
            _wake.wait(30)
            _wake.clear()
            continue
        if ytdl.download(row["id"]) == "login":
            # YouTube's bot check fails every request the same way - stop hammering it.
            state["paused"] = True
            state["pause_reason"] = "YouTube wants you signed in (bot check). Sign in, then resume."
            db.log("warn", "Queue paused: " + state["pause_reason"])
            continue
        try:
            pause = float(db.setting("sleep_seconds") or 0)
        except ValueError:
            pause = 0
        _stop.wait(pause)


def start():
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()
    threading.Thread(target=download_loop, name="downloader", daemon=True).start()


def stop():
    _stop.set()
    _wake.set()


def kick():
    _wake.set()
