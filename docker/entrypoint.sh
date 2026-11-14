#!/bin/sh
set -e
# YouTube changes often: optionally pull the newest yt-dlp into /config on each start.
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
  echo "Updating yt-dlp..."
  if pip install --quiet --no-cache-dir --upgrade --target /config/.pylib "yt-dlp[default]" 2>/config/.pylib-update.log; then
    export PYTHONPATH="/config/.pylib${PYTHONPATH:+:$PYTHONPATH}"
  elif [ -d /config/.pylib/yt_dlp ]; then
    echo "yt-dlp update failed (offline?) - using last downloaded copy"
    export PYTHONPATH="/config/.pylib${PYTHONPATH:+:$PYTHONPATH}"
  else
    echo "yt-dlp update failed - using bundled version"
  fi
fi
mkdir -p /config/tmp
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8945}" --ws websockets
