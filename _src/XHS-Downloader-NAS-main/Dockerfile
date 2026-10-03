FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ARG APP_VERSION=v1.3.1
ENV APP_VERSION=${APP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

WORKDIR /app

COPY xhs_auto_worker.py liked_extractor.js config.example.json README.md ./

RUN python -m pip install --no-cache-dir playwright==1.56.0 \
    && python -c "from playwright.async_api import async_playwright; print('playwright ok')"

CMD ["python", "/app/xhs_auto_worker.py", "--config", "/config/config.json"]
