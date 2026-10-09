FROM ghcr.io/ccawmiku/xhs-downloader:2.8-nas.2
USER root
RUN pip install --no-cache-dir Pillow==12.3.0
WORKDIR /app
COPY core/ ./core/
COPY workers/ ./workers/
ENV NAS_CORE_URL=http://console:14001 WORKER_PLATFORMS=xhs NAS_EXECUTION_DATA=/state/execution XHS_SETTINGS_PATH=/app/Volume/settings.json
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["python","-m","workers.engine"]
