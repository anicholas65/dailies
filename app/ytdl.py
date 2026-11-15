"""yt-dlp integration: option building, indexing sources, downloading media."""
import datetime as dt
import html
import re
import shutil
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
VIDEO_CONTAINERS = ("mp4", "mkv", "webm")
AUDIO_CODECS = ("mp3", "wav", "m4a", "opus")
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
        if any(k in msg for k in ("nsig", "Falling back", "falling back", "No title found", "--paths is ignored")):
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


def profile_template(profile) -> str:
    return profile["output"] or (config.AUDIO_TEMPLATE if profile["quality"] == "audio" else config.VIDEO_TEMPLATE)


def by_channel(profile) -> bool:
    """Shows are named after the publisher (else after the Dailies source)."""
    return "{channel}" in profile_template(profile)


def profile_opts(profile, source) -> dict:
    audio = profile["quality"] == "audio"
    folder = config.DOWNLOADS_DIR
    template = profile_template(profile).replace("{source}", safe_name(source["name"]))
    template = template.replace("{channel}", "%(dailies_channel)s")  # filled in by episode_fields
    opts = {
        "format": QUALITY_FORMATS.get(profile["quality"], QUALITY_FORMATS["best"]),
        "outtmpl": {"default": str(folder / template),
                    # Jellyfin/Kodi episode image: <video name>-thumb.jpg (audio: <name>.jpg, cover art)
                    "thumbnail": str(folder / template.rsplit(".", 1)[0]) + ("." if audio else "-thumb.") + "%(ext)s"},
        "paths": {"temp": str(config.CONFIG_DIR / "tmp")},
        "windowsfilenames": True,
        "writethumbnail": bool(profile["embed_thumbnail"] or profile["write_thumbnail"]),
        "postprocessors": [],
    }
    pps = opts["postprocessors"]
    if audio:
        codec = profile["container"] if profile["container"] in AUDIO_CODECS else "mp3"
        # "0" = best VBR for mp3 (V0); ignored by lossless wav
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "0"})
    else:
        opts["merge_output_format"] = profile["container"] if profile["container"] in VIDEO_CONTAINERS else "mp4"
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
    wav = audio and profile["container"] == "wav"  # WAV holds basic tags only: no chapters or cover art
    chapters = bool(profile["embed_chapters"]) and not wav
    if profile["embed_metadata"] or chapters:
        pps.append({"key": "FFmpegMetadata", "add_metadata": bool(profile["embed_metadata"]),
                    "add_chapters": chapters})
    if opts["writethumbnail"]:
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if profile["embed_thumbnail"] and not wav and (audio or opts.get("merge_output_format") != "webm"):
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


def youtube_title(kind: str, info) -> str | None:
    """Channel name for channels, else the playlist/video title."""
    if kind == "channel":
        title = info.get("channel") or info.get("uploader")
        if not title:  # tab pages are titled "Name - Videos"
            title = re.sub(r"\s+-\s+(Videos|Shorts|Live|Streams)$", "", info.get("title") or "")
    else:
        title = info.get("title")
    return (html.unescape(title).strip() or None) if title else None


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
                if source["auto_name"] and (title := youtube_title(source["kind"], info)):
                    db.run("UPDATE sources SET name=?, auto_name=0 WHERE id=?", (title, source_id))
                    source = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
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
            info.update(episode_fields(media_id, source, info, by_channel(profile)))
            info = ydl.process_ie_result(info, download=True)
            path = final_path(info)
            if path and profile["write_nfo"] and profile["quality"] != "audio":
                write_nfo(Path(path), info, source)
                write_show_files(show_dir(Path(path)), source, info, ydl)
        size = Path(path).stat().st_size if path and Path(path).exists() else None
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


# ---- refresh existing files (rename + metadata, no re-download) ---------------

SIDECARS_REGENERATED = (".nfo", ".jpg", ".jpeg", ".webp", ".png", "-thumb.jpg", "-thumb.webp", "-thumb.png")


def best_thumbnail(info) -> str | None:
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return info.get("thumbnail")
    best = max(thumbs, key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)))
    return best["url"]


