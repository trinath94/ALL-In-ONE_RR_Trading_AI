# RR Trading One — local infrastructure

The local stack for **LOCAL WINDOWS MODE** (Docker Desktop + WSL2). It is managed by the desktop supervisor
(`desktop/core/src/supervisor.rs`), not by hand. The original Ubuntu deployment (`../docker-compose.yml`, `../nginx/`,
`../docker/backend.Dockerfile`) is unchanged and not used here.

| File | Purpose |
|---|---|
| `compose.yml` | The stack. Project name `rr-trading-one`. |
| `nginx.local.conf` | Loopback HTTP gateway: same routing as `nginx/nginx.conf`, without TLS |
| `backend.Dockerfile` | Backend image built from `backend/requirements.lock`, the pinned versions the 215-strategy preservation suite was verified with |

## Services

| Service | Role | Network | Notes |
|---|---|---|---|
| `postgres` 16, `redis` 7 | State, locks, relay | internal only | Data in named volumes `rr-trading-one_pgdata` / `_redisdata` |
| `migrate` | `alembic upgrade head` + seed | internal | One-shot. The supervisor takes a `pg_dump` first and refuses to migrate without it. |
| `api` | FastAPI engine | internal + egress | |
| `frontend` | Existing Next.js UI | internal | |
| `gateway` | nginx | internal + gateway | **The only published port: `127.0.0.1:47810`** |
| `monitor` | Heartbeat, startup checks, sync, reconciliation, position protection | internal + egress | `app.workers.roles monitor` |
| `analysis` | Scanner + signals (+ Claude review) | internal + egress | Never executes |
| `learner` | Part 25/26 learning jobs | internal + egress | Original scheduler via `app.learning_engine.entry` |
| `trading-worker` | Auto-execution only | internal | Behind the `trading` **profile**: a plain `up` never starts it. `restart: "no"`. |

**Role-scoped MT5 credentials (Phase 4).** `analysis` and `learner` run with `RR_BRIDGE_SCOPE=read` and **without**
`RR_ENCRYPTION_KEY`.
* They sign MT5 requests with the agent's **read-only** secret, which is encrypted with `RR_BRIDGE_READ_KEY`.
* The original connector on the PC refuses that secret for `POST /executions`.
* Without the main encryption key they cannot decrypt the trading secret either.
* `api`, `monitor` and `trading-worker` keep full scope.

The monitor, analysis and trading roles replace the original single worker loop (decision D1). They reuse the
same engine functions, so auto-execution still requires all of the following:
* `auto_trading` is on;
* Claude confirmed the BUY/SELL;
* the startup checks passed;
* the Risk Engine approved;
* the idempotent dedupe key is new.

The trading role also ignores signals created before it started.

## Secrets

`compose.yml` requires `RR_ONE_DB_PASSWORD`, `RR_ONE_REDIS_PASSWORD`, `RR_ONE_JWT_SECRET`, `RR_ONE_ENCRYPTION_KEY`,
`RR_ONE_BRIDGE_SECRET` and `RR_ONE_BRIDGE_READ_KEY`. Each uses `${…:?}` interpolation, so Compose refuses to run if any is missing.

The supervisor generates them once and stores them DPAPI-encrypted in
`C:\ProgramData\RR Trading One\secrets\stack.secrets`, with a restricted ACL. It passes them to Docker Compose only
through the child process environment. They are never written to an `.env` file and never appear on a command line.

**Back up `stack.secrets` with the database.** The Fernet key inside it is needed to read stored broker credentials.

## Running by hand (diagnostics only)

The supported path is the desktop app, or the equivalent CLI that runs the same code:

```bash
cd desktop && cargo run -p rr-one-core --example supervise -- prereqs
```

```bash
cd desktop && cargo run -p rr-one-core --example supervise -- start_all
```

The supervisor never runs `down`, `rm` or `-v`. Stopping keeps containers and volumes.
