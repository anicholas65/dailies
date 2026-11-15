"""yt-dlp integration: option building, indexing sources, downloading media."""
import datetime as dt
import html
import re
import time
from pathlib import Path

import yt_dlp

from . import browser, config, db

QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "audio": "ba/b",
}
AUDIO_CODECS = {"m4a", "mp3", "opus"}
FEEDS = {  # need a signed-in account
    "watchlater": ("Watch Later", "https://www.youtube.com/playlist?list=WL"),
    "liked": ("Liked videos", "https://www.youtube.com/playlist?list=LL"),
}


class Quiet:
    """Route yt-dlp output into our log instead of stdout."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if any(k in msg for k in ("nsig", "Falling back", "falling back", "No title found")):
            return
        db.log("warn", msg)

    def error(self, msg):
        pass  # surfaced via the raised exception


BOT_CHECK = ("Sign in to confirm", "confirm your age", "members-only", "Private video", "cookies are no longer valid")


def clean_error(exc) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    msg = re.split(r"\s+(?:Use --cookies|See\s+https?://)", msg)[0]
    return msg.removeprefix("ERROR: ").strip()


def needs_login(msg: str) -> bool:
    return any(k.lower() in msg.lower() for k in BOT_CHECK)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "source"


def base_opts() -> dict:
    opts = {
        "logger": Quiet(),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "retries": int(db.setting("retries") or 3),
        "fragment_retries": 10,
        "ignoreerrors": False,
        "remote_components": ["ejs:npm"],  # JS challenge solver scripts (deno ships in the image)
    }
    mode = db.setting("auth_mode")
    if mode == "browser" and browser.has_profile_cookies():
        opts["cookiesfrombrowser"] = browser.browser_spec()
    elif mode == "file" and config.COOKIES_FILE.exists():
        opts["cookiefile"] = str(config.COOKIES_FILE)
    if proxy := db.setting("proxy"):
        opts["proxy"] = proxy
    if rate := db.setting("rate_limit"):
        opts["ratelimit"] = parse_rate(rate)
    return opts


def parse_rate(text: str) -> int | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)\s*", text or "")
    if not m:
        return None
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def profile_opts(profile, source) -> dict:
    audio = profile["quality"] == "audio"
    folder = config.DOWNLOADS_DIR
    template = profile["output"].replace("{source}", safe_name(source["name"]))
    opts = {
        "format": QUALITY_FORMATS.get(profile["quality"], QUALITY_FORMATS["best"]),
        "outtmpl": {"default": str(folder / template),
                    "thumbnail": str(folder / template.rsplit(".", 1)[0]) + ".%(ext)s"},
        "paths": {"temp": str(config.CONFIG_DIR / "tmp")},
        "windowsfilenames": True,
        "writethumbnail": bool(profile["embed_thumbnail"] or profile["write_thumbnail"]),
        "postprocessors": [],
    }
    pps = opts["postprocessors"]
    if audio:
        codec = profile["container"] if profile["container"] in AUDIO_CODECS else "m4a"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": codec})
    else:
        opts["merge_output_format"] = profile["container"] if profile["container"] in ("mp4", "mkv", "webm") else "mkv"
        if profile["prefer_h264"]:
            # resolution first: H.264 only wins between formats of the same height
            # (YouTube's H.264 tops out at 1080p, often lower)
            opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:m4a"]
    if profile["subtitles"].strip() and not audio:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = bool(profile["auto_subs"])
        opts["subtitleslangs"] = [s.strip() for s in profile["subtitles"].split(",") if s.strip()]
        pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    if profile["sponsorblock"].strip():
        cats = [c.strip() for c in profile["sponsorblock"].split(",") if c.strip()]
        pps.append({"key": "SponsorBlock", "categories": cats, "when": "after_filter"})
        pps.append({"key": "ModifyChapters", "remove_sponsor_segments": cats})
    if profile["embed_metadata"] or profile["embed_chapters"]:
        pps.append({"key": "FFmpegMetadata", "add_metadata": bool(profile["embed_metadata"]),
                    "add_chapters": bool(profile["embed_chapters"])})
    if opts["writethumbnail"]:
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if profile["embed_thumbnail"] and (audio or opts.get("merge_output_format") != "webm"):
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": bool(profile["write_thumbnail"])})
    return opts


# ---- indexing ----------------------------------------------------------------

def source_urls(source) -> list[str]:
    url = source["url"].strip()
    if source["kind"] != "channel":
        return [url]
    root = re.sub(r"/(featured|videos|shorts|streams|playlists|about)/?$", "", url.rstrip("/"))
    if re.search(r"/(videos|shorts|streams)/?$", url.rstrip("/")):
        return [url]  # user pointed at a specific tab
    urls = [root + "/videos"]
    if source["include_shorts"]:
        urls.append(root + "/shorts")
    if source["include_live"]:
        urls.append(root + "/streams")
    return urls


def detect_kind(url: str) -> str:
    if "list=" in url and "watch?" not in url:
        return "playlist"
    if re.search(r"(watch\?v=|youtu\.be/|/shorts/[\w-]{6,}$)", url):
        return "video"
    return "channel"


def _flat_entries(info):
    for e in info.get("entries") or []:
        if not e:
            continue
        if e.get("_type") == "playlist" or (e.get("entries") is not None):
            yield from _flat_entries(e)
        else:
            yield e


def index_source(source_id: int) -> int:
    """Fetch the source's video list and add new items. Returns count of new items."""
    source = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if source is None:
        return 0
    opts = base_opts() | {"extract_flat": "in_playlist", "skip_download": True}
    if source["max_items"]:
        opts["playlistend"] = source["max_items"]
    new = 0
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            for url in source_urls(source):
                info = ydl.extract_info(url, download=False)
                entries = list(_flat_entries(info)) if info.get("entries") is not None else [info]
                for e in entries:
                    vid = e.get("id")
                    if not vid:
                        continue
                    video_url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}"
                    if not video_url.startswith("http"):
                        video_url = f"https://www.youtube.com/watch?v={vid}"
                    with db.tx() as conn:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO media(source_id, video_id, title, url, upload_date, duration, status) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (source_id, vid, e.get("title") or vid, video_url, e.get("upload_date"),
                             int(e["duration"]) if e.get("duration") else None,
                             "queued" if source["auto_download"] else "pending"))
                        new += cur.rowcount
        db.run("UPDATE sources SET last_indexed=datetime('now'), last_error=NULL WHERE id=?", (source_id,))
        db.log("info", f"Indexed {source['name']}: {new} new")
    except Exception as exc:
        db.run("UPDATE sources SET last_indexed=datetime('now'), last_error=? WHERE id=?",
               (clean_error(exc)[:1000], source_id))
        db.log("error", f"Index failed for {source['name']}: {exc}")
    return new


