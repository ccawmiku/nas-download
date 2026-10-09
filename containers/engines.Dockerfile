# Collection libraries and their fixed F2 dependency remain isolated from the console.
ARG ENGINE_BASE=ghcr.io/ccawmiku/nas-download:v3.0.3
FROM ${ENGINE_BASE}
USER root
RUN pip install --no-cache-dir Pillow==12.3.0
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
