# RR Trading One: technical architecture

## 1. Principle

RR Trading One is a **shell and supervisor** around the existing RR Trading AI Engine:
* The engine's trading, risk, learning, intelligence, backtesting and MT5 code runs unchanged in its own processes. Nothing was rewritten or duplicated.
* The 215 original strategies are preserved and verified by hash.
* The Windows application starts and supervises the engine, connects it to the PC's MetaTrader 5 terminal, and adds what a single-PC installation needs: installation, first run, backups, migration and security.

## 2. Components

```
Windows PC
├── RR Trading One (Tauri 2 desktop app)                      desktop/
│   ├── main window: Control Centre (local HTML/JS, strict CSP; the only window with IPC)   desktop/ui
│   ├── console window: the engine's Next.js UI at http://127.0.0.1:47810 (no IPC)
│   └── rr-one-core: supervisor (allowlisted actions → fixed Docker Compose argv), secrets (DPAPI),
│       backups (AES-256-GCM), migration, settings, health probes                     desktop/core
├── RR MT5 Connector (headless, user session)                 windows-connector/ (+ mt5-connector/ code)
│   └── MetaTrader 5 terminal (the user's broker account)
└── Docker Desktop (WSL2): compose project "rr-trading-one"   infrastructure/compose.yml
    ├── gateway (nginx, 127.0.0.1:47810 only)
    ├── api (FastAPI)      frontend (Next.js)
    ├── monitor  (heartbeat, reconciliation, position protection)   ┐
    ├── analysis (scanner, agents, signals, AI review)              │ roles of the original worker,
    ├── trading-worker (executes confirmed signals; profile "trading", never auto-started) │ one role per container
    ├── learner  (scheduled learning jobs, each in its own process) ┘
    ├── postgres 16.6 (volume pgdata)       redis 7.4 (volume redisdata)
    └── migrate (Alembic + seed, run before the engine starts)
```

## 3. Key flows

**Connector link (outbound relay).**
1. The engine issues a one-time token.
2. The desktop passes it to the connector on stdin.
3. The connector registers a device key pair and **dials out** to the gateway over a WebSocket.

The engine never opens a connection to the PC's terminal. Each request is HMAC-signed with a nonce. Analysis and the
learner hold only a **read-only** credential.

**Scan → signal → execution.**
1. The analysis role scans enabled symbols (budgeted, once per closed bar, only strategies valid for the timeframe).
2. It builds the consensus and calibrated probability, then the advisory fusion and AI review. The fused decision is never more aggressive than the engine's.
3. Signals are stored.
4. The trading worker executes only new, confirmed signals created after auto trading was switched on.

Execution goes through the Risk Engine and an idempotent journal, and the connector checks the account identity on
every order.

**Learning.** The learner imports deals from the broker and attributes trades (DEMO and LIVE kept apart). It trains,
validates (walk-forward, compared with production) and signs models. An administrator promotes them; adaptive
profiles run in shadow first.

**Desktop ↔ engine.** A local supervisor API (HMAC, ±60 s, single-use nonce) reports the live safety state and
performs the one-time first-run steps (administrator, DEMO account).

## 4. Data

* **PostgreSQL:** schema managed by Alembic (0001–0011; Phase 3 onward only adds), in a Docker named volume that the application never removes.
* **Redis:** locks, the relay, nonces, rate limits, resource reports.
* **Backups:** `pg_dump` files, encrypted (`.rrbk`) in `C:\ProgramData\RR Trading One\backups`.
* **Restore:** a schema reset and the SQL in one transaction.
* **Migration from Ubuntu:** a logical dump with a manifest, verified in a scratch database, content-checked after the migrations (see the migration manual).

## 5. Design decisions

| Decision | Why |
|---|---|
| **Docker Desktop for the engine**, a native process for the connector | The engine is a Linux stack (PostgreSQL, Redis, Python with numeric libraries) that already ran in containers. The MT5 terminal's API exists only on Windows, in the user's session. |
| **One role per container** (monitor, analysis, learner, trading worker) | Independent limits and priorities, and the trading worker can be stopped and started on its own |
| **Closed action list, fixed argv** | Nothing from a page, a user or an AI can become a command |
| **Loopback only** | The engine is never reachable from the network |
| **Additive changes** | Original files, tests and strategies are preserved; every new behaviour is behind its own module and tests |

## 6. Where to read more

* The phase reports `docs/RR_TRADING_ONE_PHASE*_REPORT.md`, one per phase, with what was verified and how.
* `desktop/README.md`, `infrastructure/README.md`, `docs/WINDOWS_CONNECTOR.md`.
* [Security](SECURITY.md), the [test matrix](../TEST_MATRIX.md).
