#!/usr/bin/env bash
# Zero-surprise upgrade: backup -> build -> migrate -> rolling restart -> health check.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/backup.sh
git pull --ff-only
docker compose build
docker compose run --rm migrate
docker compose up -d --no-deps api frontend
docker compose up -d --no-deps worker   # worker re-runs startup reconciliation on boot
docker compose up -d nginx
./scripts/healthcheck.sh
