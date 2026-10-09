# Same tested Intel driver / QSV runtime as the NAS Jellyfin installation.
FROM jellyfin/jellyfin:10.11.8
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pil util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY core/ ./core/
COPY workers/ ./workers/
ENV NAS_CORE_URL=http://console:14001 FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe PYTHONUNBUFFERED=1
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["python3","-m","workers.media_worker"]
