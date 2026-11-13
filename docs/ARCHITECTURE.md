# RR Trading Engine — Architecture (Phase 1 deliverables)

## 1. Repository inspection report

The working directory was **empty** at the start: no existing repository, strategies, tests or supplied
files. There was therefore no original code to preserve, and nothing was deleted or replaced. Every
module in this repository is new. If an original strategy repository is supplied later, its strategies
should be added as new registry modules (new codes) rather than overwriting these. The registry rejects
duplicate codes, and every code change creates a new `strategy_versions` row.

## 2. Strategy preservation report

* 215 strategies in 12 categories, matching the specification's counts exactly (`docs/STRATEGIES.md`).
* Each one is a class with a `detect()` implementation, parameters, timeframes, suited regimes and a
  source hash. `tests/test_strategies.py` asserts the counts, that no strategy raises on a long
  synthetic series, and that every emitted signal is well-formed. It also checks crafted scenarios
  (EMA cross bar, liquidity sweep, double top, engulfing).
* **Honest availability.** 15 strategies need data that a standard MT5 retail feed does not provide.
  They contain real logic but are reported as unavailable and stay disabled:
  * Order flow: delta divergence, CVD, footprint, order-flow trading.
  * Market depth: iceberg orders, HFT, market making.
  * Other feeds: open interest, options implied volatility (vol arbitrage), economic calendar (news-session trading).
  * A trained model artefact: the ML strategy.
  * Allocation models that produce portfolio weights, not entries: rebalancing, factor investing, risk parity.
  * The AI signal strategy, which is the Claude agent layer itself.
* Grid, Martingale, Anti-Martingale and DCA are flagged `risky`, are disabled by default, and cannot be
  enabled for LIVE from the UI. Their size multipliers are capped by the Risk Engine
  (`max_size_multiplier`, default 1).

## 3. System architecture

```
Browser ──HTTPS/WSS──► Nginx (TLS, HSTS, CSP, rate limits)
                         ├─► Next.js frontend (static UI, no secrets)
                         └─► FastAPI api  ──► PostgreSQL (state, audit)   Redis (pub/sub, locks, cache)
                                    ▲
           worker (single, Redis-locked):  startup reconciliation → heartbeat → sync → reconcile journal
                                           → scanner (parallel symbols×TFs) → signal engine
                                           → Claude review (shortlist only) → Risk Engine → Execution Manager
                                           → position management (BE / partial / trailing)
                                    │ HTTPS + HMAC (pinned self-signed cert, IP allow-list)
                                    ▼
           Windows: RR MT5 Connector service ── execution journal (SQLite, WAL, fsync) ── MetaTrader 5 ── Broker
```

Layering (spec 3.2):
1. **Quantitative:** `app/analysis` (indicators, structure, sessions, regime), all causal.
2. **Strategy intelligence:** `app/strategies/*` evaluates each enabled strategy independently.
3. **Claude reasoning:** `app/agents/claude_agent.py` reviews the top N shortlisted candidates per scan
   (throttled per symbol/timeframe). It can confirm, downgrade or reject a candidate, and **cannot**
   flip direction, upgrade a non-trade, size a position or execute anything; `enforce_boundaries()`
   enforces this in code.
4. **Signal ranking:** `app/signals/engine.py`. Weighted components use the spec's initial weights,
   which are configurable and validated. Grades run A+ to REJECTED, conflicts produce WAIT, and
   confidence is an **isotonic-calibrated out-of-sample win rate**, or `None` when there are fewer than
   30 samples.
5. **Risk Engine:** `app/risk/engine.py`. Pure and deterministic; every order (auto, manual,
   AI-confirmed) passes through it.
6. **Execution Manager:** `app/execution/manager.py`. Idempotent and journalled; an order is
   CONFIRMED only on an MT5 success retcode.

Auto-execution additionally requires the Claude review to confirm the direction. If Claude is
unavailable, candidates are recorded but not auto-traded; manual execution through the Risk Engine
remains possible.

## 4. Database schema

`backend/app/db/models.py` holds the domain tables: all 31 entities required by the spec, plus
configuration tables (`risk_configs` per mode, `trading_state`, `economic_events`,
`notification_settings`, `strategy_experiments`, `user_sessions`, `login_history`). The schema is
managed by Alembic (`backend/migrations`), and the migration is tested for upgrade → downgrade →
upgrade. Key integrity rules:

* `execution_idempotency.dedupe_key` is UNIQUE, so one signal cannot create two orders, even under
  concurrent workers.