# ---- downloading ---------------------------------------------------------------

progress: dict = {}  # media_id -> {"percent", "speed", "eta", "title"}


def skip_reason(source, info) -> str | None:
    if source["download_after"] and info.get("upload_date"):
        if info["upload_date"] < source["download_after"].replace("-", ""):
            return f"uploaded {info['upload_date']} (before {source['download_after']})"
    title = info.get("title") or ""
    if source["title_filter"] and not re.search(source["title_filter"], title, re.I):
        return "title does not match filter"
    if source["title_exclude"] and re.search(source["title_exclude"], title, re.I):
        return "title matches exclude filter"
    if source["min_duration"] and info.get("duration") and info["duration"] < source["min_duration"]:
        return f"shorter than {source['min_duration']}s"
    if info.get("is_live") or info.get("live_status") in ("is_upcoming", "is_live"):
        return "live or upcoming"
    return None


def download(media_id: int) -> str:
    item = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    source = db.one("SELECT * FROM sources WHERE id=?", (item["source_id"],))
    profile = db.one("SELECT * FROM profiles WHERE id=?", (source["profile_id"],))
    db.run("UPDATE media SET status='downloading', attempts=attempts+1, error=NULL WHERE id=?", (media_id,))
    progress[media_id] = {"title": item["title"], "percent": 0, "speed": "", "eta": ""}

    def hook(d):
        p = progress.setdefault(media_id, {"title": item["title"]})
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            p["percent"] = round(100 * d.get("downloaded_bytes", 0) / total, 1) if total else 0
            p["speed"] = d.get("_speed_str", "").strip()
            p["eta"] = d.get("_eta_str", "").strip()
        elif d["status"] == "finished":
            p["percent"] = 100

    opts = base_opts() | profile_opts(profile, source) | {"progress_hooks": [hook]}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(item["url"], download=False)
            if reason := skip_reason(source, info):
                db.run("UPDATE media SET status='skipped', error=?, title=?, upload_date=? WHERE id=?",
                       (reason, info.get("title") or item["title"], info.get("upload_date"), media_id))
                return "skipped"
            info = ydl.process_ie_result(info, download=True)
            path = final_path(info)
        size = Path(path).stat().st_size if path and Path(path).exists() else None
        if path and profile["write_nfo"]:
            write_nfo(Path(path), info, source)
        db.run("UPDATE media SET status='downloaded', filepath=?, filesize=?, title=?, upload_date=?, duration=?, "
               "downloaded=datetime('now') WHERE id=?",
               (path, size, info.get("title") or item["title"], info.get("upload_date"),
                int(info["duration"]) if info.get("duration") else None, media_id))
        db.log("info", f"Downloaded {info.get('title')} ({resolution_note(info, profile)})")
        return "downloaded"
    except Exception as exc:
        msg = clean_error(exc)
        login = needs_login(msg)
        if login:
            msg += " - sign in on the YouTube page."
        db.run("UPDATE media SET status='failed', error=? WHERE id=?", (msg[:2000], media_id))
        db.log("error", f"Download failed: {item['title']}: {msg[:300]}")
        return "login" if login and "Sign in to confirm" in msg else "failed"
    finally:
        progress.pop(media_id, None)