_shows_refreshed: set[Path] = set()  # show folders whose artwork this refresh run already re-fetched


def refresh(media_id: int) -> str:
    try:
        return _refresh(media_id)
    finally:
        if not db.one("SELECT 1 FROM media WHERE refresh=1 AND status='downloaded'"):
            _shows_refreshed.clear()  # run finished: the next one fetches show artwork again


def _refresh(media_id: int) -> str:
    """Move a downloaded file to its profile's current name/layout and rewrite thumbnail, NFO, show
    artwork and embedded tags from fresh YouTube metadata. The media itself is not re-downloaded."""
    item = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    source = db.one("SELECT * FROM sources WHERE id=?", (item["source_id"],))
    profile = db.one("SELECT * FROM profiles WHERE id=?", (source["profile_id"],))
    db.run("UPDATE media SET refresh=0 WHERE id=?", (media_id,))
    old = Path(item["filepath"]) if item["filepath"] else None
    if not old or not old.is_file():
        db.run("UPDATE media SET status='deleted', filepath=NULL, error='File missing on refresh - queue it to "
               "download again' WHERE id=?", (media_id,))
        db.log("warn", f"Refresh: file missing for {item['title']}")
        return "missing"
    audio = profile["quality"] == "audio"
    popts = profile_opts(profile, source)
    opts = base_opts() | {"outtmpl": popts["outtmpl"], "windowsfilenames": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(item["url"], download=False, process=False)
            info.update(episode_fields(media_id, source, info, by_channel(profile)))
            info["ext"] = old.suffix[1:]
            new = Path(ydl.prepare_filename(info))
            new = move_with_sidecars(old, new)
            thumb = new.with_name(new.stem + ("" if audio else "-thumb") + ".jpg")
            if (profile["write_thumbnail"] or profile["embed_thumbnail"]) and (url := best_thumbnail(info)):
                thumb.write_bytes(to_jpeg(ydl.urlopen(url).read()))
            if profile["write_nfo"] and not audio:
                write_nfo(new, info, source)
                show = show_dir(new)
                write_show_files(show, source, info, ydl, force=show not in _shows_refreshed)
                _shows_refreshed.add(show)
        embed_tags(new, info, profile, thumb if thumb.exists() else None)
        if thumb.exists() and not profile["write_thumbnail"]:
            thumb.unlink()
    except Exception as exc:
        msg = clean_error(exc)
        db.log("error", f"Refresh failed: {item['title']}: {msg[:300]}")
        return "login" if needs_login(msg) and "Sign in to confirm" in msg else "failed"
    db.run("UPDATE media SET filepath=?, filesize=?, title=?, upload_date=?, error=NULL WHERE id=?",
           (str(new), new.stat().st_size, info.get("title") or item["title"], info.get("upload_date"), media_id))
    db.log("info", f"Refreshed {info.get('title')} -> {new.relative_to(config.DOWNLOADS_DIR)}")
    return "refreshed"


def move_with_sidecars(old: Path, new: Path) -> Path:
    """Rename the media file; keep subtitles alongside it, drop thumbnails/NFOs (they get rewritten)."""
    new.parent.mkdir(parents=True, exist_ok=True)
    for f in list(old.parent.glob(glob_escape(old.stem) + "*")):
        tail = f.name[len(old.stem):]
        if f == old or not f.is_file() or tail[:1] not in (".", "-"):
            continue
        if tail.lower() in SIDECARS_REGENERATED:
            f.unlink()
        else:
            shutil.move(f, new.with_name(new.stem + tail))
    if new != old:
        shutil.move(old, new)
        prune_empty_dirs(old.parent)
    return new


def prune_empty_dirs(folder: Path):
    root = config.DOWNLOADS_DIR.resolve()
    folder = folder.resolve()
    while folder != root and folder.is_relative_to(root):
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            break
        folder = folder.parent


def embed_tags(path: Path, info, profile, thumb: Path | None):
    """Rewrite embedded metadata/cover via yt-dlp's own postprocessors (stream copy, no re-encode)."""
    wav = path.suffix.lower() == ".wav"
    pps = []
    if profile["embed_metadata"]:
        # chapters are left as they are: SponsorBlock may already have cut the file
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False})
    if profile["embed_thumbnail"] and thumb and not wav and path.suffix.lower() != ".webm":
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})
    if not pps:
        return
    info = dict(info, filepath=str(path), __files_to_move={}, chapters=None,
                thumbnails=[{"id": "0", "url": str(thumb), "filepath": str(thumb)}] if thumb else [])
    with yt_dlp.YoutubeDL({"logger": Quiet(), "quiet": True, "postprocessors": pps}) as ydl:
        ydl.post_process(str(path), info)


