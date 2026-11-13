# RR Trading One — Phase 3 Report

**Phase:** 3 — local infrastructure and process manager
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

The desktop supervisor now **really starts, stops, recovers and health-checks** the local engine stack. It does this
with fixed Docker Compose argument vectors, never a shell. Every item below was verified against a **real Docker
stack** in this environment, using the same supervisor code the app uses:
* PostgreSQL 16, Redis 7, the API, the Next.js UI and the loopback gateway;
* the monitor, analysis and learner services, plus the trading worker.

| Result | Value |
|---|---|
| End-to-end suite (real Docker stack) | **40 / 40 passed** (final clean run) |
| Backend suite (Python 3.12, lock), including the 215-strategy preservation test | **172 passed** (155 original + 12 role tests + 5 learner-entry tests) |
| Desktop core tests (Linux) | **67 passed**. Clippy `-D warnings` clean (all targets). Windows type-check clean. |
| Desktop core tests (real Windows, CI) | **60 of 61 passed, including the DPAPI round-trip**. The one failure was a CRLF test bug, fixed (see 6). |
| Phase 2 Windows build (CI, `windows-latest`) | **Passed**: app + unsigned NSIS package built (run 43406772448) |
| Engine console IPC isolation (real app) | **Proven at runtime**: 3 of 3 IPC calls refused; external navigation blocked |

## 2. What was built

### 2.1 Worker roles (decision D1) — `backend/app/workers/roles.py` (new; the original worker is unchanged)

| Role | What it runs | Guarantee |
|---|---|---|
| `monitor` | Heartbeat, startup checks, account/position sync, journal reconciliation, signal expiry, break-even/partial/trailing management, news | Never opens positions (tested) |
| `analysis` | `scan_once` (strategies, signals, Claude review) | Passes a no-op instead of the execution callback. **Never executes**, even with auto-trading on and a Claude-confirmed signal present (tested). |
| `trading` | The original `auto_execute_factory` only | **Ignores signals created before it started** (tested), so stale signals are never executed retroactively. All original gates still apply: `auto_trading`, a Claude-confirmed BUY/SELL, startup checks, the Risk Engine and the idempotent dedupe key. Executes exactly once (tested end-to-end with the real connector app and FakeMT5). |

**Locks**
* The roles refuse to run while the original monolithic worker holds its lock.
* Each role lock has an owner token; compose gives each container a stable `hostname`. A container that Docker killed or recreated can reclaim its own lock, but never someone else's.

**No connector yet**
* While no MT5 connector is registered, which is the normal state on a fresh install, the roles **wait instead of crashing**.
* The monitor publishes "no RR MT5 Connector agent is registered" to the bridge-health key. Analysis skips scanning, and the System Monitor shows the truth.

### 2.2 Learner entrypoint — `backend/app/learning_engine/entry.py` (new; the scheduler is unchanged)
It wraps the original `scheduler.main()`. At start-up it clears a leftover `rr:learner:lock` **only** if the owner record names this same container (see 6, defect 7).

### 2.3 Local stack — `infrastructure/` (new; the Ubuntu deployment files are untouched)

**Network**
* Only the `gateway` publishes a port: **`127.0.0.1:47810`**. This was verified with `docker port`.
* PostgreSQL and Redis are on an internal-only network. The trading worker has no egress network.

**Trading worker**
* It sits behind a compose **profile**, so a plain `up` never starts it.
* `restart: "no"`, so a crashed trading worker stays stopped.

**Secrets**
* Required through `${…:?}`: Compose refuses to run without them (verified, and checked in CI).
* Supplied only through the supervisor's child-process environment.

**Images and limits**
* The backend image installs from `requirements.lock` with `--no-deps`. The build succeeded, which proves the lock is complete.
* Resource limits: analysis 2 CPU / 2 GB, learner 2 CPU / 3 GB, monitor and trading 0.5 CPU.
* Log rotation: 10 MB × 5 per container.

**Gateway**
* `nginx.local.conf` has the same routing as the Ubuntu nginx, without TLS. That includes the MT5 relay WebSocket the connector will use in Phase 4.
* `RR_COOKIE_SECURE=false`: the browser never leaves 127.0.0.1. Login inside the app was verified.

### 2.4 Supervisor — `desktop/core/src/{runner,compose,secrets,supervisor,local,sys}.rs`

**Runner**
* Absolute program path plus argv; no shell.
* Hard timeout, after which the child is killed.
* Secrets travel only in the environment and are redacted from `Debug`.
* `CREATE_NO_WINDOW` on Windows.

**Compose**
* Project and file are always explicit.
* No code path can produce `down`, `rm`, `-v`, `--volumes`, `prune` or `kill` (tested).
* Stopping only the native connector can never become a nameless "stop everything" (tested).

