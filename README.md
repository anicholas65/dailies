# Dailies

Self-hosted YouTube archiver for Unraid, in the spirit of Pinchflat — with **YouTube sign-in built in**.

- Subscribe to channels, playlists, single videos, or your own **Watch Later** / **Liked** lists
- Scheduled checks, download queue with live progress, retries, auto-pause on YouTube's bot check
- Profiles: quality cap (4K…480p, audio-only), MP4/MKV, prefer H.264, subtitles, SponsorBlock, chapters, thumbnails, Jellyfin/Plex/Kodi `.nfo`
- Download folders (Settings): YouTube, Educational, Movies, Concerts, MP3 out of the box, each with a Jellyfin layout (Shows, Movies or Music); every source picks one, audio-only sources default to MP3
- Files are organised by publisher: `Channel/Season YYYY/sYYYYeMMDDnn - Title [id].mp4`, whatever source (channel, playlist or single video) they came from. The video thumbnail is embedded as cover art and saved as `-thumb.jpg`; the channel avatar/banner become the show poster/fanart
- Per-source filters: newest N, skip uploads before a date, title include/exclude regex, minimum length, Shorts/livestream toggles, auto-delete after N days
- yt-dlp + deno (YouTube JS challenges) + ffmpeg; yt-dlp self-updates on container start

## Signing in to YouTube

yt-dlp can't log in with a username/password any more — YouTube only accepts browser cookies. Dailies ships a real **Chromium inside the container**, shown in the web UI over noVNC:

1. **YouTube → Open browser.** Sign in to Google as normal (2FA works). Your password goes only to Google.
2. **Done — close browser & save.** Chromium shuts down cleanly and its profile stays in `/config/browser`.
3. yt-dlp reads cookies straight from that profile (`--cookies-from-browser chromium+basictext:/config/browser`) on every request, so there's no stale cookie export to manage.

Alternatives on the same page: upload a `cookies.txt`, export one from the built-in browser, or run anonymously. **Test with YouTube** checks the session against your Watch Later list. If downloads hit YouTube's "confirm you're not a bot" wall, the queue pauses itself and resumes after you sign in.

The VNC server listens on localhost only; the browser is reachable solely through the app's authenticated WebSocket. Set `DAILIES_PASSWORD` — anyone who can open the UI can drive a browser signed in to your Google account.

## Unraid

Template: `unraid/dailies.xml` (copy into `/boot/config/plugins/dockerMan/templates-user/`, then Docker → Add Container → Dailies).

| Path / variable | Purpose |
|---|---|
| `/config` | database, Chromium profile, cookies |
| `/downloads` | media (point Jellyfin at it) |
| `/media` | optional: media share for the Movies / Concerts / Educational / MP3 folders (e.g. `/mnt/user/Data/Media`) |
| `DAILIES_PASSWORD` | web UI password (recommended) |
| `YTDLP_AUTO_UPDATE` | `1` = pip-update yt-dlp into `/config/.pylib` each start |
| `DAILIES_SCREEN` | sign-in browser resolution, default `1280x800` |

Runs as 99:100 on port 8945. The template sets `--shm-size=512m` for Chromium.

## Dev

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest -q
CONFIG_DIR=./data/config DOWNLOADS_DIR=./data/dl .venv/bin/uvicorn app.main:app --port 8945 --reload
```

Push to `main` → GitHub Actions runs tests and publishes `ghcr.io/anicholas65/dailies:latest`.
