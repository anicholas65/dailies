"""SQLite storage."""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  quality TEXT NOT NULL DEFAULT '1080',      -- best|2160|1440|1080|720|480|audio
  container TEXT NOT NULL DEFAULT 'mp4',     -- mp4|mkv|webm ; audio: m4a|mp3|opus
  prefer_h264 INTEGER NOT NULL DEFAULT 1,
  output TEXT NOT NULL DEFAULT '{source}/%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s',
  embed_thumbnail INTEGER NOT NULL DEFAULT 1,
  write_thumbnail INTEGER NOT NULL DEFAULT 1,
  write_nfo INTEGER NOT NULL DEFAULT 1,
  embed_metadata INTEGER NOT NULL DEFAULT 1,
  embed_chapters INTEGER NOT NULL DEFAULT 1,
  subtitles TEXT NOT NULL DEFAULT '',        -- e.g. "en,es" ; blank = none
  auto_subs INTEGER NOT NULL DEFAULT 0,
  sponsorblock TEXT NOT NULL DEFAULT ''      -- e.g. "sponsor,selfpromo" ; blank = off
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'channel',      -- channel|playlist|video|feed
  profile_id INTEGER NOT NULL REFERENCES profiles(id),
  enabled INTEGER NOT NULL DEFAULT 1,
  auto_download INTEGER NOT NULL DEFAULT 1,
  include_shorts INTEGER NOT NULL DEFAULT 0,
  include_live INTEGER NOT NULL DEFAULT 0,
  interval_hours REAL NOT NULL DEFAULT 6,
  max_items INTEGER NOT NULL DEFAULT 0,      -- index only the newest N (0 = all)
  download_after TEXT NOT NULL DEFAULT '',   -- YYYY-MM-DD ; skip older uploads
  title_filter TEXT NOT NULL DEFAULT '',     -- regex, must match
  title_exclude TEXT NOT NULL DEFAULT '',    -- regex, must not match
  min_duration INTEGER NOT NULL DEFAULT 0,   -- seconds
  keep_days INTEGER NOT NULL DEFAULT 0,      -- delete files older than N days (0 = keep)
  last_indexed TEXT,
  last_error TEXT,
  created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  upload_date TEXT,
  duration INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',    -- pending|queued|downloading|downloaded|failed|skipped|deleted
  filepath TEXT,
  filesize INTEGER,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  found TEXT NOT NULL DEFAULT (datetime('now')),
  downloaded TEXT,
  UNIQUE(source_id, video_id)
);
CREATE INDEX IF NOT EXISTS media_status ON media(status);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  level TEXT NOT NULL,
  message TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "auth_mode": "browser",      # browser|file|none
    "rate_limit": "",            # e.g. 5M
    "sleep_seconds": "5",        # pause between downloads
    "retries": "3",
    "proxy": "",
    "scheduler": "1",
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn: sqlite3.Connection | None = None


def get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def reset():
    """Close the shared connection (tests switch CONFIG_DIR)."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


@contextmanager
def tx():
    with _lock:
        conn = get()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql, args=()):
    with _lock:
        return get().execute(sql, args).fetchall()


def one(sql, args=()):
    with _lock:
        return get().execute(sql, args).fetchone()


def run(sql, args=()) -> int:
    with tx() as conn:
        return conn.execute(sql, args).lastrowid


PRESETS_V2 = [  # name, quality, container, write_nfo
    ("720p MP4", "720", "mp4", 1),
    ("2160p (4K) MP4", "2160", "mp4", 1),
    ("Audio (MP3)", "audio", "mp3", 0),
    ("Audio (WAV)", "audio", "wav", 0),
]


def init():
    config.ensure_dirs()
    with tx() as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        if "auto_name" not in cols:  # 0.1.5: name the source from YouTube on first index
            conn.execute("ALTER TABLE sources ADD COLUMN auto_name INTEGER NOT NULL DEFAULT 0")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        fresh = not conn.execute("SELECT 1 FROM profiles").fetchone()
        if fresh:
            conn.execute("INSERT INTO profiles(name, quality, container) VALUES ('1080p MP4', '1080', 'mp4')")
            conn.execute("INSERT INTO profiles(name, quality, container) VALUES ('Best (MKV)', 'best', 'mkv')")
            conn.execute(
                "INSERT INTO profiles(name, quality, container, embed_chapters, write_nfo) "
                "VALUES ('Audio (M4A)', 'audio', 'm4a', 1, 0)"
            )
        # presets added in 0.1.5 - once, so deleting one later sticks
        if not conn.execute("SELECT 1 FROM settings WHERE key='presets_v2'").fetchone():
            have = {r[0] for r in conn.execute("SELECT name FROM profiles")}
            for name, quality, container, nfo in PRESETS_V2:
                if name not in have:
                    conn.execute("INSERT INTO profiles(name, quality, container, write_nfo) VALUES (?, ?, ?, ?)",
                                 (name, quality, container, nfo))
            conn.execute("INSERT INTO settings(key, value) VALUES ('presets_v2', '1')")
        # downloads interrupted by a restart go back to the queue
        conn.execute("UPDATE media SET status='queued' WHERE status='downloading'")


def setting(key: str) -> str:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str):
    run("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def log(level: str, message: str):
    with tx() as conn:
        conn.execute("INSERT INTO log(level, message) VALUES (?, ?)", (level, message[:2000]))
        conn.execute("DELETE FROM log WHERE id <= (SELECT MAX(id) - 2000 FROM log)")
