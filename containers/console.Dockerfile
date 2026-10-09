FROM node:22.14.0-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ARG APP_VERSION=4.0.3
ENV APP_VERSION=${APP_VERSION}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NAS_CORE_DATA=/state/core NAS_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
COPY core/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY core/ ./core/
COPY --from=frontend /build/frontend/dist ./frontend/dist/
EXPOSE 14001
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:14001/healthz',timeout=3)"
CMD ["uvicorn","core.app:app","--host","0.0.0.0","--port","14001","--no-access-log"]
