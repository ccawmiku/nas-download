# Collection libraries and their pinned F2 dependency remain isolated from the console.
ARG ENGINE_BASE=ghcr.io/ccawmiku/nas-download:v3.0.3
FROM ${ENGINE_BASE}
USER root
RUN pip uninstall -y f2 \
    && pip install --no-cache-dir \
        "f2 @ git+https://github.com/Johnserf-Seed/f2.git@f6be8c0ffba9a127075bbeafe4838716650b6325" \
        Pillow==13.0.0 gallery-dl==1.32.19 yt-dlp==2026.10.26 pillow-heif==1.8.1
WORKDIR /opt/nas-auto
COPY core/ ./core/
COPY workers/ ./workers/
COPY _src/x-auto-download-nas-main/ ./x/
COPY _src/pixiv-auto-download-nas-main/ ./pixiv/
COPY _src/douyin-f2-auto-main/ ./douyin/
ENV NAS_CORE_URL=http://console:14001 NAS_EXECUTION_DATA=/state/execution WORKER_PLATFORMS=x,pixiv,douyin
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["python","-m","workers.engine"]
