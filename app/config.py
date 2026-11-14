"""Paths and environment settings."""
import os
import secrets
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/downloads"))
DB_PATH = CONFIG_DIR / "dailies.db"
BROWSER_PROFILE = CONFIG_DIR / "browser"          # Chromium user-data-dir used for YouTube sign-in
COOKIES_FILE = CONFIG_DIR / "cookies.txt"          # uploaded / exported Netscape cookies
PASSWORD = os.environ.get("DAILIES_PASSWORD", "")  # optional web UI password
DISPLAY = os.environ.get("DAILIES_DISPLAY", ":99")
VNC_PORT = int(os.environ.get("DAILIES_VNC_PORT", "5900"))
SCREEN = os.environ.get("DAILIES_SCREEN", "1280x800")
SCHEDULER = os.environ.get("DAILIES_SCHEDULER", "1") != "0"


def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def secret_key() -> bytes:
    path = CONFIG_DIR / "secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()
