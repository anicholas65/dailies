FROM python:3.13-slim

# Chromium + virtual display + VNC (for in-app YouTube sign-in), ffmpeg for muxing.
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc ffmpeg tini \
      fonts-liberation fonts-noto-color-emoji ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config DOWNLOADS_DIR=/downloads HOME=/config PORT=8945 \
    NOVNC_DIR=/usr/share/novnc YTDLP_AUTO_UPDATE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY extension ./extension
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config /downloads && chown 99:100 /config /downloads
# Unraid default nobody:users
USER 99:100
VOLUME ["/config", "/downloads"]
EXPOSE 8945
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
