FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock.txt ./
COPY flowhub ./flowhub
COPY web ./web
COPY plugins ./plugins
COPY flowhub_plugins ./flowhub_plugins
COPY vendor/compareBot ./vendor/compareBot
COPY bridges ./bridges
RUN pip install --no-cache-dir -r requirements.lock.txt && pip install --no-cache-dir --no-deps . && pip install --no-cache-dir "./vendor/compareBot[search1688,dinov2]" "python-dotenv>=1.2,<2" && useradd -m -u 10001 flowhub && mkdir /app/data && chown -R flowhub:flowhub /app
USER flowhub
ENV FLOWHUB_DATA=/app/data FLOWHUB_HTTPS=1 PYTHONUNBUFFERED=1
CMD ["uvicorn", "flowhub.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
