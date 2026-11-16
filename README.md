# Dailies

Self-hosted YouTube archiver for Unraid, in the spirit of Pinchflat — with **YouTube sign-in built in**.

- Subscribe to channels, playlists, single videos, or your own **Watch Later** / **Liked** lists
- Scheduled checks, download queue with live progress, retries, auto-pause on YouTube's bot check
- Profiles: quality cap (4K…480p, audio-only), MP4/MKV, prefer H.264, subtitles, SponsorBlock, chapters, thumbnails, Jellyfin/Plex/Kodi `.nfo`
- Download folders (Settings): YouTube, Educational, Movies, Concerts, MP3 out of the box, each with a Jellyfin layout (Shows, Movies or Music); every source picks one, audio-only sources default to MP3
- Files are organised by publisher: `Channel/Season YYYY/sYYYYeMMDDnn - Title [id].mp4`, whatever source (channel, playlist or single video) they came from. The video thumbnail is embedded as cover art and saved as `-thumb.jpg`; the channel avatar becomes the show poster (fitted on a 2:3 poster so Jellyfin doesn't crop it) and the banner the fanart. If the channel art can't be fetched, a video thumbnail stands in and the real art is retried once a day
- Per-source filters: newest N, skip uploads before a date, title include/exclude regex, minimum length, Shorts/livestream toggles, auto-delete after N days
- yt-dlp + deno (YouTube JS challenges) + ffmpeg; yt-dlp self-updates on container start

## Signing in to YouTube

yt-dlp can't log in with a username/password any more — YouTube only accepts browser cookies.

### Chrome extension (recommended)

The `extension/` folder is a Chrome extension that works like Internet Download Manager. It runs in the Chrome you already use, so there's nothing to sign in to on the server:

- It sends Dailies your **youtube.com** cookies (never google.com ones) and resends them whenever Chrome rotates them, plus every 30 minutes.
- A **Download** button on YouTube video, playlist and channel pages (and a right-click "Download with Dailies") queues the page on Dailies with the format (profile) and folder you pick.
- On the YouTube home feed, search, subscriptions and sidebars, hovering a video tile shows a **Dailies** chip: add that video's **whole channel as a series**, its playlist, or just the video, without opening it. Adding a channel or playlist that's already in Dailies just re-checks it for new videos.

Set up: YouTube page → **Download the extension**, unzip, `chrome://extensions` → Developer mode → **Load unpacked**. In the extension's settings enter the Dailies address and a key from **Create key** (needs `DAILIES_PASSWORD`). Only a hash of the key is stored; **Revoke key** cuts the extension off. Each yt-dlp run uses a private copy of `cookies.txt`, so a run can't overwrite a fresher sign-in the extension just sent.

### Built-in Chromium

Dailies also ships a real **Chromium inside the container**, shown in the web UI over noVNC:

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

## Security

- **Set `DAILIES_PASSWORD`.** Without it, anyone on your network can use Dailies; the sign-in browser and
  cookie export are disabled until a password is set, because they'd expose your Google account.
- Requests from other websites are refused (Origin/Referer check on every form and the browser
  WebSocket), pages can't be framed by other sites, and 5 wrong passwords lock that address for 10 minutes.
- Exported cookies.txt holds youtube.com cookies only, never google.com, and no copy is kept on disk.
- Download folders can't be system folders or `/config`.
- Behind a reverse proxy, forward the original host (`X-Forwarded-Host`) so the same-site check passes.
- Don't expose port 8945 to the internet; use a VPN (e.g. Tailscale/WireGuard) for remote access.
