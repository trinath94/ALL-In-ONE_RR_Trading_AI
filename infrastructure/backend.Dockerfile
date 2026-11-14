# RR Trading One backend image: identical to docker/backend.Dockerfile except that dependencies come from the
# pinned lock (backend/requirements.lock), the versions the 215-strategy preservation suite was verified with.
FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# libgomp1: OpenMP runtime required by LightGBM (and used by XGBoost). Without it, importing lightgbm fails with
# OSError and model training could not run in the installed engine (found in the Phase 11 end-to-end run).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home /app rr
WORKDIR /app
COPY backend/requirements.lock .
RUN pip install --no-deps -r requirements.lock
COPY backend/ /app/
USER rr
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
