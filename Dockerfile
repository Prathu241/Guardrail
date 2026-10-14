FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GUARDRAIL_DB_PATH=/data/guardrail.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY guardrail ./guardrail
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 guardrail && mkdir /data && chown guardrail /data

USER guardrail
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["guardrail", "serve", "--host", "0.0.0.0", "--port", "8000"]
