"""In-container Chromium for signing in to YouTube.

Xvfb provides a virtual display, Chromium runs on it with a persistent profile
in /config/browser, and x11vnc exposes the display on localhost only. The web
UI reaches it through an authenticated WebSocket proxy (see main.py) using
noVNC, so you type your Google credentials into a real browser - nothing is
automated and the password is never stored by Dailies.

yt-dlp then reads cookies straight from that profile (``--cookies-from-browser
chromium+basictext:/config/browser``). Chromium is started with
``--password-store=basic`` so its cookie DB is decryptable without a keyring.
"""
import http.cookiejar
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import config, db

GOOGLE_SIGNIN = ("https://accounts.google.com/ServiceLogin?service=youtube"
                 "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue")
AUTH_COOKIES = {"LOGIN_INFO", "SAPISID", "__Secure-3PSID", "SID"}

_lock = threading.Lock()
_procs: dict[str, subprocess.Popen] = {}


def chromium_binary() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def available() -> dict:
    return {
        "chromium": chromium_binary() is not None,
        "xvfb": shutil.which("Xvfb") is not None,
        "x11vnc": shutil.which("x11vnc") is not None,
        "novnc": Path(novnc_dir()).is_dir(),
    }


def novnc_dir() -> str:
    return os.environ.get("NOVNC_DIR", "/usr/share/novnc")


def running() -> bool:
    proc = _procs.get("chromium")
    return proc is not None and proc.poll() is None


def _spawn(name: str, args: list[str], env: dict):
    log = open(config.CONFIG_DIR / f"{name}.log", "ab")
    _procs[name] = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)


def _clear_stale_locks():
    # A container restart can leave Chromium's singleton lock pointing at a dead host/pid.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = config.BROWSER_PROFILE / name
        if path.is_symlink() or path.exists():
            path.unlink(missing_ok=True)


def start(url: str = GOOGLE_SIGNIN):
    with _lock:
        if running():
            return
        stop_locked()
        missing = [k for k, ok in available().items() if not ok]
        if missing:
            raise RuntimeError("Browser sign-in needs: " + ", ".join(missing) +
                               " (these ship in the Docker image)")
        config.BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
        _clear_stale_locks()
        env = dict(os.environ, DISPLAY=config.DISPLAY, HOME=str(config.CONFIG_DIR))
        w, h = config.SCREEN.split("x")
        lock = Path(f"/tmp/.X{config.DISPLAY.lstrip(':')}-lock")
        lock.unlink(missing_ok=True)
        _spawn("xvfb", ["Xvfb", config.DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"], env)
        time.sleep(1.0)
        _spawn("x11vnc", ["x11vnc", "-display", config.DISPLAY, "-rfbport", str(config.VNC_PORT),
                          "-localhost", "-nopw", "-forever", "-shared", "-quiet", "-noxdamage"], env)
        _spawn("chromium", [
            chromium_binary(),
            f"--user-data-dir={config.BROWSER_PROFILE}",
            "--password-store=basic",
            "--no-sandbox",               # containers lack the user namespaces Chromium's sandbox needs
            "--disable-dev-shm-usage",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--window-position=0,0", f"--window-size={w},{h}",
            "--start-maximized",
            url,
        ], env)
        db.log("info", "Sign-in browser started")


def stop_locked():
    # Chromium first, with SIGTERM, so it flushes the cookie DB to disk.
    for name in ("chromium", "x11vnc", "xvfb"):
        proc = _procs.pop(name, None)
        if proc is None or proc.poll() is not None:
            continue
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def stop():
    with _lock:
        was = running()
        stop_locked()
    if was:
        db.log("info", "Sign-in browser closed")


def wipe_profile():
    """Sign out: delete the Chromium profile entirely."""
    stop()
    shutil.rmtree(config.BROWSER_PROFILE, ignore_errors=True)
    db.log("info", "Browser profile deleted (signed out)")


# ---- cookies -----------------------------------------------------------------

def browser_spec():
    """The ``cookiesfrombrowser`` tuple yt-dlp expects."""
    return ("chromium", str(config.BROWSER_PROFILE), "BASICTEXT", None)


def has_profile_cookies() -> bool:
    return any(config.BROWSER_PROFILE.glob("**/Cookies"))


def load_browser_cookies():
    from yt_dlp.cookies import extract_cookies_from_browser
    name, profile, keyring, container = browser_spec()
    return extract_cookies_from_browser(name, profile, keyring=keyring, container=container)


def load_file_cookies():
    jar = http.cookiejar.MozillaCookieJar(str(config.COOKIES_FILE))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def youtube_cookies(jar) -> list[http.cookiejar.Cookie]:
    return [c for c in jar if c.domain.lstrip(".").endswith(("youtube.com", "google.com"))]


def status() -> dict:
    """What auth yt-dlp will use and whether it looks signed in (cheap, offline check)."""
    mode = db.setting("auth_mode")
    out = {"mode": mode, "running": running(), "available": available(),
           "signed_in": False, "cookie_count": 0, "expires": None, "detail": ""}
    try:
        if mode == "browser":
            if not has_profile_cookies():
                out["detail"] = "No browser profile yet - start the browser and sign in."
                return out
            jar = load_browser_cookies()
        elif mode == "file":
            if not config.COOKIES_FILE.exists():
                out["detail"] = "No cookies.txt uploaded."
                return out
            jar = load_file_cookies()
        else:
            out["detail"] = "Anonymous - age-restricted, members-only and private videos will fail."
            return out
    except Exception as exc:  # corrupt / locked / undecryptable
        out["detail"] = f"Could not read cookies: {exc}"
        return out
    yt = youtube_cookies(jar)
    out["cookie_count"] = len(yt)
    out["signed_in"] = any(c.name in AUTH_COOKIES and c.domain.lstrip(".").endswith("youtube.com") for c in yt)
    exp = [c.expires for c in yt if c.name in AUTH_COOKIES and c.expires]
    if exp:
        out["expires"] = time.strftime("%Y-%m-%d", time.gmtime(min(exp)))
    out["detail"] = "Signed in to YouTube." if out["signed_in"] else "Cookies found but no YouTube login."
    return out


def export_cookies_txt(dest: Path) -> int:
    """Write the browser profile's YouTube/Google cookies to a Netscape cookies.txt."""
    from yt_dlp.cookies import YoutubeDLCookieJar
    out = YoutubeDLCookieJar(str(dest))
    for c in youtube_cookies(load_browser_cookies()):
        out.set_cookie(c)
    out.save(ignore_discard=True, ignore_expires=True)
    dest.chmod(0o600)
    return len(out)
