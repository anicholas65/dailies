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

# The URL YouTube's own "Sign in" button uses. The bare action_handle_signin=true continue URL (without
# app/next) lands on youtube.com/oops "There was an issue signing you into YouTube" when Google already
# has a session, e.g. reopening the browser after an earlier sign-in.
GOOGLE_SIGNIN = ("https://accounts.google.com/ServiceLogin?service=youtube&uilel=3&passive=true"
                 "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue"
                 "%26app%3Ddesktop%26hl%3Den%26next%3Dhttps%253A%252F%252Fwww.youtube.com%252F")
YOUTUBE_HOME = "https://www.youtube.com/"
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
    log = open(config.CONFIG_DIR / f"{name}.log", "wb")  # fresh log each start
    _procs[name] = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)


def log_tail(name: str, lines: int = 12) -> str:
    try:
        text = (config.CONFIG_DIR / f"{name}.log").read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _wait(name: str, ready, timeout: float):
    """Wait until ready() is true; fail loudly if the process dies or it takes too long."""
    end = time.time() + timeout
    while time.time() < end:
        proc = _procs[name]
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with code {proc.returncode}: {log_tail(name) or 'no output'}")
        if ready():
            return
        time.sleep(0.2)
    raise RuntimeError(f"{name} did not start within {timeout:.0f}s: {log_tail(name) or 'no output'}")


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _clear_stale_locks():
    # A container restart can leave Chromium's singleton lock pointing at a dead host/pid.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = config.BROWSER_PROFILE / name
        if path.is_symlink() or path.exists():
            path.unlink(missing_ok=True)


def start_url() -> str:
    """YouTube itself when the profile is already signed in, else Google sign-in."""
    return YOUTUBE_HOME if profile_signed_in() else GOOGLE_SIGNIN


def start(url: str | None = None):
    url = url or start_url()
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
        num = config.DISPLAY.lstrip(":")
        Path(f"/tmp/.X{num}-lock").unlink(missing_ok=True)
        Path(f"/tmp/.X11-unix/X{num}").unlink(missing_ok=True)
        try:
            _spawn("xvfb", ["Xvfb", config.DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp", "-ac"], env)
            _wait("xvfb", lambda: Path(f"/tmp/.X11-unix/X{num}").exists(), 10)
            _spawn("x11vnc", ["x11vnc", "-display", config.DISPLAY, "-rfbport", str(config.VNC_PORT),
                              "-localhost", "-nopw", "-forever", "-shared", "-noxdamage"], env)
            _wait("x11vnc", lambda: _port_open(config.VNC_PORT), 15)
            _spawn("chromium", [
                chromium_binary(),
                f"--user-data-dir={config.BROWSER_PROFILE}",
                "--password-store=basic",
                "--no-sandbox",               # containers lack the user namespaces Chromium's sandbox needs
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter",
                "--window-position=0,0", f"--window-size={w},{h}",
                "--start-maximized",
                url,
            ], env)
            started = time.time()
            _wait("chromium", lambda: time.time() - started > 3, 10)  # survives startup
        except Exception as exc:
            stop_locked()
            db.log("error", f"Sign-in browser failed: {exc}")
            raise RuntimeError(str(exc)) from exc
        db.log("info", "Sign-in browser started")


def diagnostics() -> dict:
    procs = {n: ("running" if (p := _procs.get(n)) and p.poll() is None
                 else ("not started" if p is None else f"exited ({p.returncode})"))
             for n in ("xvfb", "x11vnc", "chromium")}
    return {"procs": procs, "vnc_port": _port_open(config.VNC_PORT),
            "logs": {n: log_tail(n, 40) for n in ("xvfb", "x11vnc", "chromium")}}


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


def profile_signed_in() -> bool:
    """Whether the browser profile already holds YouTube sign-in cookies."""
    try:
        return has_profile_cookies() and bool(
            AUTH_COOKIES & {c.name for c in youtube_cookies(load_browser_cookies())})
    except Exception:
        return False


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
    """Write the browser profile's youtube.com cookies (enough for yt-dlp) to a Netscape cookies.txt.
    google.com cookies are left out: they would unlock the whole Google account (Gmail, Drive...)."""
    from yt_dlp.cookies import YoutubeDLCookieJar
    out = YoutubeDLCookieJar(str(dest))
    for c in [c for c in load_browser_cookies() if c.domain.lstrip(".").endswith("youtube.com")]:
        out.set_cookie(c)
    out.save(ignore_discard=True, ignore_expires=True)
    dest.chmod(0o600)
    return len(out)
