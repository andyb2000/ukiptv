# Runs tvh_sync.py on a timer, next to (not inside) your Tvheadend container.
#
#   docker build -t ukiptv-sync .
#   docker run --rm -v /path/to/config:/config ukiptv-sync          # dry run
#   docker run -d --name ukiptv-sync --restart unless-stopped \
#     -v /path/to/config:/config ukiptv-sync --apply --interval 360
#
# /config must hold tvh_sync.conf and your desired channel list. Backups and
# the change log are written to /config/.tvh_sync.
FROM python:3.12-alpine

WORKDIR /app
COPY iptv_uk.py tvh_sync.py desired_channels.txt tvh_sync.conf.example ./

ENV TVH_SYNC_CONFIG=/config/tvh_sync.conf \
    PYTHONUNBUFFERED=1
VOLUME /config

ENTRYPOINT ["python3", "/app/tvh_sync.py"]
# Default is a safe dry run. For the long-running container pass:
#   --apply --interval 360      (sync every 6 hours)
CMD []