def final_path(info) -> str | None:
    for d in info.get("requested_downloads") or []:
        if d.get("filepath"):
            return d["filepath"]
    return info.get("filepath") or info.get("_filename")


def channel_name(info, source) -> str:
    name = info.get("channel") or info.get("uploader") or source["name"]
    return html.unescape(name).strip() or source["name"]


def episode_fields(media_id: int, source, info, channel_shows: bool = True) -> dict:
    """Season = upload year, episode = MMDDnn (nn = 01, 02... in the order videos from that day are
    downloaded), unique within the show (channel, or source when the layout uses {source}).
    Assigned once and stored, so re-downloads keep their number.
    Not season_number/episode_number: MP4 stores those as 8-bit, so 2026 would embed as 234."""
    date = info.get("upload_date") or dt.date.today().strftime("%Y%m%d")
    base = int(date[4:8]) * 100
    channel = channel_name(info, source)
    scope, key = ("channel=?", channel) if channel_shows else ("source_id=?", source["id"])
    with db.tx() as conn:
        ep = conn.execute("SELECT episode FROM media WHERE id=?", (media_id,)).fetchone()["episode"]
        clash = ep and conn.execute(f"SELECT 1 FROM media WHERE {scope} AND episode=? AND "
                                    "substr(upload_date, 1, 4)=? AND id!=?",
                                    (key, ep, date[:4], media_id)).fetchone()
        if not ep or ep // 100 != base // 100 or clash:
            last = conn.execute(f"SELECT MAX(episode) FROM media WHERE {scope} AND episode BETWEEN ? AND ? "
                                "AND substr(upload_date, 1, 4)=? AND id!=?",
                                (key, base, base + 99, date[:4], media_id)).fetchone()[0]
            ep = min((last or base) + 1, base + 99)
        conn.execute("UPDATE media SET upload_date=?, episode=?, channel=? WHERE id=?", (date, ep, channel, media_id))
    return {"series": channel if channel_shows else source["name"], "dailies_channel": safe_name(channel),
            "dailies_season": int(date[:4]), "dailies_episode": ep}


def show_dir(path: Path) -> Path:
    return path.parent.parent if re.fullmatch(r"Season \d+", path.parent.name) else path.parent


def _aired(date: str | None) -> str:
    return f"{date[:4]}-{date[4:6]}-{date[6:]}" if date and len(date) == 8 else ""


def write_nfo(path: Path, info, source):
    """Jellyfin/Kodi episode NFO next to the media file."""
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    aired = _aired(info.get("upload_date"))
    tags = "".join(f"  <tag>{e(t)}</tag>\n" for t in (info.get("tags") or [])[:20])
    genres = "".join(f"  <genre>{e(g)}</genre>\n" for g in info.get("categories") or [])
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<episodedetails>\n'
        f"  <title>{e(info.get('title'))}</title>\n"
        f"  <showtitle>{e(info.get('series') or source['name'])}</showtitle>\n"
        f"  <season>{e(info.get('dailies_season'))}</season>\n"
        f"  <episode>{e(info.get('dailies_episode'))}</episode>\n"
        f"  <plot>{e(info.get('description'))}</plot>\n"
        f"  <aired>{aired}</aired>\n  <premiered>{aired}</premiered>\n"
        f"  <studio>{e(info.get('uploader') or info.get('channel'))}</studio>\n"
        f"  <runtime>{int((info.get('duration') or 0) // 60)}</runtime>\n"
        f"{genres}{tags}"
        f"  <uniqueid type=\"youtube\" default=\"true\">{e(info.get('id'))}</uniqueid>\n"
        "</episodedetails>\n"
    )
    path.with_suffix(".nfo").write_text(body, encoding="utf-8")


