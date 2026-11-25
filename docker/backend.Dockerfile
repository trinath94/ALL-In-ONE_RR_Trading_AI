FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# IANA tz data comes from the `tzdata` wheel, backups run in the postgres container. git: the Claude Code workspace
# (worktree per task, patch review), used only by the claude-code service.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home /app rr && mkdir -p /workspace/home && chown -R rr /workspace
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/ /app/
# Claude Code workspace only: the simulated MT5 connector the engine's DEMO end-to-end tests run against (source, no
# secrets). Never imported by the engine itself.
COPY mt5-connector/connector /opt/rr-source/mt5-connector/connector
COPY mt5-connector/tests /opt/rr-source/mt5-connector/tests
COPY mt5-connector/windows/gen_cert.py /opt/rr-source/mt5-connector/windows/gen_cert.py
USER rr
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