**Secrets**
* CSPRNG-generated.
* DPAPI-encrypted on Windows (round-trip verified on real Windows in CI); 0600/0700 on dev machines.
* **Never regenerated over an unreadable or corrupt file.** Regenerating would lock out the database and make broker credentials unreadable.
* The folder ACL is restricted with `icacls` (fixed argv: SYSTEM, Administrators, current user).

**Execution semantics**
* Start plans stop at the first failure; stop plans are best-effort.
* **Migrations never run without a fresh, non-empty `pg_dump`.** The newest 10 pre-migration dumps are kept.
* Recovery restarts only failed services, **never the trading worker**.

**Health**
* Every service started in the plan must be healthy.
* The engine's `/api/health` must pass through the gateway whenever the API runs.
* A healthy container status cannot mask a failing engine (tested).

**Prerequisites:** Docker installed and running, Compose, WSL2 (Windows), the compose file, disk space, and the gateway port.

**App wiring**
* `run_action` executes plans, one at a time, off the UI thread.
* The new `service_status` command feeds the live Services table. It is granted only to the Control Centre window.
* `examples/supervise.rs` runs the same code from a terminal.

**Plan change:** Recovery is now `[RestartUnhealthy, VerifyHealth]`. A leading health check would have aborted exactly when recovery is needed.

## 3. End-to-end verification (real Docker stack, this environment)

| Area | Verified |
|---|---|
| Prerequisites | Docker 29.9.2, Compose 5.6.1, compose file, 13–15 GiB free, port free or "already ours" |
| Start All (from stopped) | All 7 steps OK. A real `PGDMP` backup before migrations. Every service running and healthy. **Trading worker not running.** |
| Stop All | One ordered graceful stop, trading worker first. **Both data volumes kept.** |
| Restart persistence | Admin created, then Stop All and Start All. **Login still works** (volume persisted). Wrong password returns 401. |
| Network | Exactly one published port, `127.0.0.1:47810`. PostgreSQL and Redis unpublished. Health and UI reachable through the gateway. |
| Trading worker | Refused without the phrase and with a wrong-case phrase (nothing started). Starts with the phrase and takes its lock. Stops gracefully and **releases** the lock. Other services untouched. **Audit of the supervisor log: across every run, only `start_trading_worker` ever started it.** |
| Daemon restart | `unless-stopped` services came back by themselves; **the trading worker did not**. |
| Recovery | Restarted only the crash-looping `analysis` service, then verified everything healthy |
| No connector registered | Monitor published the true state; analysis waited; no crash loops |
| Log redaction | Supervisor log written; **no secret value** in it |
| Real desktop app (Xvfb) | Live service states and healthy probes shown. **Trading Console opened the existing Next.js UI and login worked inside the app.** The Command Center showed honest empty states (DEMO, AUTO OFF, connector down, no symbols). |
| Console isolation | From the console window, `get_overview`, `run_action(stop_all)` and `service_status` were each refused: *"not allowed on window "console"… allowed on: windows: "main", URL: local"*. Navigation to `https://example.com` was blocked. |
| UI under production CSP | Failed and skipped steps render correctly. An injected `<img onerror>` / `<script>` in tool output rendered as text. No console errors. |

## 4. Files

**Created**
* `backend/app/workers/roles.py`
* `backend/app/learning_engine/entry.py`
* `backend/tests/test_roles.py`
* `backend/tests/test_learner_entry.py`
* `infrastructure/{compose.yml,nginx.local.conf,backend.Dockerfile,README.md}`
* `desktop/core/src/{runner,compose,secrets,supervisor,local,sys}.rs`
* `desktop/core/examples/supervise.rs`
* `.gitattributes`
* this report

**Modified**
* `desktop/core/src/{actions,lib}.rs`: Gateway service, Recovery plan, compose cross-check tests
* `desktop/core/Cargo.toml`: base64, getrandom, libc (unix), windows-sys (Windows)
* `desktop/src-tauri/{src/lib.rs,build.rs,capabilities/main.json}`: real execution, `service_status`
* `desktop/ui/{app.js,index.html}`: step outcomes, live service states, busy state
* `desktop/README.md`
* `.github/workflows/backend.yml`: compose safety checks, `infrastructure/**` trigger

**Preserved unchanged**
* Everything under `backend/app` except the two new files, including `workers/main.py` and `learning_engine/scheduler.py`.
* All migrations, `frontend/`, `mt5-connector/`, `windows-connector/`.
* The Ubuntu `docker-compose.yml`, `nginx/` and `docker/`.
* All 215 strategies (preservation test passes).

**Database changes:** none. **Migrations:** none (the existing 0001–0003 are applied by the stack).

## 5. Commands executed (main ones)

