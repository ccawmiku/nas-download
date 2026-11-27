FROM python:3.12-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/JoeanAmier/XHS-Downloader.git /xhs-src \
    && git -C /xhs-src checkout e4fc8a61630bbe59c5002e1461302ff90eb1172b \
    && rm -rf /xhs-src/.git
RUN pip install --no-cache-dir --prefix="/install" -r /xhs-src/requirements.txt Pillow==13.0.0

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --from=builder /xhs-src/locale /app/locale
COPY --from=builder /xhs-src/source /app/source
COPY --from=builder /xhs-src/static /app/static
COPY --from=builder /xhs-src/LICENSE /app/LICENSE
COPY --from=builder /xhs-src/main.py /app/main.py
VOLUME /app/Volume
COPY core/ ./core/
COPY workers/ ./workers/
ENV NAS_CORE_URL=http://console:14001 WORKER_PLATFORMS=xhs NAS_EXECUTION_DATA=/state/execution XHS_SETTINGS_PATH=/app/Volume/settings.json
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["python","-m","workers.engine"]
