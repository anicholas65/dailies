import time

import pytest

from app import browser, config, db, ytdl

COOKIES = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t{exp}\tLOGIN_INFO\tabc
.youtube.com\tTRUE\t/\tTRUE\t{exp}\tSAPISID\tdef
.google.com\tTRUE\t/\tTRUE\t{exp}\tSID\tghi
"""


def test_pages_render(client):
    for path in ["/", "/sources", "/media", "/profiles", "/youtube", "/settings", "/api/status"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_add_source_and_detail(client):
    r = client.post("/sources", data={"url": "https://www.youtube.com/@SomeChannel", "profile_id": "1",
                                      "interval_hours": "6", "max_items": "0"}, follow_redirects=False)
    assert r.status_code == 303
    s = db.one("SELECT * FROM sources")
    assert s["name"] == "SomeChannel" and s["kind"] == "channel"
    assert client.get(r.headers["location"]).status_code == 200


def test_edit_source_flags(client):
    client.post("/sources", data={"url": "https://www.youtube.com/@x"})
    client.post("/sources/1/edit", data={"name": "X", "url": "https://www.youtube.com/@x", "kind": "channel",
                                         "profile_id": "1", "interval_hours": "12", "include_shorts": "on",
                                         "enabled": "on"})
    s = db.one("SELECT * FROM sources WHERE id=1")
    assert (s["name"], s["interval_hours"], s["include_shorts"], s["auto_download"]) == ("X", 12, 1, 0)
    assert ytdl.source_urls(s) == ["https://www.youtube.com/@x/videos", "https://www.youtube.com/@x/shorts"]


def test_detect_kind():
    assert ytdl.detect_kind("https://www.youtube.com/playlist?list=PL123") == "playlist"
    assert ytdl.detect_kind("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "video"
    assert ytdl.detect_kind("https://youtu.be/dQw4w9WgXcQ") == "video"
    assert ytdl.detect_kind("https://www.youtube.com/@chan") == "channel"


def test_profile_opts(client):
    p = db.one("SELECT * FROM profiles WHERE quality='1080'")
    s = {"name": "My/Chan"}
    o = ytdl.profile_opts(p, s)
    assert "height<=1080" in o["format"]
    assert "/%(dailies_channel)s/Season" in o["outtmpl"]["default"]
    assert "My_Chan/" in ytdl.profile_opts(dict(p) | {"output": "{source}/%(title)s.%(ext)s"}, s)["outtmpl"]["default"]
    a = ytdl.profile_opts(db.one("SELECT * FROM profiles WHERE quality='audio'"), s)
    assert a["postprocessors"][0] == {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}


def test_auth_opts(client):
    db.set_setting("auth_mode", "none")
    assert "cookiefile" not in ytdl.base_opts() and "cookiesfrombrowser" not in ytdl.base_opts()
    config.COOKIES_FILE.write_text("x")
    db.set_setting("auth_mode", "file")
    assert ytdl.base_opts()["cookiefile"] == str(config.COOKIES_FILE)
    db.set_setting("auth_mode", "browser")
    (config.BROWSER_PROFILE / "Default").mkdir(parents=True)
    (config.BROWSER_PROFILE / "Default" / "Cookies").write_bytes(b"")
    assert ytdl.base_opts()["cookiesfrombrowser"][0] == "chromium"
    assert ytdl.base_opts()["cookiesfrombrowser"][2] == "BASICTEXT"


def test_cookie_upload_and_status(client):
    exp = int(time.time()) + 86400 * 30
    r = client.post("/youtube/cookies", files={"file": ("cookies.txt", COOKIES.format(exp=exp))},
                    follow_redirects=False)
    assert r.status_code == 303 and "ok=1" in r.headers["location"]
    st = browser.status()
    assert st["mode"] == "file" and st["signed_in"] and st["cookie_count"] == 3
    assert "signed in" in client.get("/youtube").text


def test_cookie_upload_rejects_junk(client):
    r = client.post("/youtube/cookies", files={"file": ("x.txt", "hello")}, follow_redirects=False)
    assert "ok=0" in r.headers["location"]
    assert not config.COOKIES_FILE.exists()


def _login(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "PASSWORD", "pw")
    client.post("/login", data={"password": "pw"})


def test_browser_start_reports_missing(client, monkeypatch):
    _login(client, monkeypatch)
    monkeypatch.setattr(browser, "available", lambda: {"chromium": False, "xvfb": True, "x11vnc": True, "novnc": True})
    r = client.post("/youtube/browser/start", follow_redirects=False)
    assert "chromium" in r.headers["location"]


def test_skip_reason():
    s = {"download_after": "2024-01-01", "title_filter": "", "title_exclude": "trailer", "min_duration": 60}
    assert ytdl.skip_reason(s, {"upload_date": "20231231", "title": "a"})
    assert ytdl.skip_reason(s, {"upload_date": "20240102", "title": "Big Trailer"})
    assert ytdl.skip_reason(s, {"upload_date": "20240102", "title": "ok", "duration": 30})
    assert ytdl.skip_reason(s, {"upload_date": "20240102", "title": "ok", "duration": 300}) is None


def test_media_actions_and_index(client, monkeypatch):
    client.post("/sources", data={"url": "https://www.youtube.com/playlist?list=PLx"})

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            return {"_type": "playlist", "entries": [
                {"id": "aaaaaaaaaaa", "title": "One", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
                {"id": "bbbbbbbbbbb", "title": "Two", "duration": 61.0}]}

    monkeypatch.setattr(ytdl.yt_dlp, "YoutubeDL", FakeYDL)
    assert ytdl.index_source(1) == 2
    assert ytdl.index_source(1) == 0  # no duplicates
    rows = db.q("SELECT * FROM media ORDER BY id")
    assert [r["status"] for r in rows] == ["queued", "queued"]  # auto_download defaults on
    db.run("UPDATE media SET status='pending'")
    assert rows[1]["url"] == "https://www.youtube.com/watch?v=bbbbbbbbbbb"
    client.post(f"/media/{rows[0]['id']}/queue")
    assert db.one("SELECT status FROM media WHERE id=?", (rows[0]["id"],))["status"] == "queued"
    client.post("/sources/1/queue-all")
    assert db.one("SELECT COUNT(*) n FROM media WHERE status='queued'")["n"] == 2
    assert "One" in client.get("/sources/1").text


def test_download_flow(client, monkeypatch, tmp_path):
    client.post("/sources", data={"url": "https://www.youtube.com/@c", "name": "Chan"})
    mid = db.run("INSERT INTO media(source_id, video_id, title, url, status) VALUES (1,'vid','T','https://y/v','queued')")
    out = config.DOWNLOADS_DIR / "Chan" / "T [vid].mp4"

    class FakeYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            return {"id": "vid", "title": "T", "upload_date": "20250101", "duration": 100}
        def process_ie_result(self, info, download=True):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x" * 10)
            return info | {"requested_downloads": [{"filepath": str(out)}]}

    monkeypatch.setattr(ytdl.yt_dlp, "YoutubeDL", FakeYDL)
    assert ytdl.download(mid) == "downloaded"
    m = db.one("SELECT * FROM media WHERE id=?", (mid,))
    assert m["status"] == "downloaded" and m["filesize"] == 10
    assert out.with_suffix(".nfo").read_text().count("<title>T</title>") == 1
    client.post(f"/media/{mid}/delete")
    assert not out.exists() and not out.with_suffix(".nfo").exists()


def test_password(client, monkeypatch):
    monkeypatch.setattr(config, "PASSWORD", "pw")
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/health").status_code == 200
    client.post("/login", data={"password": "nope"})
    assert client.get("/", follow_redirects=False).status_code == 303
    client.post("/login", data={"password": "pw"})
    assert client.get("/", follow_redirects=False).status_code == 200


def _fake_bin(d, name, body):
    p = d / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)


def test_browser_failure_is_reported(client, monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "Xvfb", 'echo "Fatal server error: no screens found" >&2; exit 1')
    _fake_bin(bindir, "x11vnc", "sleep 30")
    _fake_bin(bindir, "chromium", "sleep 30")
    novnc = tmp_path / "novnc"
    novnc.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("NOVNC_DIR", str(novnc))
    _login(client, monkeypatch)
    r = client.post("/youtube/browser/start", follow_redirects=False)
    from urllib.parse import unquote_plus
    loc = unquote_plus(r.headers["location"])
    assert "ok=0" in loc and "xvfb exited with code 1" in loc and "no screens found" in loc
    assert not browser.running()


def test_vnc_iframe_uses_absolute_ws_path(client, monkeypatch):
    # noVNC 1.6 resolves a relative path against /novnc/vnc.html, which misses the proxy route.
    monkeypatch.setattr(browser, "running", lambda: True)
    html = client.get("/youtube").text
    assert "path=/vnc/ws" in html
    assert any(r.path == "/vnc/ws" for r in client.app.routes)


def _fmt(fid, h, vcodec, ext="mp4"):
    return {"format_id": fid, "url": f"http://x/{fid}", "ext": ext, "height": h, "width": h * 16 // 9,
            "vcodec": vcodec, "acodec": "none", "protocol": "https", "tbr": h}


FAKE_FORMATS = [
    _fmt("136", 720, "avc1.4d401f"), _fmt("137", 1080, "avc1.640028"),
    _fmt("248", 1080, "vp09.00.40.08", "webm"), _fmt("271", 1440, "vp09.00.50.08", "webm"),
    _fmt("313", 2160, "vp09.00.51.08", "webm"),
    # legacy 360p muxed stream - old sort ranked its AAC audio above resolution
    _fmt("18", 360, "avc1.42001E") | {"acodec": "mp4a.40.2"},
    {"format_id": "140", "url": "http://x/140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
     "abr": 128, "protocol": "https"},
]


@pytest.mark.parametrize("quality,want", [("2160", "313"), ("1440", "271"), ("1080", "137"), ("720", "136")])
def test_quality_is_honoured_with_prefer_h264(client, quality, want):
    import yt_dlp
    from app import db, ytdl
    db.run("UPDATE profiles SET quality=?, prefer_h264=1 WHERE id=1", (quality,))
    p = db.one("SELECT * FROM profiles WHERE id=1")
    opts = ytdl.profile_opts(p, {"name": "S"}) | {"quiet": True, "simulate": True, "postprocessors": []}
    info = {"id": "x", "title": "t", "formats": [dict(f) for f in FAKE_FORMATS],
            "extractor": "youtube", "extractor_key": "Youtube", "webpage_url": "http://x"}
    r = yt_dlp.YoutubeDL(opts).process_ie_result(info, download=False)
    assert r["format_id"] == f"{want}+140"
    assert ytdl.resolution_note(r, p).startswith(f"{quality}p")


def test_presets_and_audio_formats(client):
    from app import db, ytdl
    names = {r["name"]: r for r in db.q("SELECT * FROM profiles")}
    for n in ("720p MP4", "1080p MP4", "2160p (4K) MP4", "Audio (MP3)", "Audio (WAV)"):
        assert n in names
    s = {"name": "S"}
    mp3 = ytdl.profile_opts(names["Audio (MP3)"], s)
    assert mp3["postprocessors"][0] == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
    assert any(pp["key"] == "EmbedThumbnail" for pp in mp3["postprocessors"])
    wav = ytdl.profile_opts(names["Audio (WAV)"], s)
    keys = [pp["key"] for pp in wav["postprocessors"]]
    assert wav["postprocessors"][0]["preferredcodec"] == "wav"
    assert "EmbedThumbnail" not in keys
    assert all(not pp.get("add_chapters") for pp in wav["postprocessors"])
    assert "height<=2160" in ytdl.profile_opts(names["2160p (4K) MP4"], s)["format"]
    assert "height<=720" in ytdl.profile_opts(names["720p MP4"], s)["format"]


def test_presets_added_once_to_existing_install(client):
    from app import db
    db.run("DELETE FROM profiles WHERE name='Audio (WAV)'")
    db.reset()
    db.init()
    assert not db.one("SELECT 1 FROM profiles WHERE name='Audio (WAV)'")


def test_profile_save_normalises_format(client):
    from app import db
    client.post("/profiles", data={"name": "A", "quality": "audio", "container": "mp4"})
    client.post("/profiles", data={"name": "V", "quality": "720", "container": "wav"})
    assert db.one("SELECT container FROM profiles WHERE name='A'")["container"] == "mp3"
    assert db.one("SELECT container FROM profiles WHERE name='V'")["container"] == "mp4"
    page = client.get("/profiles").text
    assert 'value="wav"' in page and "2160p (4K)" in page


def test_youtube_title():
    assert ytdl.youtube_title("channel", {"channel": "Cody", "title": "Cody - Videos"}) == "Cody"
    assert ytdl.youtube_title("channel", {"title": "Cody - Videos"}) == "Cody"
    assert ytdl.youtube_title("playlist", {"title": "My &amp; List"}) == "My & List"
    assert ytdl.youtube_title("video", {}) is None


def test_blank_name_is_auto_named_on_index(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ytdl.yt_dlp.YoutubeDL, "extract_info",
                        lambda self, url, download=False: calls.append(url) or
                        {"title": "How I Vibe Coded a Recipe App", "id": "9atd5lczG2k"})
    client.post("/sources", data={"url": "https://www.youtube.com/watch?v=9atd5lczG2k"})
    client.post("/sources", data={"url": "https://www.youtube.com/watch?v=abcdefghijk", "name": "Mine"})
    a, b = db.q("SELECT * FROM sources ORDER BY id")
    assert a["auto_name"] == 1 and b["auto_name"] == 0
    ytdl.index_source(a["id"])
    ytdl.index_source(b["id"])
    names = [r["name"] for r in db.q("SELECT name FROM sources ORDER BY id")]
    assert names == ["How I Vibe Coded a Recipe App", "Mine"]


def test_jellyfin_layout(client):
    from app import config
    p = db.one("SELECT * FROM profiles WHERE name='1080p MP4'")
    assert p["output"] == "" and ytdl.by_channel(p)
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('My: Chan', 'u', 'channel', ?)", (p["id"],))
    m1 = db.run("INSERT INTO media(source_id, video_id, title, url) VALUES (?, 'a', 't', 'u')", (sid,))
    m2 = db.run("INSERT INTO media(source_id, video_id, title, url) VALUES (?, 'b', 't', 'u')", (sid,))
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    d = {"upload_date": "20260523", "channel": "My: Chan"}
    assert ytdl.episode_fields(m2, src, d)["dailies_episode"] == 52301
    assert ytdl.episode_fields(m1, src, d) == {"series": "My: Chan", "dailies_channel": "My_ Chan",
                                               "dailies_season": 2026, "dailies_episode": 52302}
    assert ytdl.episode_fields(m2, src, d)["dailies_episode"] == 52301  # re-download keeps its number
    m3 = db.run("INSERT INTO media(source_id, video_id, title, url) VALUES (?, 'c', 't', 'u')", (sid,))
    assert ytdl.episode_fields(m3, src, d | {"upload_date": "20250523"})["dailies_episode"] == 52301  # other year
    o = ytdl.profile_opts(p, src)
    assert o["outtmpl"]["default"].endswith("%(dailies_channel)s/Season %(dailies_season)s/s%(dailies_season)se%(dailies_episode)06d - %(title)s [%(id)s].%(ext)s")
    assert o["outtmpl"]["thumbnail"].endswith("[%(id)s]-thumb.%(ext)s")
    a = ytdl.profile_opts(db.one("SELECT * FROM profiles WHERE name='Audio (MP3)'"), src)
    assert a["outtmpl"]["default"] == "/media/Music/%(dailies_channel)s/%(title)s [%(id)s].%(ext)s"  # MP3 folder
    from pathlib import Path
    assert ytdl.show_dir(Path("/d/Chan/Season 2026/x.mp4")) == Path("/d/Chan")


def test_videos_group_by_channel_across_sources(client):
    """Single-video and playlist sources from one publisher land in that publisher's show,
    with episode numbers unique across the show."""
    p = db.one("SELECT * FROM profiles WHERE name='1080p MP4'")
    s1 = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('Some Video', 'u', 'video', ?)", (p["id"],))
    s2 = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('A Playlist', 'u', 'playlist', ?)", (p["id"],))
    m1 = db.run("INSERT INTO media(source_id, video_id, title, url) VALUES (?, 'a', 't', 'u')", (s1,))
    m2 = db.run("INSERT INTO media(source_id, video_id, title, url) VALUES (?, 'b', 't', 'u')", (s2,))
    src1, src2 = (db.one("SELECT * FROM sources WHERE id=?", (i,)) for i in (s1, s2))
    d = {"upload_date": "20260523", "channel": "Greg &amp; Co"}
    f1, f2 = ytdl.episode_fields(m1, src1, d), ytdl.episode_fields(m2, src2, d)
    assert f1["series"] == f2["series"] == "Greg & Co" and f1["dailies_channel"] == "Greg & Co"
    assert (f1["dailies_episode"], f2["dailies_episode"]) == (52301, 52302)
    assert db.one("SELECT channel FROM media WHERE id=?", (m2,))["channel"] == "Greg & Co"
    # numbers assigned per source before 0.1.8 that now collide in one show get renumbered
    db.run("UPDATE media SET episode=52301 WHERE id=?", (m2,))
    assert ytdl.episode_fields(m2, src2, d)["dailies_episode"] == 52302
    # no channel in metadata: fall back to the source name
    assert ytdl.episode_fields(m1, src1, {"upload_date": "20260523"})["series"] == "Some Video"
    # a {source} template keeps one show per source
    custom = dict(p) | {"output": "{source}/%(title)s.%(ext)s"}
    assert not ytdl.by_channel(custom)
    assert ytdl.episode_fields(m2, src2, d, False)["series"] == "A Playlist"


def test_media_list_shows_thumbnail_and_channel(client):
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('Some Video', 'u', 'video', 1)")
    db.run("INSERT INTO media(source_id, video_id, title, url, channel) VALUES (?, 'abc123', 't', 'u', 'Greg')", (sid,))
    html = client.get("/media").text
    assert "https://i.ytimg.com/vi/abc123/mqdefault.jpg" in html
    assert "Greg in <a href=\"/sources/1\">Some Video</a>" in html


def test_legacy_template_migrates(client):
    from app import config
    db.run("UPDATE profiles SET output=? WHERE id=1", (config.LEGACY_TEMPLATE,))
    db.run("DELETE FROM settings WHERE key='jellyfin_layout'")
    db.reset()
    db.init()
    assert db.one("SELECT output FROM profiles WHERE id=1")["output"] == ""


def test_refresh_buttons_queue_and_missing_file(client, monkeypatch):
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('S', 'u', 'channel', 1)")
    mid = db.run("INSERT INTO media(source_id, video_id, title, url, status, filepath) "
                 "VALUES (?, 'a', 't', 'u', 'downloaded', '/nope/x.mp4')", (sid,))
    assert "Rename &amp; refresh files" in client.get(f"/sources/{sid}").text
    assert "Rename &amp; refresh all files" in client.get("/media").text
    client.post(f"/sources/{sid}/refresh")
    assert db.one("SELECT refresh FROM media WHERE id=?", (mid,))["refresh"] == 1
    assert "refreshing 1 file" in client.get("/").text
    assert ytdl.refresh(mid) == "missing"
    row = db.one("SELECT * FROM media WHERE id=?", (mid,))
    assert row["status"] == "deleted" and row["refresh"] == 0


def test_move_with_sidecars(client, tmp_path):
    from app import config
    d = config.DOWNLOADS_DIR / "Old"
    d.mkdir(parents=True)
    old = d / "2026-05-23 - T [a].mp4"
    for name in (old.name, "2026-05-23 - T [a].jpg", "2026-05-23 - T [a].nfo", "2026-05-23 - T [a].en.vtt"):
        (d / name).write_text("x")
    new = config.DOWNLOADS_DIR / "New" / "Season 2026" / "s2026e052301 - T [a].mp4"
    assert ytdl.move_with_sidecars(old, new) == new
    assert sorted(p.name for p in new.parent.iterdir()) == ["s2026e052301 - T [a].en.vtt", "s2026e052301 - T [a].mp4"]
    assert not d.exists()  # emptied folder pruned


def test_show_artwork_refetched_when_forced(client, tmp_path):
    """Rename & refresh re-downloads poster/fanart/tvshow.nfo; normal downloads leave them alone."""
    import io
    jpeg = lambda tag: b"\xff\xd8\xff" + tag  # noqa: E731

    class FakeYdl:
        def __init__(self, tag):
            self.tag = tag

        def extract_info(self, url, download=False, process=False):
            return {"description": "About", "thumbnails": [{"id": "avatar_uncropped", "url": "a"},
                                                           {"id": "banner_uncropped", "url": "b"}]}

        def urlopen(self, url):
            return io.BytesIO(jpeg(self.tag + url.encode()))

    src = {"name": "S", "kind": "video"}
    info = {"channel_url": "c", "series": "Greg", "channel": "Greg"}
    ytdl.write_show_files(tmp_path, src, info, FakeYdl(b"old"))
    assert (tmp_path / "poster.jpg").read_bytes() == jpeg(b"olda")
    assert "<title>Greg</title>" in (tmp_path / "tvshow.nfo").read_text()
    ytdl.write_show_files(tmp_path, src, info | {"series": "Greg 2"}, FakeYdl(b"new"))
    assert (tmp_path / "poster.jpg").read_bytes() == jpeg(b"olda")  # not forced: untouched
    ytdl.write_show_files(tmp_path, src, info | {"series": "Greg 2"}, FakeYdl(b"new"), force=True)
    assert (tmp_path / "poster.jpg").read_bytes() == jpeg(b"newa")
    assert (tmp_path / "fanart.jpg").read_bytes() == jpeg(b"newb")
    assert "<title>Greg 2</title>" in (tmp_path / "tvshow.nfo").read_text()


def _video(sid, vid, status="downloaded", title=None):
    from app import config
    path = None
    if status == "downloaded":
        season = config.DOWNLOADS_DIR / "Greg" / "Season 2026"
        season.mkdir(parents=True, exist_ok=True)
        f = season / f"s2026e0101{vid} - t [{vid}].mp4"
        for side in ("", "-thumb.jpg", ".nfo", ".en.srt"):
            (f.with_name(f.stem + side) if side else f).write_bytes(b"x")
        path = str(f)
    return db.run("INSERT INTO media(source_id, video_id, title, url, status, filepath) VALUES (?, ?, ?, 'u', ?, ?)",
                  (sid, vid, title or f"Video {vid}", status, path))


def test_delete_single_and_selected(client):
    from app import config
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('S', 'u', 'channel', 1)")
    show = config.DOWNLOADS_DIR / "Greg"
    a, b, c = _video(sid, "01"), _video(sid, "02"), _video(sid, "03", "pending")
    for n in ("tvshow.nfo", "poster.jpg", "fanart.jpg"):
        (show / n).write_bytes(b"x")
    page = client.get("/media").text
    assert 'name="ids" value="%d" form="bulk"' % a in page and "Delete all shown (3)" in page
    client.post(f"/media/{a}/delete")
    assert not list(show.glob("Season 2026/*[[]01[]]*"))  # video, thumb, nfo, subs all gone
    assert db.one("SELECT status, filepath FROM media WHERE id=?", (a,))["status"] == "deleted"
    assert (show / "Season 2026").is_dir() and (show / "poster.jpg").exists()  # b still there
    client.post("/media/bulk-delete", data={"ids": [str(b), str(c)]})
    assert {r["status"] for r in db.q("SELECT status FROM media")} == {"deleted"}
    assert not show.exists()  # last video gone: season folder and show artwork removed
    assert "Delete all shown" not in client.get("/media").text  # deleted are hidden by default
    assert "Video 01" in client.get("/media?status=deleted").text
    client.post(f"/media/{a}/queue")  # can still be downloaded again on purpose
    assert db.one("SELECT status FROM media WHERE id=?", (a,))["status"] == "queued"


def test_delete_all_shown_respects_filter(client):
    s1 = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('S1', 'u', 'channel', 1)")
    s2 = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('S2', 'u', 'channel', 1)")
    keep = _video(s1, "01", title="Keep me")
    gone = _video(s1, "02", title="Cats one")
    _video(s1, "03", "downloading", title="Cats busy")
    other = _video(s2, "04", "pending", title="Cats other source")
    client.post("/media/bulk-delete", data={"scope": "all", "status": "", "q": "Cats", "source_id": str(s1)})
    st = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM media")}
    assert st[gone] == "deleted" and st[keep] == "downloaded" and st[other] == "pending"
    assert "downloading" in st.values()  # an in-progress download is left alone
    assert "Delete all shown (1)" in client.get(f"/sources/{s2}").text
    client.post("/media/bulk-delete", data={"scope": "all", "status": "", "q": ""})
    assert set(st2 := {r["status"] for r in db.q("SELECT status FROM media")}) == {"deleted", "downloading"}, st2


def test_download_folders(client, tmp_path):
    from app import config, folders
    names = {f["name"]: f for f in folders.all_folders()}
    assert list(names) == ["YouTube", "Educational", "Movies", "Concerts", "MP3"]
    assert names["YouTube"]["path"] == str(config.DOWNLOADS_DIR) and names["Movies"]["layout"] == "movies"
    video = db.one("SELECT * FROM profiles WHERE name='1080p MP4'")
    mp3 = db.one("SELECT * FROM profiles WHERE name='Audio (MP3)'")
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('S', 'u', 'video', ?)", (video["id"],))
    src = lambda: db.one("SELECT * FROM sources WHERE id=?", (sid,))  # noqa: E731
    assert folders.for_source(src(), video)["name"] == "YouTube"  # automatic: video -> YouTube
    assert folders.for_source(src(), mp3)["name"] == "MP3"        # automatic: audio -> MP3
    # pick Movies on the source page
    movies = tmp_path / "Movies"
    client.post(f"/settings/folders/{names['Movies']['id']}", data={"name": "Movies", "path": str(movies), "layout": "movies"})
    client.post(f"/sources/{sid}/edit", data={"name": "S", "url": "u", "folder_id": str(names["Movies"]["id"])})
    assert folders.for_source(src(), video)["name"] == "Movies"
    o = ytdl.profile_opts(video, src())
    assert o["outtmpl"]["default"] == str(movies / config.MOVIE_TEMPLATE)
    client.post(f"/sources/{sid}/edit", data={"name": "S", "url": "u", "folder_id": ""})
    assert src()["folder_id"] is None
    # add / delete folders from Settings
    client.post("/settings/folders", data={"name": "Kids", "path": str(tmp_path / "Kids"), "layout": "shows"})
    kids = db.one("SELECT * FROM folders WHERE name='Kids'")
    page = client.get("/settings").text
    assert "Kids" in page and "Folder is created on the first download." in page
    assert "Use a full path" in client.post("/settings/folders", data={"name": "X", "path": "rel/x", "layout": "shows"},
                                            follow_redirects=True).text
    client.post(f"/settings/folders/{kids['id']}/delete")
    assert db.one("SELECT 1 FROM folders WHERE name='Kids'") is None
    # a folder still used by a source can't be deleted
    client.post(f"/sources/{sid}/edit", data={"name": "S", "url": "u", "folder_id": str(names["Concerts"]["id"])})
    client.post(f"/settings/folders/{names['Concerts']['id']}/delete")
    assert db.one("SELECT 1 FROM folders WHERE name='Concerts'")


def test_movie_files_and_cleanup(client, tmp_path):
    from app import folders
    db.run("UPDATE folders SET path=? WHERE name='Movies'", (str(tmp_path / "Movies"),))
    d = tmp_path / "Movies" / "Live at Wembley (2026) [abc]"
    d.mkdir(parents=True)
    f = d / "Live at Wembley (2026) [abc].mp4"
    f.write_bytes(b"v")
    f.with_name(f.stem + "-thumb.jpg").write_bytes(b"\xff\xd8\xffT")
    info = {"title": "Live at Wembley", "upload_date": "20260704", "id": "abc", "channel": "Band", "duration": 5400}
    ytdl.write_movie_files(f, info, None)
    nfo = f.with_suffix(".nfo").read_text()
    assert "<movie>" in nfo and "<year>2026</year>" in nfo and "<runtime>90</runtime>" in nfo
    assert (d / "poster.jpg").read_bytes() == b"\xff\xd8\xffT" == (d / "fanart.jpg").read_bytes()
    assert folders.root_of(f) == (tmp_path / "Movies").resolve()
    ytdl.delete_files({"filepath": str(f)})
    assert not d.exists() and (tmp_path / "Movies").exists()  # movie folder gone, library root kept
    ytdl.delete_files({"filepath": "/etc/passwd"})  # outside every folder: ignored


def test_folder_location_dropdown(client):
    from app import config, folders
    (config.DOWNLOADS_DIR / "Music" / "YouTube").mkdir(parents=True)
    (config.DOWNLOADS_DIR / ".hidden").mkdir()
    locs = folders.locations()[str(config.DOWNLOADS_DIR)]
    assert locs[:3] == [str(config.DOWNLOADS_DIR), str(config.DOWNLOADS_DIR / "Music"),
                        str(config.DOWNLOADS_DIR / "Music" / "YouTube")]
    assert not any(".hidden" in x for x in locs)
    page = client.get("/settings").text
    assert f'<option value="{config.DOWNLOADS_DIR / "Music"}"' in page and "Type a path…" in page
    assert '<option value="/media/Movies" selected>' in page  # current path kept even if not mapped
    # choose a location + new subfolder
    client.post("/settings/folders", data={"name": "Kids", "path": str(config.DOWNLOADS_DIR / "Music"),
                                           "subfolder": "Kids/", "layout": "music"})
    assert db.one("SELECT path FROM folders WHERE name='Kids'")["path"] == str(config.DOWNLOADS_DIR / "Music" / "Kids")
    # or type one
    client.post("/settings/folders", data={"name": "Docs", "path": "__custom", "path_custom": "/srv/docs/", "layout": "shows"})
    assert db.one("SELECT path FROM folders WHERE name='Docs'")["path"] == "/srv/docs"


def test_cross_site_requests_blocked(client):
    sid = db.run("INSERT INTO sources(name, url, kind, profile_id) VALUES ('S', 'u', 'channel', 1)")
    mid = db.run("INSERT INTO media(source_id, video_id, title, url, status) VALUES (?, 'a', 't', 'u', 'pending')", (sid,))
    evil = {"origin": "http://evil.example"}
    r = client.post("/media/bulk-delete", data={"scope": "all"}, headers=evil)
    assert r.status_code == 403
    assert db.one("SELECT status FROM media WHERE id=?", (mid,))["status"] == "pending"
    r = client.post("/media/bulk-delete", data={"ids": [str(mid)]}, headers={"referer": "http://evil.example/x"})
    assert r.status_code == 403
    # same site (as a browser sends it) and no Origin (curl) still work
    ok = client.post(f"/media/{mid}/skip", headers={"origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303 and db.one("SELECT status FROM media WHERE id=?", (mid,))["status"] == "skipped"
    proxied = client.post(f"/media/{mid}/queue", headers={"origin": "https://dailies.home", "x-forwarded-host": "dailies.home"},
                          follow_redirects=False)
    assert proxied.status_code == 303
    page = client.get("/")
    assert page.headers["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in page.headers["content-security-policy"]


def test_no_password_locks_google_features(client, monkeypatch):
    r = client.post("/youtube/browser/start", follow_redirects=False)
    assert "Set+a+web+UI+password" in r.headers["location"]
    r = client.get("/youtube/cookies.txt", follow_redirects=False)
    assert r.status_code == 303 and "password" in r.headers["location"]
    assert "No web UI password" in client.get("/").text
    _login(client, monkeypatch)
    assert "No web UI password" not in client.get("/").text


def test_login_throttle(client, monkeypatch):
    from app import config, main
    monkeypatch.setattr(config, "PASSWORD", "pw")
    main._failures.clear()
    for _ in range(5):
        assert "error=1" in client.post("/login", data={"password": "nope"}, follow_redirects=False).headers["location"]
    r = client.post("/login", data={"password": "pw"}, follow_redirects=False)
    assert "error=locked" in r.headers["location"]  # even the right password waits out the lock
    main._failures.clear()
    assert client.post("/login", data={"password": "pw"}, follow_redirects=False).headers["location"] == "/"


def test_folder_paths_guarded(client):
    from app import config
    for bad in ("/", "/etc", "/usr/lib", str(config.CONFIG_DIR), str(config.CONFIG_DIR / "browser"), "/proc/self"):
        r = client.post("/settings/folders", data={"name": "X", "path": "__custom", "path_custom": bad, "layout": "shows"},
                        follow_redirects=True)
        assert db.one("SELECT 1 FROM folders WHERE name='X'") is None, bad
        assert "system folder" in r.text or "Google sign-in data" in r.text, bad
