# Use the NAS cached Python runtime for isolated local previews.
ARG CONSOLE_BASE=ghcr.io/ccawmiku/nas-download-telegram:v3.0.3
FROM ${CONSOLE_BASE}
USER root
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1
COPY core/ ./core/
COPY frontend/dist/ ./frontend/dist/
ENV NAS_CORE_DATA=/state/core NAS_FRONTEND_DIST=/app/frontend/dist NAS_CORE_URL=http://console:14001
EXPOSE 14001
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:14001/healthz',timeout=3)"
CMD ["uvicorn","core.app:app","--host","0.0.0.0","--port","14001","--no-access-log"]
