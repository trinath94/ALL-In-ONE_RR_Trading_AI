# RR Trading One — Phase 1 Report

**Phase:** 1 — repository audit, baseline verification, and the Windows migration and integration plan
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`
**Source:** `RR-Trading-AI-Engine-main.zip`, SHA-256 `685909b2af005599e8802fb8b54c2b9f733f68e60f8bc9454e1554a5ebd3e998`
**Production code modified:** none

This report states only what was inspected, executed and measured here. Anything that needs a Windows PC, a real MetaTrader 5 terminal, a broker account or an AI provider key is marked as unverified.

---

## 1. Summary

| Item | Result |
|---|---|
| Baseline imported | Commit `5c6c26b`: the zip committed verbatim (`diff -r` against the extracted zip: identical). |
| 215-strategy baseline | **Verified.** 215 of 215 IDs present, 0 code-hash mismatches, 0 config mismatches, category counts exact. |
| Backend tests (PostgreSQL 16 + Redis 7, real migrations) | **155 passed**, 0 failed (77 s) |
| `mt5-connector` tests | **10 passed** |
| `windows-connector` tests (Qt offscreen, Linux) | **24 passed** |
| Frontend `next build` (strict TypeScript) | **Passed**, 24 routes, all statically prerendered |
| Part 25/26 intelligence | Present and wired in: 10 learner jobs, 19 tables (migration `0002`) and a governed model lifecycle. Not validated on real broker data. |

**Overall:** the engine is a sound foundation for RR Trading One. Most of what the specification asks for already exists and should be reused. Section 7 lists the real gaps; section 8 lists the decisions I need from you before Phase 2.

### Important context about "original" strategies
`docs/ARCHITECTURE.md` and `docs/PHASE_REPORT.md` in the source both say the repository was **empty** when the engine was first built. The 215 strategies were written in that earlier session to match a count specification. They were then frozen as the baseline at commit `0293bb3`.

They are the baseline the specification tells me to preserve, and I treat them that way. But they are not externally sourced or battle-tested strategies, and no performance evidence exists for any of them on real broker data. The UI and documentation must not present them as proven.

---

## 2. Files inspected

All 241 tracked files were listed and sized. These were read in full or in their relevant sections:

* **Docs:** `README.md`, `docs/PHASE_REPORT.md`, `docs/ARCHITECTURE.md`, `docs/PART25_26_REPORT.md`, `docs/PART25_26_BASELINE.md`, `docs/WINDOWS_CONNECTOR.md`, `windows-connector/RELEASE_NOTES.md`
* **Strategies:** `backend/app/strategies/registry.py`, `backend/app/strategies/base.py`, `backend/tests/baseline/strategy_inventory.json` (via the audit script), `backend/tests/test_preservation.py`
* **Scanner and symbols:** `backend/app/scanner/service.py` (full), `backend/app/services/trading.py` (symbol discovery, identity), `backend/app/core/config.py`, `backend/app/db/models.py` (`Symbol`, `TradingState`)
* **Worker and execution:** `backend/app/workers/main.py` (main loop, position management), `backend/app/execution/bridge.py` (interface), `backend/app/risk/engine.py` (policies)
* **AI:** `backend/app/agents/claude_agent.py` (`enforce_boundaries`)
* **Learning:** `backend/app/learning_engine/scheduler.py` (jobs), `backend/app/learning_engine/models.py` (lifecycle, artifact loading), `backend/app/strategies/experimental/lifecycle.py`
* **Backtesting:** `backend/app/backtesting/engine.py` (function inventory)
* **Infrastructure:** `docker-compose.yml`, `docker/backend.Dockerfile`, `scripts/backup.sh`, `.gitignore`
* **MT5 connectors:** `mt5-connector/connector/adapter.py`, `windows-connector/rr_connector/agent.py` (engine URL rules)
* **Frontend:** `frontend/next.config.mjs`, `frontend/lib/api.ts`, `frontend/package.json`, the `frontend/components/Shell.tsx` navigation
* **Tests:** `backend/tests/conftest.py`, plus a listing of all 133 backend test functions

## 3. Files created or modified

| File | Change |
|---|---|
| All 241 baseline files | Added unmodified (baseline import commit) |
| `docs/RR_TRADING_ONE_PHASE1_REPORT.md` | **New**: this report |

No production code, tests, migrations or configuration were changed. The original files are preserved byte-for-byte.

## 4. Commands executed and test results

The environment was a Linux container with PostgreSQL 16 (service) and Redis (service), test database `rr_test` and user `rr`. Python 3.13 ran in a virtualenv; Node 22.

```
pip install -r backend/requirements-dev.txt
pip install -r windows-connector/requirements.txt   # needed: backend tests import rr_connector (see 7.9)
apt-get install libegl1 libgl1 libxkbcommon0 ...     # needed: Qt offscreen tests
cd backend && python -m pytest -q                    → 155 passed, 679 warnings in 76.83s
cd mt5-connector && python -m pytest -q              → 10 passed
cd windows-connector && QT_QPA_PLATFORM=offscreen python -m pytest -q → 24 passed
cd frontend && npm ci && npm run build               → compiled, types OK, 24/24 static pages
python -I inventory_audit.py <repo>                  → independent strategy audit (section 5)
```

The backend fixture runs `alembic downgrade base` and then `alembic upgrade head` through `0003`, so all three migrations were applied against a real PostgreSQL 16.

**Warnings worth acting on.** There were 679 warnings, almost all `Pandas4Warning`. The strategies use `Timestamp.dayofweek` (`ict.py:399`, `sessions.py:241-243`), which pandas 3.x deprecates and a future pandas will remove.

The requirements are **unpinned** (`pandas>=2.2`), and this run resolved pandas 3.1.1 and SQLAlchemy 2.1.8. A future install could break strategy code without any change to the repository.

A lockfile is required. Editing the strategies is not an option: that would change their code hashes.

---

## 5. 215-strategy baseline verification

The audit was independent: a separate script compared the live registry with `strategy_inventory.json`. It did not just rely on `test_preservation.py`.

| Check | Result |
|---|---|
| Baseline `count` / `source_commit` | 215 / `0293bb3` |
| IDs missing from the registry / extra IDs | none / none |
| `code_hash` mismatches | **0** |
| name, category, component, params, timeframes, regimes, requires, risky | identical (`test_preservation.py` passed) |
| `exp_*` codes in the production registry | none |
| Category counts | smc 20, ict 20, price_action 30, liquidity 20, trend 15, indicators 25, volume 15, sessions 15, fibonacci 10, reversal 10, quantitative 20, combinations 15. Total **215**, equal to `EXPECTED_COUNTS`. |
| Versions | all `1.0.0` |

### Availability (from each strategy's own `requires`)

| Group | Count | Strategies |
|---|---|---|
| OHLC + tick volume only (runnable on standard MT5 data) | **195** | — |
| Need peer-symbol data (obtainable from MT5, but the scanner never supplies it) | 5 | `ict_smt`, `combo_ict_smt`, `quant_pairs`, `quant_cointegration`, `quant_stat_arb` |
| Need data MT5 retail feeds do not provide | 15 | order flow ×4 (`vol_cvd`, `vol_delta_divergence`, `vol_footprint`, `vol_order_flow`); DOM ×1 (`vol_iceberg`); open interest; options IV (`quant_vol_arb`); news feed (`sess_news`); trained model (`quant_ml`); Claude agent (`quant_ai_signal`); portfolio allocator ×3 (`quant_factor`, `quant_rebalancing`, `quant_risk_parity`); HFT ×2 (`quant_hft`, `quant_market_making`) |
| Flagged `risky` (disabled by default, blocked for LIVE) | 4 | `quant_grid`, `quant_martingale`, `quant_anti_martingale`, `quant_dca` |

**Honest executable count on a standard MT5 feed:** at most **195**. That rises to 200 if peer data is wired in (gap 7.2). Risky strategies stay disabled unless explicitly enabled.

### Declared timeframe coverage

| TF | M1 | M5 | M15 | M30 | H1 | H4 | D1 | **W1** |
|---|---|---|---|---|---|---|---|---|
| Strategies declaring it | 2 | 33 | 204 | 7 | 176 | 176 | 6 | **0** |

**Consequence.** If timeframe compatibility is enforced, as the specification requires, scanning W1 runs no strategy, and M1 or D1 run almost none. W1 can still provide regime and higher-timeframe context. The UI must show this honestly instead of implying all strategies cover all timeframes.

178 strategies declare no regime restriction, so they are considered suitable for every regime.

### Packaging note
`code_hash` is computed with `inspect.getsource()`, which fails in a frozen (PyInstaller) build. The fallback hashes only the code string, so preservation would silently break. **The backend must run from source** (Docker container or embedded Python with `.py` files), never as a frozen executable.

---

## 6. Existing capability map (reuse, don't duplicate)

| Specification area | Already present | Reuse decision |
|---|---|---|
| API | FastAPI with 74 route decorators, cookie session + CSRF, RBAC (admin, trader, readonly), audit log | Reuse unchanged |
| Database | 60 tables: `0001` 39, `0002` 19, `0003` 2. Linear Alembic chain `0001→0002→0003`. | Reuse; future migrations additive only |
| Strategies | Registry, 215 strategies, experimental sandbox (AST allow-list, subprocess, rlimits), lifecycle DISCOVERED→…→PRODUCTION with approvals | Reuse unchanged |
| Scanner | `scan_once`: DB-enabled symbols × configured TFs, `ThreadPoolExecutor(scan_max_parallel=4)`, closed bars only, one HTF, regime, news, Part 26 intelligence | Extend (gaps 7.1–7.3) |
| Signal fusion | `signals/engine.build_signal` with weighted components, A+…REJECTED grades, WAIT on conflict; `Calibrator` gives isotonic calibration, `None` below 30 samples | Reuse as the benchmark |
| Probability | `intelligence/probability.estimate` with registry-gated model, shadow when unapproved, "LOW DATA CONFIDENCE" | Reuse |
| Consensus | `intelligence/consensus` with 11 specialists, advisory by default; enforce mode can only downgrade | Reuse; maps to most of the requested "agents" |
| Regime | Rule-based baseline (`analysis/regime`) + HMM, CUSUM and 13-regime probabilities (`intelligence/regime`) | Reuse |
| Anomaly, portfolio, fingerprint, execution analytics | `intelligence/*`; critical anomalies block entries only | Reuse |
| Learner | `python -m app.learning_engine.scheduler`, separate process, Redis lock, 10 jobs: trade_sync, failure_analysis, portfolio, performance_snapshot, drift, fingerprints, experiments, train_probability, consensus_weights, discovery | Reuse; supervise as its own service |
| Trade intelligence | Collector with 6 h overlap sync, partial-exit VWAP aggregation, `INCONSISTENT` ledger guard; features with MAE/MFE in R; performance across 13 dimensions with Wilson/bootstrap CIs; DEMO/LIVE separated by `mode` | Reuse |
| Model lifecycle | TRAINED→VALIDATED→APPROVED→PRODUCTION, RETIRED, rollback; embargoed chronological splits; Brier/ECE/AUC; bootstrap skill bound; drift (PSI) | Reuse |
| Backtesting | `run_backtest` (next-bar fills, spread, SL-first), `split_oos`, `walk_forward`, `monte_carlo`, `spread_sensitivity`, `optimise` (grid) | Reuse |
| Risk | `risk/engine.evaluate` with one named check per rule; existing-position policy defaults to `PROTECT` | Reuse unchanged |
| Execution | Idempotent `execution_idempotency` (UNIQUE), journal, HMAC + nonce, UNKNOWN→reconcile, never resend | Reuse unchanged |
| MT5 | `mt5-connector` (HTTPS + HMAC, journal) and `windows-connector` (PySide6, tray, WSS relay, Ed25519 registration, Credential Manager vault, live guard, emergency stop) | Reuse; LOCAL mode through relay (section 9) |
| Claude | `claude_agent` with structured output and `enforce_boundaries`: no flip, no upgrade, no sizing, no execution | Reuse; generalise behind a provider gateway |
| Frontend | Next.js 15.5 / React 19, 20 navigation entries, all pages static | Reuse inside the desktop shell |

**Specification references that are empty or absent.** `backend/app/portfolio/` and `backend/app/optimisation/` contain only an empty `__init__.py`. The real implementations are `intelligence/portfolio.py` and `backtesting/engine.optimise`. No Qwen, Ollama or other non-Anthropic provider code exists anywhere.

### Safety conditions (specification Part 22) vs. existing tests

| # | Condition | Existing coverage |
|---|---|---|
| 1 | Learner cannot submit orders | ⚠️ **Not enforced structurally, not tested** (gap 7.4) |
| 2 | AI cannot enable LIVE | 🟡 `test_trader_cannot_switch_to_live` and `test_live_requires_confirmation_and_approved_account`. AI has no route to LIVE, but no explicit test asserts that. |
| 3 | AI cannot modify execution permissions | 🟡 By construction (AI has no API identity); no explicit test |
| 4 | Wrong broker identity blocks execution | ✅ `test_identity_mismatch_is_detected`; worker kill switch on `IdentityError` |
| 5 | Stale quotes block entries | ✅ `test_stale_quote_cannot_authorise_entry` |
| 6 | Critical anomalies block entries | ✅ `test_anomaly_blocks_entries_but_keeps_positions_and_recovers` |
| 7 | Risk failures block execution | ✅ `test_risk.py` |
| 8 | Duplicate submissions prevented | ✅ `test_duplicate_signal_never_executes_twice` |
| 9 | Unresolved executions reconciled | ✅ `test_lost_response_is_reconciled_not_resent` |
| 10 | Experimental code never in production registry | ✅ two tests |
| 11 | Model promotion needs validation + approval | ✅ `test_governance_requires_approval_and_supports_rollback`, RBAC test |
| 12 | Emergency stop blocks new entries | ✅ `test_kill_switch_*`, `test_pc_emergency_stop_activates_engine_kill_switch` |
| 13 | Emergency stop does not blindly close positions | ✅ by default: the policy defaults to `PROTECT`; `CLOSE_ALL` runs only when requested explicitly |
| 14 | Strategy preservation | ✅ verified above |

---

## 7. Gaps and defects found (to be fixed in later phases, not now)

| # | Finding | Evidence | Fix (phase) |
|---|---|---|---|
| 7.1 | **The scanner ignores strategy timeframe metadata.** `registry.evaluate_all` checks only `requires` and `min_bars`. For example, a strategy declared for H1/H4 still runs on M15. | `strategies/base.py:available`, `scanner/service.py:analyse` | Compatibility filter in the scanner, **outside** strategy classes so hashes are unchanged. Report "not applicable on TF", not "no signal". (P8) |
| 7.2 | Peer data is never supplied, so the 5 peer strategies are always unavailable. | `StrategyContext(..., peers=None)` | Fetch configured peer symbols when the strategy is compatible. (P8) |
| 7.3 | Scheduling is flat: one fixed pool, no priority, fairness, backpressure, cancellation or per-job timeout. W1 HTF maps to MN1. | `scan_once` | Priority queue with budgets. (P8/P20) |
| 7.4 | **The learner has order-capable bridge access.** It builds a full `BridgeClient` that has `submit()` and shares the worker's HMAC secret. "Never trades" is only a convention. | `scheduler.py:90`, `collector.py:24`, `bridge.py:142` | A read-only bridge client type for the learner and agents, plus a connector-side scope: a separate secret or key with no `/orders` permission. Add an explicit test. (P4/P14) |
| 7.5 | **Newly discovered symbols default to scan-enabled.** `discover_symbols` relies on `Symbol.scan_enabled default=True`, so every tradable broker symbol is scanned immediately. | `models.py:150`, `services/trading.py:81` | New rows `scan_enabled=False` on insert only; existing rows untouched. (P6) |
| 7.6 | Trading sessions cannot be read. The MetaTrader5 Python package has no session-schedule API. | `adapter.py` | Show "not provided by broker API"; optional user-entered or observed-tick schedule, clearly labelled. Never fabricate. (P6) |
| 7.7 | **The worker is monolithic.** Heartbeat, sync, reconcile, break-even/trailing management, news, scanning and auto-execution share one loop. Stopping it also stops scanning and position management. | `workers/main.py:main` | Decision D1 |
| 7.8 | Model artifacts are `pickle`d in the database, and the SHA-256 sits **in the same row**. That is integrity without authenticity: anyone with DB write access, or a restored foreign dump, can achieve code execution in the API or learner. | `learning_engine/models.py:220-223` | HMAC-sign artifacts with a local key; the migration tool re-verifies or quarantines imported models. (P11/P17) |
| 7.9 | Undeclared test dependency: backend tests import `windows-connector` (keyring, PySide6, websockets). | `test_mt5_agent.py:32` | Add to dev requirements or a test extra. (P3) |
| 7.10 | Dependencies are unpinned and pandas 3 deprecations are already firing in strategy code. | Test warnings | Lockfiles (pip-tools / `uv.lock`); pin pandas to the version the hashes were validated under. (P3) |
| 7.11 | Backups are bash-only, the dump is **unencrypted**, and the config tarball holds `.env` secrets in plaintext. | `scripts/backup.sh` | Windows-native, encrypted backup and restore with verification. (P17) |
| 7.12 | Compose publishes nginx on `80:80`/`443:443`. On Docker Desktop that binds all interfaces, exposing the engine to the LAN. | `docker-compose.yml` | Windows profile binding `127.0.0.1` only. (P3) |
| 7.13 | The Claude review for ranked candidates passes `spread: 0.0`. | `scanner/service.py`, `scan_once` | Pass the analysed spread. (P13) |
| 7.14 | Navigation has 20 entries; the specification asks for 27. | `Shell.tsx` | Extend the existing UI, not a new one. (P15) |
| 7.15 | AI is Anthropic-only; no gateway, usage/cost ledger by provider, or fallback. | `agents/` | Provider gateway that keeps the `enforce_boundaries` contract for every provider. (P9/P14) |
| 7.16 | Not verified anywhere, including in the source's own reports: a real Windows MT5 terminal, a real broker account, a live Claude call, real broker history for model validation. | Source docs | Needs your environment. (P4, P11) |

---

## 8. Decisions needed before Phase 2

**D1 — What does "WORKER STOPPED" mean?** The specification wants the worker stopped by default, but the scanner, Signal Centre and position protection must run. Options:

* **(a) Recommended.** Split the existing loop into supervised roles that reuse the same functions, with no new engine:
  * **monitor:** heartbeat, sync, reconcile and position protection. Runs whenever MT5 is connected.
  * **analysis:** scanner and signals.
  * **trading worker:** auto-execution only. **STOPPED by default.**

  `auto_trading=false` remains as an additional independent gate.
* **(b)** Keep one worker, stopped by default. Nothing scans and nothing trails stops until you start it; broker-side SL/TP still protect positions.

**D2 — Local infrastructure runtime.** The specification says Docker Desktop + WSL2. Two caveats:

* Docker Desktop needs a **paid subscription** for organisations with more than 250 employees or more than $10M revenue.
* Docker Desktop runs only while a user is signed in. That is acceptable here because MT5 also needs the interactive session.

I recommend Docker Desktop as specified, with the runtime abstracted so Podman or native Windows services could replace it later. Please confirm.

**D3 — Desktop shell to frontend wiring.** The frontend relies on same-origin `/api`, an httpOnly `SameSite=Strict` session cookie and a CSRF cookie.

* **Recommended:** the Tauri window loads `http://127.0.0.1:<port>` from the local nginx container, bound to loopback only. The auth model stays unchanged and no frontend rewrite is needed.
* **Alternative:** serve the static export from `tauri://`, which requires cross-origin auth changes. Not recommended.

**D4 — MT5 local transport.** Reuse the existing **relay** mode: the connector dials `ws://127.0.0.1` outbound, which `agent.py` already allows for localhost. No inbound Windows port, and the existing Ed25519, HMAC and nonce protections are kept.

Direct mode (`host.docker.internal:8443`) would open an inbound listener on Windows, so I recommend against it for LOCAL mode. The PySide6 connector would run as a supervised sidecar in the user session, with its UI kept as the diagnostic tool.

**D5 — Scope realism.** Phases 2–17 are many weeks of work. Several acceptance items cannot be verified from this Linux environment:

* Tauri Windows builds (possible in GitHub Actions `windows-latest`, as the connector already does);
* MT5 behaviour;
* WSL2/Docker Desktop installation.

I will mark each one ⛔ until you run it on your PC, rather than claim it works.

---

## 9. Proposed Windows architecture (for review)

```
RR Trading One.exe  (Tauri 2, Rust supervisor, tray, allowlisted actions only)
 │  health-checks every service via its real health endpoint
 ├─ Docker Desktop / WSL2  (compose project "rr-trading-one", ports bound to 127.0.0.1)
 │    postgres:16  redis:7  migrate(one-shot)  api  nginx(127.0.0.1:<port>)  frontend
 │    learner  (CPU/mem limits; read-only bridge scope)
 │    monitor / analysis / trading-worker  (D1; trading-worker not started by default)
 ├─ RR MT5 Connector  (existing windows-connector, user session, NOT Session 0)
 │    └─ outbound ws://127.0.0.1 relay → api  (existing Ed25519 + HMAC + journal)
 │    └─ MetaTrader5 package → terminal64.exe (explicitly selected profile)
 └─ WebView2 window → http://127.0.0.1:<port>  (existing Next.js UI)

C:\Program Files\RR Trading One\   app binaries, compose files (read-only)
C:\ProgramData\RR Trading One\     backups, logs, models, datasets (DB/Redis live in Docker named volumes)
%APPDATA%\RR Trading One\          settings; secrets via Windows Credential Manager / DPAPI
```

Initial state stays as specified: **DEMO / auto-trading OFF / trading worker STOPPED / LIVE locked.** These already are the seeded defaults (`cli.seed`, `TradingState`) and the connector's LIVE lock default.

## 10. Security implications identified

* **Learner order capability (7.4)** and **pickle authenticity (7.8)** are the two material issues. Both get fixed before any live use.
* **LAN exposure** if compose ports are not bound to loopback (7.12).
* **Plaintext secrets in backups** (7.11).
* **Bridge secret sharing.** Every backend container receives the same bridge secret through `env_file`. Per-role scoping (7.4) narrows this.
* **Strengths to keep:** HMAC + nonce replay protection, idempotent journal, Claude boundary enforcement in code, sandboxed experimental code, RBAC, typed LIVE confirmation, and a PC-side LIVE lock.

## 11. Known limitations of this Phase 1 audit

* Linux only. No Windows, MT5, Docker Desktop, WSL2 or Tauri Windows build was exercised.
* Tests ran on Python 3.13 with latest-resolved dependencies; the production image uses Python 3.12.8. The results show compatibility but are not a pinned reproduction.
* Strategy logic was audited for preservation and metadata, not for trading validity. No performance claims are made.

## 12. Rollback

Nothing to roll back in production code. To discard Phase 1 entirely, delete the branch. The baseline commit `5c6c26b` is a verbatim copy of the supplied zip.

## 13. Next phase

**Phase 2 — Windows desktop shell and application structure.** It starts after you review this report and answer D1–D5. Its first step adds a dependency lockfile without changing any strategy file, then re-runs the preservation test.
