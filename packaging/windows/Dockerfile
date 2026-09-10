FROM node:24-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && apt-get update && apt-get install -y --no-install-recommends curl tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-chi-sim && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app/ /app/
RUN pip install --no-cache-dir -r requirements.lock.txt && pip install --no-cache-dir requests Pillow search1688api aiohttp beautifulsoup4 lxml && pip install --no-cache-dir --no-deps .
COPY legacy/ /opt/legacy/
RUN cd /opt/legacy && npm install --omit=dev --ignore-scripts && useradd -m -u 10001 flowhub && mkdir /data && chown -R flowhub:flowhub /data /app /opt/legacy
COPY entrypoint.py /app/entrypoint.py
USER flowhub
ENV FLOWHUB_DATA=/data/app FLOWHUB_LEGACY_ROOT=/data/legacy FLOWHUB_PORTABLE=1 FLOWHUB_FEISHU_CONFIG=/data/private/feishu.json FLOWHUB_BIND_HOST=0.0.0.0 FLOWHUB_PORT=38427 FLOWHUB_HTTPS=0 PYTHONUNBUFFERED=1 OZON_OCR_BACKEND=tesseract MAOZI_HTTP_BACKEND=native
CMD ["python","entrypoint.py"]