def resolution_note(info, profile) -> str:
    """e.g. '1080p vp9', flagged when below the profile's target resolution."""
    height = info.get("height")
    if profile["quality"] == "audio" or not height:
        return info.get("format_id") or "?"
    note = f"{height}p {(info.get('vcodec') or '').split('.')[0]}".strip()
    if profile["quality"].isdigit() and height < int(profile["quality"]):
        note += f", below {profile['quality']}p target - YouTube offered nothing higher"
    return note


def final_path(info) -> str | None:
    for d in info.get("requested_downloads") or []:
        if d.get("filepath"):
            return d["filepath"]
    return info.get("filepath") or info.get("_filename")


def write_nfo(path: Path, info, source):
    """Kodi/Jellyfin episode NFO next to the media file."""
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    date = info.get("upload_date") or ""
    aired = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else ""
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<episodedetails>\n'
        f"  <title>{e(info.get('title'))}</title>\n"
        f"  <showtitle>{e(source['name'])}</showtitle>\n"
        f"  <plot>{e(info.get('description'))}</plot>\n"
        f"  <aired>{aired}</aired>\n  <premiered>{aired}</premiered>\n"
        f"  <studio>{e(info.get('uploader') or info.get('channel'))}</studio>\n"
        f"  <runtime>{int((info.get('duration') or 0) // 60)}</runtime>\n"
        f"  <uniqueid type=\"youtube\" default=\"true\">{e(info.get('id'))}</uniqueid>\n"
        "</episodedetails>\n"
    )
    path.with_suffix(".nfo").write_text(body, encoding="utf-8")


def apply_retention():
    """Delete downloaded files older than each source's keep_days."""
    for s in db.q("SELECT * FROM sources WHERE keep_days > 0"):
        cutoff = (dt.datetime.utcnow() - dt.timedelta(days=s["keep_days"])).strftime("%Y-%m-%d %H:%M:%S")
        for m in db.q("SELECT * FROM media WHERE source_id=? AND status='downloaded' AND downloaded < ?",
                      (s["id"], cutoff)):
            delete_files(m)
            db.run("UPDATE media SET status='deleted', filepath=NULL WHERE id=?", (m["id"],))
            db.log("info", f"Retention: removed {m['title']}")


def delete_files(item):
    if not item["filepath"]:
        return
    p = Path(item["filepath"])
    stem = p.with_suffix("")
    for f in [p, *p.parent.glob(glob_escape(stem.name) + ".*")]:
        try:
            if f.is_file() and f.resolve().is_relative_to(config.DOWNLOADS_DIR.resolve()):
                f.unlink()
        except OSError:
            pass


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def verify_login() -> tuple[bool, str]:
    """Ask YouTube for the Watch Later list - only works when signed in."""
    opts = base_opts() | {"extract_flat": "in_playlist", "playlistend": 1, "skip_download": True}
    opts["logger"] = _Silent()
    t = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(FEEDS["watchlater"][1], download=False)
        return True, f"YouTube accepted the session ({time.time() - t:.1f}s)."
    except Exception as exc:
        return False, clean_error(exc)[:400]


class _Silent(Quiet):
    def warning(self, msg):
        pass

    def error(self, msg):
        pass
