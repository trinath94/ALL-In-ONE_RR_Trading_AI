# RR Trading One backend image: identical to docker/backend.Dockerfile except that dependencies come from the
# pinned lock (backend/requirements.lock), the versions the 215-strategy preservation suite was verified with.
FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# libgomp1: OpenMP runtime required by LightGBM (and used by XGBoost). Without it, importing lightgbm fails with
# OSError and model training could not run in the installed engine (found in the Phase 11 end-to-end run).
# git: the Claude Code workspace (worktree per task, patch review). Only the claude-code service uses it.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 git && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home /app rr && mkdir -p /workspace/home && chown -R rr /workspace
WORKDIR /app
COPY backend/requirements.lock .
RUN pip install --no-deps -r requirements.lock
COPY backend/ /app/
# Claude Code workspace only: the simulated MT5 connector the engine's DEMO end-to-end tests run against (source, no
# secrets). Never imported by the engine itself.
COPY mt5-connector/connector /opt/rr-source/mt5-connector/connector
COPY mt5-connector/tests /opt/rr-source/mt5-connector/tests
COPY mt5-connector/windows/gen_cert.py /opt/rr-source/mt5-connector/windows/gen_cert.py
USER rr
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
