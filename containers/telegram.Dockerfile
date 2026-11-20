ARG TELEGRAM_BASE=ghcr.io/ccawmiku/nas-download-telegram:v3.0.3
FROM ${TELEGRAM_BASE}
USER root
COPY services/telegram/requirements.txt /tmp/telegram-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/telegram-requirements.txt && rm /tmp/telegram-requirements.txt
WORKDIR /app
COPY services/telegram/app/ ./app/
COPY core/ ./core/
COPY workers/ ./workers/
ENV NAS_CORE_URL=http://console:14001 NAS_EXECUTION_DATA=/state/execution PUBLIC_BASE_PATH=/api/telegram PANEL_AUTH_REQUIRED=false
USER bot
CMD ["python","-m","app.serve"]
