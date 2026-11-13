# RR TRADING ENGINE — AI COMMAND CENTER

AI-assisted, risk-first automated trading platform: an Ubuntu application server (FastAPI, PostgreSQL,
Redis, Next.js, Claude) driving a MetaTrader 5 terminal on an on-premises Windows server through an
authenticated, idempotent execution bridge.

```
backend/         FastAPI API, worker, analysis, 215-strategy registry, signals, risk, execution, backtesting, Claude agent
mt5-connector/   Windows MT5 connector (HTTPS + HMAC, execution journal) + PowerShell service scripts
frontend/        Next.js hacker-style command center (18 modules)
docker/ nginx/   container images, TLS reverse proxy
scripts/         install, backup, restore, upgrade, healthcheck
docs/            ARCHITECTURE, INSTALL, RECOVERY, API, STRATEGIES, PHASE_REPORT
```

**Start here:** `docs/PHASE_REPORT.md` (status and what still needs your environment), then `docs/INSTALL.md`.

## Development

```bash
cd backend && pip install -r requirements-dev.txt
```

```bash
cd backend && python -m pytest -q
```

```bash
cd mt5-connector && python -m pytest -q
```

```bash
cd frontend && npm ci && npm run build
```

Backend tests need local PostgreSQL (database `rr_test`, user `rr`/`rr`) and Redis.

## Safety principles
* The Risk Engine validates every order; Claude can only confirm, downgrade or reject candidates.
* An order counts as executed only after MT5 confirms it. Unknown outcomes are reconciled, never resent.
* DEMO and LIVE are configured separately. LIVE requires administrator approval plus typed confirmation.
* Strategies that need data the broker doesn't provide are reported as unavailable, never simulated.
* Trading carries substantial risk of loss. Nothing in this software guarantees profitability.
