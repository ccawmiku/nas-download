FROM node:22.14.0-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ARG APP_VERSION=v3.0.0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/opt/nas-auto \
    APP_VERSION=${APP_VERSION}

WORKDIR /opt/nas-auto

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY _integrated/requirements.txt /tmp/integrated-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/integrated-requirements.txt

COPY _src/XHS-Downloader-NAS-main /opt/nas-auto/xhs
COPY _src/x-auto-download-nas-main /opt/nas-auto/x
COPY _src/pixiv-auto-download-nas-main /opt/nas-auto/pixiv
COPY _src/douyin-f2-auto-main /opt/nas-auto/douyin
COPY _common/nas_auto_common /opt/nas-auto/nas_auto_common
COPY _integrated/nas_auto /opt/nas-auto/nas_auto
COPY _integrated/integrated_server.py /opt/nas-auto/integrated_server.py
COPY --from=frontend-build /app/frontend/dist /opt/nas-auto/frontend/dist

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:14001/healthz', timeout=3)"

EXPOSE 14001

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/nas-auto/integrated_server.py"]
