# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GUARDRAIL_DB_PATH=/data/guardrail.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY guardrail ./guardrail
# Behind a TLS-intercepting proxy, pass its CA as a build secret (never stored in the image):
#   docker build --secret id=ca,src=/path/to/ca.pem .
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 guardrail && mkdir /data && chown guardrail /data

USER guardrail
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import json, urllib.request; h = json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz')); raise SystemExit(h['status'] != 'ok')"
CMD ["guardrail", "serve", "--host", "0.0.0.0", "--port", "8000"]
