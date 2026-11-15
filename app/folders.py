"""Download folders: where each source's files go, and in which Jellyfin layout."""
import os
import re
from pathlib import Path

from . import config, db

# layout -> (label, example of where a file lands)
LAYOUTS = {
    "shows": ("Shows", "Channel/Season 2026/s2026e052301 - Title [id].mp4"),
    "movies": ("Movies", "Title (2026) [id]/Title (2026) [id].mp4"),
    "music": ("Music", "Channel/Title [id].mp3"),
}

# seeded once (0.1.12); /media is an optional extra mapping to the host's media share
DEFAULT_FOLDERS = [
    ("YouTube", None, "shows"),  # None = DOWNLOADS_DIR, the original /downloads mapping
    ("Educational", "/media/Educational", "shows"),
    ("Movies", "/media/Movies", "movies"),
    ("Concerts", "/media/Concerts", "movies"),
    ("MP3", "/media/Music", "music"),
]


def seed(conn):
    if conn.execute("SELECT 1 FROM settings WHERE key='folders_v1'").fetchone():
        return
    ids = {}
    for name, path, layout in DEFAULT_FOLDERS:
        cur = conn.execute("INSERT INTO folders(name, path, layout) VALUES (?, ?, ?)",
                           (name, path or str(config.DOWNLOADS_DIR), layout))
        ids[name] = cur.lastrowid
    for key, name in (("default_folder", "YouTube"), ("default_audio_folder", "MP3")):
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(ids[name])))
    conn.execute("INSERT INTO settings(key, value) VALUES ('folders_v1', '1')")


def all_folders():
    return db.q("SELECT * FROM folders ORDER BY id")


def for_source(source, profile):
    """The source's chosen folder, else the default for audio/video profiles."""
    fid = source["folder_id"] if "folder_id" in source.keys() else None
    if not fid:
        key = "default_audio_folder" if profile["quality"] == "audio" else "default_folder"
        fid = db.setting(key)
    row = db.one("SELECT * FROM folders WHERE id=?", (fid,)) if fid else None
    return row or db.one("SELECT * FROM folders ORDER BY id") or {
        "id": 0, "name": "Downloads", "path": str(config.DOWNLOADS_DIR), "layout": "shows"}


def roots() -> list[Path]:
    """Every folder Dailies may write to, deepest first (for prune/delete safety checks)."""
    paths = {config.DOWNLOADS_DIR.resolve()}
    try:
        paths |= {Path(f["path"]).resolve() for f in all_folders()}
    except Exception:  # table missing in a half-initialised DB
        pass
    return sorted(paths, key=lambda p: len(p.parts), reverse=True)


def root_of(path: Path) -> Path | None:
    path = path.resolve()
    return next((r for r in roots() if path == r or path.is_relative_to(r)), None)


def mount_point(path: Path) -> Path:
    p = Path(os.path.abspath(path))
    while not p.exists():
        p = p.parent
    while not os.path.ismount(p):
        p = p.parent
    return p


IN_CONTAINER = Path("/.dockerenv").exists()

SYSTEM_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/tmp", "/etc", "/usr", "/var", "/lib",
                 "/opt", "/app", "/home")


def volumes() -> list[Path]:
    """Directories mapped into the container (Unraid Paths), minus /config and system mounts."""
    found = {config.DOWNLOADS_DIR}
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mp = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), line.split()[4])
            if mp == "/" or mp.startswith(SYSTEM_MOUNTS) or Path(mp) == config.CONFIG_DIR:
                continue
            found.add(Path(mp))
    except OSError:
        pass
    return sorted(p for p in found if p.is_dir())


def _subdirs(p: Path) -> list[Path]:
    try:
        return sorted((c for c in p.iterdir() if c.is_dir() and not c.name.startswith((".", "@"))),
                      key=lambda c: c.name.lower())
    except OSError:
        return []


def locations() -> dict[str, list[str]]:
    """Choices for the folder dropdowns: each mapped volume, its folders, and one level deeper where
    that stays short (so /media/Music/YouTube is offered but not every film in /media/Movies)."""
    out = {}
    for vol in volumes():
        paths = [vol]
        for child in _subdirs(vol)[:150]:
            paths.append(child)
            grand = _subdirs(child)
            if len(grand) <= 25:
                paths += grand
        out[str(vol)] = [str(p) for p in paths]
    return out


def check(path: str) -> tuple[bool, str, str]:
    """(usable, short status, explanation) for the Settings page and before a download."""
    p = Path(path)
    if not p.is_absolute():
        return False, "Invalid path", "Use a full path inside the container, e.g. /media/Movies."
    existing = p
    while not existing.exists():
        existing = existing.parent
    if (IN_CONTAINER and mount_point(p) == Path("/")) or existing == Path("/"):
        top = "/" + p.parts[1] if len(p.parts) > 1 else path
        return False, "Not mapped", (f"{top} isn't mapped to your server. In Unraid, edit the Dailies container and "
                                     f"add a Path: container {top} → host folder (e.g. /mnt/user/Data/Media).")
    if not os.access(existing, os.W_OK):
        return False, "No write access", f"Dailies can't write to {existing}. Check the share's permissions."
    return True, "Ready", "" if p.exists() else "Folder is created on the first download."