```
cargo test -p rr-one-core --locked                         → 67 passed
cargo clippy --workspace --all-targets --locked -D warnings → clean
cargo check -p rr-one-core --all-targets --target x86_64-pc-windows-msvc → clean
pytest -q (Python 3.12.3, requirements.lock)               → 172 passed
docker build (backend + frontend, sandbox CA variant)      → OK
supervise prereqs | start_all | stop_all | recovery | system_health | start/stop_trading_worker
scratchpad e2e.sh (40 checks)                              → 40 passed
real app under Xvfb + xdotool: Control Centre, console, login, IPC-isolation probe
```

## 6. Defects found by verification (all fixed)

| # | Defect | Found by | Fix |
|---|---|---|---|
| 1 | The POSIX path check depended on the host OS, so 2 core tests failed on Windows | Windows CI | Textual check |
| 2 | The supervisor's `nginx` service name did not match compose's `gateway`. **Start All would have failed in production.** | Supervisor unit tests | Renamed, plus a test that cross-checks every service name against the real compose file |
| 3 | Roles crashed when no MT5 connector was registered (the normal fresh-install state) | Real stack | Wait and retry; publish a "disconnected" heartbeat; rebuild the bridge if the registered agent changes |
| 4 | Role lock leaked when a role crashed after acquiring it | Real stack | Lock handling inside `try/finally`, plus owner tokens |
| 5 | The lock code assumed `bytes`, but the app's Redis client returns `str`. Unit tests used a raw client and missed it. | Real stack | Normalised; tests now use the app's own client, plus a bytes-client test |
| 6 | A recreated container (new image) got a new hostname and could not reclaim its own lock | Real stack | Stable compose `hostname` per role (tested in Rust against the compose file) |
| 7 | **Pre-existing in the original engine:** the learner's lock outlives a killed learner (300 s TTL, 60 s sleep, longer than the 30 s grace). Every Stop All then caused up to 5 minutes of learner crash-looping. | Real stack | `learning_engine/entry.py` wrapper; the original scheduler is unchanged |
| 8 | The CRLF checkout on Windows broke a text test. It would also convert container-mounted configs to CRLF. | Windows CI | Test normalises line endings; `.gitattributes` forces LF for container files |
| 9 | Recovery began with a health check that would abort exactly when recovery is needed | Design review | Plan is now `[RestartUnhealthy, VerifyHealth]` |

## 7. Not verified here / known limitations

* **Docker Desktop + WSL2 on Windows has not been exercised.** Every stack run used Linux Docker. Not exercised on Windows:
  * the `wsl.exe --status` check;
  * the `icacls` ACL step;
  * `GetDiskFreeSpaceExW`;
  * `CREATE_NO_WINDOW`.

  Those are type-checked for Windows only, except DPAPI, which ran on Windows CI.
* **Images are built from source on first start.** That requires the source tree and internet access. Prebuilt images for the installer are Phase 16.
* **Sandbox-only build variant.** In this environment, images were built from generated variants of the committed Dockerfiles that trust this sandbox's TLS proxy CA. The committed Dockerfiles do not contain this.
* **Learner jobs that need the broker** (trade sync, portfolio, experiments on broker data) log errors until an MT5 connector is registered in Phase 4. The learner itself stays healthy.
* **Stacks started outside the app.** `unless-stopped` services (not the trading worker) restart when Docker Desktop starts, even if the app is closed. This keeps position protection running, and is documented.
* **The MT5 connector** is not yet supervised; its steps report "skipped" (Phase 4).
* **The safety-state panel** still shows shipped defaults. Live engine values come in Phase 5.

## 8. Security implications
* **No shell anywhere in the supervisor.** Argv is fixed and tested, and nothing can remove containers or volumes.
* **Secrets** are DPAPI-encrypted at rest, kept in an ACL-restricted folder, passed only via the environment, redacted in reports and logs (tested), and never regenerated destructively.
* **Network exposure:** the stack is reachable only on loopback, and the databases are not published (verified and CI-checked).
* **Separation of duties at process level:** analysis cannot execute and monitor cannot open positions (tested). The trading worker is started only by its explicit, phrase-gated action (tested plus log audit).
* **Console window:** it has no IPC access, verified at runtime.
* **Open item from Phase 1 (gap 7.4):** the learner still holds an order-capable bridge client. It is scheduled with the Phase 4 connector work, which introduces per-role bridge scoping.

## 9. Rollback
* **Desktop side:** revert to commit `2d70d6e` (end of Phase 2). The shell returns to plan-only mode.
* **Stack:** `supervise stop_all` (or `docker compose -p rr-trading-one stop`). This keeps the volumes.
* **Backend additions:** delete the four new backend files. Nothing else references them.
* **Database:** none of this changes the schema. Pre-migration dumps are in `ProgramData\RR Trading One\backups`.

## 10. Next phase
**Phase 4 — native Windows MT5 connector integration:**
* register and supervise the existing `windows-connector` as a user-session process over the loopback relay;
* show the MT5 Manager in the app;
* explicit terminal and account profile selection;
* per-role bridge scoping (gap 7.4).
