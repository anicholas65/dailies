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


# os.path.exists/isdir never raise; Path.exists() raises PermissionError on Python 3.13 when a parent
# folder isn't readable by the container user, which crashed the Settings page.
def exists(p) -> bool:
    return os.path.exists(p)


def mount_point(path: Path) -> Path:
    p = Path(os.path.abspath(path))
    while not exists(p):
        p = p.parent
    while not os.path.ismount(p):
        p = p.parent
    return p


IN_CONTAINER = Path("/.dockerenv").exists()

SYSTEM_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/tmp", "/etc", "/usr", "/var", "/lib",
                 "/opt", "/app", "/home")


def _unescape(s: str) -> str:
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), s)


def mounts() -> dict[str, str | None]:
    """Container mount point -> folder on the server, where mountinfo tells us (Unraid user shares
    and array disks); None when it can't (e.g. a pool's subvolume)."""
    out = {}
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        try:
            root, mp = _unescape(f[3]), _unescape(f[4])
            fstype, src = f[f.index("-") + 1], f[f.index("-") + 2]
        except (IndexError, ValueError):
            continue
        host = None
        if fstype.startswith("fuse.shfs"):
            host = "/mnt/user" + (root if root != "/" else "")
        elif m := re.fullmatch(r"/dev/md(\d+)(p\d+)?", src):
            host = f"/mnt/disk{m.group(1)}" + (root if root != "/" else "")
        out[mp] = host
    return out


def host_path(path) -> str | None:
    """Where a container path lives on the server, e.g. /downloads/X -> /mnt/user/Data/Media/YouTube/X."""
    p = Path(os.path.abspath(path))
    best = None
    for mp, host in mounts().items():
        if mp != "/" and (p == Path(mp) or p.is_relative_to(mp)) and (best is None or len(mp) > len(best[0])):
            best = (mp, host)
    if not best or not best[1]:
        return None
    rest = p.relative_to(best[0])
    return best[1] + ("" if str(rest) == "." else "/" + str(rest))


def volumes() -> list[Path]:
    """Directories mapped into the container (Unraid Paths), minus /config and system mounts."""
    found = {config.DOWNLOADS_DIR}
    for mp in mounts():
        if mp == "/" or mp.startswith(SYSTEM_MOUNTS) or Path(mp) == config.CONFIG_DIR:
            continue
        found.add(Path(mp))
    return sorted(p for p in found if os.path.isdir(p))


def _subdirs(p: Path) -> list[Path]:
    try:
        return sorted((c for c in p.iterdir() if os.path.isdir(c) and not c.name.startswith((".", "@"))),
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


FORBIDDEN = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys",
             "/usr", "/var", "/app", "/opt")


def forbidden(path: str) -> str:
    """Download folders may not be system directories or hold Dailies' own config/sign-in data."""
    p = Path(os.path.abspath(path))
    cfg = config.CONFIG_DIR.resolve()
    if str(p) == "/" or any(str(p) == f or (f != "/" and p.is_relative_to(f)) for f in FORBIDDEN):
        return f"{path} is a system folder. Pick one of your mapped shares."
    if p.resolve() == cfg or p.resolve().is_relative_to(cfg) or cfg.is_relative_to(p.resolve()):
        return f"{path} would mix downloads with Dailies' settings and Google sign-in data. Pick another folder."
    return ""


def check(path: str) -> tuple[bool, str, str]:
    """(usable, short status, explanation) for the Settings page and before a download."""
    p = Path(path)
    if not p.is_absolute():
        return False, "Invalid path", "Use a full path inside the container, e.g. /media/Movies."
    if reason := forbidden(path):
        return False, "Not allowed", reason
    existing = p
    while not exists(existing):
        existing = existing.parent
    if (IN_CONTAINER and mount_point(p) == Path("/")) or existing == Path("/"):
        top = "/" + p.parts[1] if len(p.parts) > 1 else path
        return False, "Not mapped", (f"{top} isn't mapped to your server. In Unraid, edit the Dailies container and "
                                     f"add a Path: container {top} → host folder (e.g. /mnt/user/Data/Media).")
    if not os.access(existing, os.W_OK):
        return False, "No write access", f"Dailies can't write to {existing}. Check the share's permissions."
    return True, "Ready", "" if exists(p) else "Folder is created on the first download."
