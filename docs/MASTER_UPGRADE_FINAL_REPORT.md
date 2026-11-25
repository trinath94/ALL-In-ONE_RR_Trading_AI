# RR Trading One — Master Existing Engine Upgrade: final report

**Date:** 2026-11-25 · **Branch:** `claude/focused-cori-b8em6u` · **Audit:** `5c8b903` (`docs/MASTER_UPGRADE_AUDIT.md`)
**Scope received:** sections 1–12.2 of the prompt. The prompt as received ends inside 12.2 ("Unexpected
application"); anything after it was not received and is not covered here.
**Change set since the audit:** 70 files, +4,287 / −97 lines, migrations 0016–0020 (all additive). Also on this
branch just before the upgrade: the With AI / Without AI switch (`a31c5b1`, migration 0015).

The upgrade extends the existing platform; it adds no second engine, registry, learning engine, scheduler, execution
path or monitoring system. The 215 strategies, their IDs, source and default parameters are unchanged (preservation
tests). No risk control was weakened; every new automatic behaviour that could affect trading is off by default, and
LIVE stays locked.

## 1. What was done, by section of the prompt

| § | Requirement | Before (audit) | Now |
|---|---|---|---|
| 1 | Audit, preserve, classify | — | Audit with A–E classification; nothing rebuilt or duplicated |
| 2.1 | Real broker DEMO execution | A | unchanged: DEMO orders go through MT5 to the broker; simulation is research only |
| 2.2 | One shared engine for DEMO and LIVE | A | unchanged; the same workflow now runs for every execution account |
| 2.3 | Account isolation on records | B | orders, executions, positions, risk evaluations carry the account; each signal strategy entry records its configuration version |
| 3 | Multiple MT5 accounts (add, enable, risk, permissions, status) | B | per-account name, trading switch, eligible symbols and strategies, own risk limits; execution-accounts overview (traded now, reconciled, connector, equity, positions) |
| 3.3 | Credential security | A | unchanged (encrypted, never shown, never sent to AI) |
| 3.4 | Account verification | A | per account, through its own connector, before its first entry and after every disconnection |
| 4 | Multi-account autonomous trading | E | each signal evaluated for every execution account: eligibility, own equity and limits, own idempotency key, own connector; a rejection on one account never undoes another's execution |
| 5 | Reuse strategies and agents | A | reused; one learner job added under the existing adaptive-optimisation agent |
| 6.1 | Providers (Anthropic, OpenAI, Qwen, Ollama) | A | plus last successful request and last error per provider |
| 6.2 | Single Active AI | E | one provider and model for every AI task; disabled providers get no requests; provider and model recorded per analysis |
| 6.3 | Fallback | B | explicit fallback only; without it a failed provider stops AI analysis (no review, no auto-execution in With AI mode) |
| 6.4 | Claude Pro | E | shown as not available (no official application access to a Claude.ai subscription); the Anthropic API is the supported way |
| 7.1 | Market analysis inputs | A | unchanged |
| 7.2 | Structured pre-trade analysis | B | per opportunity: why, supporting strategies and patterns, favourable and contradicting conditions, invalidation, entry/stop/target justification, failure risks, should it be rejected |
| 7.3 | Post-trade review | B | every closed trade answers the same questions; stored with the learner's failure report |
| 7.4 | Learn from every outcome; normal loss vs defect | B | wins, losses, missed and rejected trades (existing) plus a binomial test of each strategy's recent loss rate against its validated evidence: normal variance, mistake with evidence, or suspected recurring defect |
| 8 | Adaptive, versioned configuration | E | versioned profiles per strategy, regime, symbols, timeframes; never changing the strategy; rollback to the validated parent |
| 8.4 | Overfitting controls | A (tools) | gates: out-of-sample sample size, expectancy ≥ defaults, profit factor, drawdown, in/out-of-sample stability, walk-forward, doubled-spread cost test, look-ahead and leakage |
| 9 | Strategy discovery and promotion | A/B | existing lifecycle; optional automatic start of the DEMO shadow test of validated experiments (off by default); LIVE always manual |
| 10 | Signals and execution | A/B | configuration version on signals; per-account execution |
| 11 | Risk and account safety | A/E | per-account limits; kill switch reaches every account; a tripped limit on any account locks new entries everywhere (conservative) |
| 12–12.2 | Error detection, popups, self-healing | B/E | classified, de-duplicated incidents with explanation, diagnosis and fix; detectors; pop-up alerts; console error boundary; safe, verified, recorded repairs; AI diagnostic advice; statistics |

## 2. Files changed (master upgrade)

**Engine — new:** `app/strategies/profiles.py` (configuration profiles), `app/learning_engine/review.py` (post-trade
review, loss significance), `app/services/incidents.py` (error detection and self-healing).
**Engine — extended:** `services/trading.py` (account limits, execution accounts, eligibility, reconciliation state),
`execution/manager.py` (optional account on every entry point), `execution/bridge.py` and `relay.py` (per-account
connector routing, `AccountBridges`), `workers/main.py` and `roles.py` (per-account execution and maintenance,
incident cycle), `scanner/service.py` (profiles after regime detection, configuration version on signals),
`ai/gateway.py` (Single Active AI, `config_proposal` task), `intelligence/opportunity.py` (pre-trade analysis),
`learning_engine/failure.py` and `scheduler.py` (review stored; `config_profiles` job), `services/events.py`
(warning-and-above events become incidents), `main.py` (unhandled errors become incidents), `services/readiness.py`
(LIVE account's own limits), API routes `trading.py`, `ai.py`, `intel.py`, `system.py`, models.
**Windows connector:** `headless.py` (`RRMT5_INSTANCE`: own folder and vault entry per instance).
**Desktop:** `core/src/connector.rs`, `local.rs`, `src-tauri/src/lib.rs`, `ui/mt5.js`, `ui/index.html` (one connector
per MT5 profile; Stop All stops every instance).
**Console:** `AccountSettings`, `ExecutionAccounts`, `ActiveAI`, `StrategyProfiles`, `Thesis` (pre-trade analysis and
trade review), `Incidents` (pop-ups and panel), `app/(console)/error.tsx`; pages Settings, AI Studio, Strategy
Library, Opportunity Lifecycle, Trade History, System Monitor.
**Tests:** `test_accounts_multi.py` (9), `test_accounts_execution.py` (8), `test_ai_control.py` (8),
`test_profiles_reviews.py` (22), `test_incidents.py` (22), `test_master_upgrade_integration.py` (1), 2 connector and
1 desktop test. Existing tests changed: fixtures that insert an active account directly also set `trading_enabled`
(setup only); `conftest.py` truncates the new tables.

## 3. Database changes (all additive)

| Migration | Adds |
|---|---|
| 0016 | `trading_accounts`: `label`, `trading_enabled` (backfilled from `is_active`), `risk_overrides` (`{}`), `allowed_symbols`, `allowed_strategies` |
| 0017 | `trading_accounts`: `reconciled`, `emergency_applied` |
| 0018 | table `ai_control` (Single Active AI; off unless saved) |
| 0019 | tables `strategy_config_profiles`, `automation_policy` (everything off); `failure_reports.review` |
| 0020 | table `incidents`; `automation_policy.auto_repair` (on: safe repairs only) |

**Data preservation verified** on a scratch database seeded with 215 strategies, 2 accounts and 25 closed trades:
row counts identical at 0014, after upgrade to 0020, after downgrade to 0014 and after upgrade again; the active
account was backfilled as the execution account, the other as research-only; AI mode defaulted to With AI.

## 4. API changes

New: `PUT /api/accounts/{id}/settings` · `GET/PUT /api/accounts/{id}/risk` · `GET /api/accounts/status` ·
`GET/PUT /api/ai/control` · `GET/POST /api/strategies/{code}/profiles` · `POST /api/strategies/{code}/profiles/propose`
· `POST /api/profiles/{id}/action` · `GET/PUT /api/automation/policy` · `GET /api/opportunities/{id}/thesis` ·
`GET /api/trades/{closed_id}/review` · `GET /api/strategies/{code}/loss-significance` · `GET /api/incidents` ·
`GET /api/incidents/summary` · `POST /api/incidents/{id}/ack|resolve|repair|diagnose` · `POST /api/client-errors`.
Extended: account objects (name, trading switch, own limits, eligibility, reconciliation); `/api/ai/providers` (last
success, last error); `/api/positions/history` (id). No route was removed.

## 5. Testing

Commands as in `docs/TEST_MATRIX.md` (engine, MT5 connector, Windows connector, desktop core, then
`scripts/test_matrix.py`). Results at `451b880`:

| Suite | Before the upgrade (`5c8b903`) | Now |
|---|---|---|
| Engine | 408 passed | **478 passed**, 0 failed |
| MT5 connector | 17 | **17** |
| Windows connector | 38 | **40** |
| Desktop core | 134 | **135**, clippy clean |
| Test matrix | 25 areas, 16 safety conditions | **25 areas, 20 safety conditions, 35 Parts 27–30 validations: all proven** |
| CI on `451b880` | — | desktop (Windows install/upgrade/uninstall) green and installer published; backend, frontend, security green on `dbae78e` |

**Failures introduced and fixed:** one test of the pre-trade analysis (step 4) assumed that a forced BUY always has a
supporting strategy; it failed once in CI depending on which opportunity it picked. The assertion now checks what the
analysis guarantees and the opportunity is picked deterministically. No baseline test fails.

**Safety conditions added:** 16 (Run without AI keeps the Risk Engine), 17 (each account only through its own
connector, reconciliation and limits; never across modes), 18 (Single Active AI never calls an unconfigured provider),
19 (profiles only after validation; AI values bounded; LIVE never automatic), 20 (self-healing verified, recorded, never
changes trading controls).

## 6. Integration

`test_master_upgrade_integration.py` runs the whole upgrade once with two simulated broker accounts behind two real
connector apps and a scripted AI provider: Single Active AI reviews the candidates; a validated DEMO profile is applied
and recorded on each signal; both accounts trade, account 2 within its own lot limit; positions are closed and synced
from each account's deal history; the learner reviews every closed trade; an MT5 outage becomes one incident and
closes itself when the connection returns; the kill switch reaches both accounts and its incident closes only after
the administrator's reset; the engine stays in DEMO.

## 7. DEMO and LIVE readiness

**DEMO:** ready for acceptance on your PC (runbook `docs/rr-trading-one/DEMO_VALIDATION.md`). Not yet done: real MT5
terminals side by side on Windows (one installation per account), real broker fills, and several trading days.

**LIVE:** not ready and still locked. Unresolved: DEMO acceptance; your decision on whether LIVE requires a
non-negative DEMO result; the unsigned installer; real-terminal testing of several connectors on one PC; DEMO and LIVE
accounts do not trade at the same time (LIVE accounts trade only while the existing LIVE switch is on).

## 8. Operating the new features

| Task | Where |
|---|---|
| Add a second account | MT5 Manager: one profile per terminal → Use, Link, Start. Console: Settings → add, Verify, Settings → Enable trading |
| See what is traded | Settings → Execution accounts |
| Per-account limits | Settings → account → Settings → Risk limits |
| One AI provider | AI Studio → Active AI (optional fallback) |
| Configuration profiles | Strategy Library → strategy → Configuration profiles |
| Pre-trade analysis / trade review | Opportunity Lifecycle → opportunity · Trade History → review |
| Incidents, repairs, auto-repair switch | System Monitor → Incidents (pop-ups appear on every page) |
| Database version | `docker exec rr-trading-one-postgres-1 psql -U rr -d rr_trading -Atc "select version_num from alembic_version"` (expect `0020`) |