* `trade_orders.execution_id` is UNIQUE and is shared with the connector journal.
* `open_positions` is replaced from MT5 on each sync. `closed_positions` is built from MT5 deal
  history and linked back to `execution_id`, which feeds calibration.
* DEMO and LIVE have separate `risk_configs` rows and separate `strategy_configurations`.
* Broker passwords are stored only as Fernet ciphertext (`trading_accounts.encrypted_password`), and
  user passwords only as argon2id hashes.

## 5. API architecture

FastAPI. The route list is in `docs/API.md` (71 routes plus the WebSocket).

* **Authentication.** An httpOnly, Secure, SameSite=Strict session cookie (JWT bound to a revocable
  `user_sessions` row) plus a double-submit CSRF token on every non-GET request.
* **Roles.** admin, trader and readonly, built from permission codes (`trade.live` is admin-only).
* **Rate limits.** Login is limited per IP in both Redis and Nginx, and writes are limited per user.
  Accounts lock after 5 failed logins. Login timing is equalised for unknown users.
* **Realtime.** `/api/ws` checks the session cookie and fans out Redis pub/sub events (signals,
  orders, positions, account, control, notifications, agent activity).

## 6. Linux ↔ Windows communication design

| Concern | Mechanism |
|---|---|
| Transport | HTTPS. The connector serves a self-signed cert that the Linux side **pins** (`RR_BRIDGE_CA_BUNDLE`). The Windows firewall admits only the Linux IP, and the connector enforces `RR_ALLOWED_IPS` as well. |
| Authentication | HMAC-SHA256 over `METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)`, a 30 s clock-skew window, and nonces journalled to reject replays. The secret is ≥ 32 chars and stored in Windows Credential Manager. |
| Idempotency | Each command carries an `execution_id`. The connector **reserves** it in its journal before doing anything, and a repeated id returns the journalled result. |
| Crash safety | The journal records SENDING (fsync) before `order_send`. On restart or a repeat request, SENDING/UNKNOWN entries are reconciled against MT5 deals and orders tagged with `RR+sha1(id)[:14]` in the order comment. An entry is declared never-executed only after a 120 s grace period with no trace. |
| Lost responses | The Linux side marks the order UNKNOWN and resolves it **only** through `GET /executions/{id}`. It never resubmits under a new id. This is tested in `test_lost_response_is_reconciled_not_resent`. |
| Identity | Every command includes the account login and server, which the connector compares with `account_info()`. The Linux side also checks the DEMO/REAL account type against the configured mode. |
| Heartbeat | The worker polls `/health` every `RR_BRIDGE_HEARTBEAT_SECONDS`. Losing it raises an MT5_DISCONNECTED notification and clears `startup_checks_passed`, so reconciliation must pass again before any entry. |
| Reconnection | The connector watchdog re-initialises MT5 every 5 s if it is disconnected, then reconciles the journal. |

## 7. Risk and security design

Risk (spec Part 9) is implemented in `evaluate()` with one named check per rule, recorded in
`risk_evaluations`:

* Kill switch, risk lock and pause.
* Expiry, mandatory SL (correct side, at least the broker stops level), TP, and minimum R:R.
* Spread, slippage, and news status (UNKNOWN blocks by default).
* Daily, weekly and monthly loss, and peak drawdown. These **lock trading** and apply the existing-position policy.
* Consecutive losses and the daily profit target.
* Maximum positions, per-symbol limit, duplicate positions, and unprotected positions.
* Sizing from the **live** tick value and step. The stop distance includes spread and slippage, and
  volume is floored, never rounded up.
* Lot cap, total open risk, correlated risk (explicit ρ or shared currency legs), notional leverage,
  and broker-computed margin.

If margin can't be obtained, the order is rejected. The emergency position policy is
PROTECT, REDUCE or CLOSE_ALL.

Security:

* Secrets live only in `.env` (mode 600) or Credential Manager, never in the frontend.
* The database and Redis sit on an internal-only Docker network.
* Nginx sets CSP, HSTS, X-Frame-Options and nosniff.
* All inputs go through Pydantic validation, and SQL goes through SQLAlchemy parameters.
* Dangerous actions require typed confirmation phrases.
* Every control, configuration and order action is written to the append-only `audit_logs` table.

## 8. Roadmap

Phases 1–10 are implemented in this repository and covered by automated tests. See
`docs/PHASE_REPORT.md` for verification evidence and the work that **requires the real environment**
(Windows server + broker + Claude API key), which can't be completed or verified from here.