def write_show_files(folder: Path, source, info, ydl, force: bool = False):
    """tvshow.nfo + poster.jpg (channel avatar) + fanart.jpg (channel banner), once per show folder.
    force: fetch them again and overwrite (existing art is kept if the fetch fails)."""
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    art = {"poster.jpg": "avatar_uncropped", "fanart.jpg": "banner_uncropped"}
    missing = [n for n in art if force or not (folder / n).exists()]
    if (folder / "tvshow.nfo").exists() and not missing:
        return
    channel = {}
    if url := info.get("channel_url"):
        try:
            channel = ydl.extract_info(url, download=False, process=False) or {}
        except Exception as exc:  # artwork is nice-to-have
            db.log("warn", f"Channel artwork for {source['name']}: {clean_error(exc)[:200]}")
    if force or not (folder / "tvshow.nfo").exists():
        (folder / "tvshow.nfo").write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<tvshow>\n'
            f"  <title>{e(info.get('series') or source['name'])}</title>\n"
            f"  <plot>{e(channel.get('description'))}</plot>\n"
            f"  <studio>{e(info.get('channel') or info.get('uploader'))}</studio>\n"
            f"  <genre>YouTube</genre>\n"
            f"  <uniqueid type=\"youtube\" default=\"true\">{e(info.get('channel_id'))}</uniqueid>\n"
            "</tvshow>\n", encoding="utf-8")
    thumbs = {t.get("id"): t.get("url") for t in channel.get("thumbnails") or []}
    for name in missing:
        # no channel art (or it failed to load): the video's own thumbnail stands in
        if not (url := thumbs.get(art[name]) or best_thumbnail(info)):
            continue
        try:
            data = ydl.urlopen(url).read()
            (folder / name).write_bytes(to_jpeg(data))
        except Exception as exc:
            db.log("warn", f"{name} for {source['name']}: {clean_error(exc)[:200]}")


def to_jpeg(data: bytes) -> bytes:
    """YouTube art may be webp/png; Jellyfin wants what the name says."""
    if data[:3] == b"\xff\xd8\xff":
        return data
    import subprocess
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg",
                          "-q:v", "2", "pipe:1"], input=data, capture_output=True, check=True)
    return out.stdout


def apply_retention():
    """Delete downloaded files older than each source's keep_days."""
    for s in db.q("SELECT * FROM sources WHERE keep_days > 0"):
        cutoff = (dt.datetime.utcnow() - dt.timedelta(days=s["keep_days"])).strftime("%Y-%m-%d %H:%M:%S")
        for m in db.q("SELECT * FROM media WHERE source_id=? AND status='downloaded' AND downloaded < ?",
                      (s["id"], cutoff)):
            delete_files(m)
            db.run("UPDATE media SET status='deleted', filepath=NULL WHERE id=?", (m["id"],))
            db.log("info", f"Retention: removed {m['title']}")


SHOW_FILES = {"tvshow.nfo", "poster.jpg", "fanart.jpg"}


def delete_files(item):
    """Delete the media file and its sidecars (thumbnail, NFO, subtitles). A season folder left empty
    is removed; so is a show folder holding nothing but its own artwork."""
    if not item["filepath"]:
        return
    p = Path(item["filepath"])
    if not p.resolve().is_relative_to(config.DOWNLOADS_DIR.resolve()):
        return
    stem = p.with_suffix("")
    for f in [p, *p.parent.glob(glob_escape(stem.name) + ".*"), *p.parent.glob(glob_escape(stem.name) + "-thumb.*")]:
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass
    show = show_dir(p)
    prune_empty_dirs(p.parent)
    try:
        if show.is_dir() and show.resolve() != config.DOWNLOADS_DIR.resolve() and \
                {f.name for f in show.iterdir()} <= SHOW_FILES:
            for f in show.iterdir():
                f.unlink()
            prune_empty_dirs(show)
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
