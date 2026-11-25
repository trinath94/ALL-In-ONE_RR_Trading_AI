# RR Trading One — Master Existing Engine Upgrade: audit and classification

**Date:** 2026-11-25 · **Baseline:** `ff7f3bf` (branch `claude/focused-cori-b8em6u`)
**Scope:** sections 1–12.2 of the Master Existing Engine Upgrade prompt. The prompt as received ends inside 12.2
("Unexpected application"). The rest of section 12 and anything after it were not received and are not classified
here.

Classification: **A** exists and works (skip) · **B** incomplete (extend) · **C** exists but not connected
(integrate) · **D** defective (repair) · **E** missing (implement).

## Baseline before any change

| Suite | Result at `ff7f3bf` |
|---|---|
| Engine | 408 passed |
| MT5 connector | 17 passed |
| Windows connector | 38 passed |
| Desktop core | 134 passed, clippy clean |
| Console | builds (type and lint checks) |
| CI | backend, desktop (Windows install/upgrade/uninstall), frontend, windows-connector, security: green |

No baseline failures.

## The one structural gap

The engine already uses **one** trading workflow for DEMO and LIVE (section 2.2 is **A**). What it does not do is
trade **several accounts at once**. Today:

* `trading_state.active_mode` selects DEMO or LIVE, and exactly one `TradingAccount` per mode is `is_active`.
  `services/trading.active_account()` returns that one account, and the execution manager, startup reconciliation,
  position sync, Risk Engine inputs and auto-execution all work on it.
* Risk limits are stored per **mode** (`risk_configs.account_mode`, unique), not per account.
* `relay.select_agent()` picks **one** connector (the one serving the active account).
* One RR MT5 Connector drives **one** MT5 terminal, and an MT5 terminal is logged in to one account at a time.

So sections 3.2, 3.5, 4 and the per-account parts of 11 need the active account to become a set of enabled
accounts, each with its own connector, risk profile, reconciliation state and kill switch. This is an extension of the
existing objects (accounts, connectors, journal, Risk Engine, worker), not a second engine.

**Hard constraint (MetaTrader 5, not a design choice):** each account traded at the same time needs its own MT5
terminal installation (separate folder, logged in to that account) and its own connector instance on the PC. The MT5
Python API attaches one process to one terminal. Accounts on the same terminal cannot be traded concurrently.

## Section-by-section classification

### 1. Preserve the existing application

| Requirement | Class | Evidence |
|---|---|---|
| Audit before change | A (this document) | |
| 215 production strategies, IDs, source | A | `tests/test_preservation.py`; unchanged |
| Agents, registry (27), configurations | A | `orchestration/agents.py`, `test_orchestration.py` |
| No duplicate engines/registries/learning/monitoring | A, enforced by plan | every item below extends an existing module |

### 2. One shared engine for DEMO and LIVE

| Requirement | Class | Evidence / gap |
|---|---|---|
| 2.1 Real broker DEMO (terminal, market data, specs, orders, fills, history) | A | DEMO goes through the same bridge and connector to the broker; the simulated broker is used only by tests; backtests and the Simulation Laboratory are labelled research |
| 2.2 Same workflow both modes | A | one scanner, signal engine, Risk Engine, execution manager; mode selects account, risk profile and destination |
| 2.3 Account isolation on orders, executions, positions, closed trades | A | `account_id` on orders, executions, positions, snapshots, closed positions; connector re-verifies login and server on every command |
| 2.3 Account fields on signals and learning records | B | signals are account-agnostic (correct: one signal, many accounts); per-account **evaluation** records are missing; learning records carry account via trades only |
| 2.3 Strategy version and configuration version on every record | B | strategy codes recorded; no configuration version exists yet (see 8) |

### 3. Multiple MT5 account connection manager

| Requirement | Class | Evidence / gap |
|---|---|---|
| Add, edit, remove, enable/disable, DEMO/LIVE | B | accounts CRUD, verification and LIVE approval exist; "enabled for trading" is a single `is_active` per mode |
| Connect/disconnect, test connection | B | per connector (desktop MT5 Manager); one connector per PC today |
| Equity, margin, positions, history per account | B | exist for the active account only |
| 3.3 Credential security | A | Fernet-encrypted passwords, DPAPI on the PC, redaction, never in AI prompts |
| 3.4 Account verification (terminal, server, login, mode, currency, permissions, symbols, specs, match) | A | `verify_account`, startup reconciliation, connector identity checks; mismatch → kill switch and alert |
| 3.5 Per-account strategies, instruments, risk profile, research-only vs execution | E | today per mode only |

### 4. Multi-account autonomous trading

| Requirement | Class | Evidence / gap |
|---|---|---|
| Per-account loop: info, risk, exposure, sizing, submit, confirm, manage, record | E (by extending the existing worker) | worker runs for the one active account |
| Account-specific position sizing (equity, currency, contract, margin) | A for one account | `risk.evaluate` sizes from the account's equity and symbol spec; must run per account |

### 5. Reuse existing agents and strategies

| Requirement | Class | Evidence |
|---|---|---|
| Analyse strategies, configs, suitability, history, regimes | A | strategy DNA, adaptive weights, selection decisions, regime performance |
| Select strongest validated candidates, not all | A | consensus + ranking + `claude_max_candidates_per_scan` + selection stage |

### 6. Multi-provider AI

| Requirement | Class | Evidence / gap |
|---|---|---|
| Anthropic, OpenAI, Qwen (DashScope), Ollama | A | presets, encrypted keys, model discovery, test connection, usage and cost ledger |
| Per-provider ON/OFF, status, model, test, usage, cost | A | `ai_providers.enabled`, `/api/ai/providers/{id}/test`, `/api/ai/usage`, `/api/ai/health` |
| Last successful request, last error per provider | B | in the ledger; not shown per provider |
| 6.2 Single Active AI mode (one provider for all AI tasks, clearly displayed) | E | routes are per task with chains; no global selector |
| 6.3 Fallback off → WAIT/NO_TRADE; on → only an explicit fallback, recorded | B | chain fallback exists and is recorded; no global "fallback off" switch |
| 6.4 Claude Pro | E (status only) | no official API access through a Claude.ai subscription; show "not available, use the Anthropic API" |
| 6.8 No keys in frontend, no broker credentials to AI, AI never executes | A | gateway boundaries, redaction, write-only keys |
| Run without AI | A | added 2026-11-23 (`ai_mode`) |

### 7. AI trading intelligence

| Requirement | Class | Evidence / gap |
|---|---|---|
| 7.1 Market analysis inputs | A | regime, structure, patterns, MTF, news, spread, exposure, peers |
| 7.2 Structured pre-trade analysis (why, contradictions, invalidation, SL/TP reasons, failure risk) | B | decision trace + entry validation + exit plan + failure prediction + AI review exist; no single pre-trade thesis record |
| 7.3 Post-trade review per trade | B | failure analysis, execution analytics, trade intelligence; no per-trade review record covering all questions |
| 7.4 Learn from wins, losses, missed and rejected; normal loss vs defect | B | failure analysis + counterfactuals + governance; no statistical "normal loss vs recurring defect" classifier |

### 8. Adaptive strategy configuration

| Requirement | Class | Evidence / gap |
|---|---|---|
| Versioned configuration profiles without changing source | E | `strategy_configurations` is one mutable row per strategy/mode/symbol; no history or regime profiles |
| Regime-specific profile selection | E | adaptive weights adapt weighting only |
| Overfitting controls (sample size, OOS, walk-forward, drawdown, stability, cost sensitivity, shadow/DEMO) | A as tools | backtester, walk-forward, bias report, variants; must gate profile deployment |
| Rollback | E | with versioning |

### 9. Strategy discovery

| Requirement | Class | Evidence / gap |
|---|---|---|
| Candidates with ID, parent, reason, rules, validation, comparison | A | experiments, variants, sandbox, validator, bias report |
| Statuses Experimental → Validating → Shadow → DEMO-approved → Production-approved | A (different names) | `DISCOVERED … DEMO_TESTING … APPROVED … PRODUCTION` |
| Automatic promotion to DEMO under configured criteria | B | evidence-gated stages are automatic up to approval; DEMO deployment needs approval |
| LIVE promotion only with independent validation and administrator policy | A | `MANUAL_ONLY` stages |

### 10. Signal generation and execution

| Requirement | Class | Evidence / gap |
|---|---|---|
| Workflow order | A | decision trace stages |
| Structured signal fields | B | missing configuration version and per-account eligibility |
| Execution checks (stale, still valid, spread, specs, identity, risk) | A | expiry, anomaly, spread, identity, Risk Engine |
| Position management (SL, TP, BE, trailing, partial, structure, time, emergency) | A | `manage_position`, optional exits, emergency policies |

### 11. Risk and account safety

| Requirement | Class | Evidence / gap |
|---|---|---|
| Risk limits (per trade, daily, weekly, drawdown, positions, exposure, margin, lot) | A per mode | `RiskParams` |
| Independent limits per account | E | per mode only |
| LIVE locked until authorised | A | five separate consents |
| Disable account, stop new trading, stop all, disable provider, strategy, symbol, emergency stop | A, except per-account controls | per account: E with multi-account |
| Disable automatic promotion | E | |

### 12. Errors, popups, self-healing (12–12.2 only)

| Requirement | Class | Evidence / gap |
|---|---|---|
| Backend logging, redaction, per-service error counts | A | `ErrorCounter`, log redaction |
| System events, notifications (in-app, Telegram, email) | A | `system_event`, `notify` |
| Monitoring snapshot (latency, errors, DB, backlog, resources) | A | `/api/system/monitoring`, `/api/system/resources` |
| Restart policies, connector reconnect, learner job recovery | A | compose `unless-stopped`; trading worker deliberately not auto-restarted |
| Central incident record: classify, de-duplicate, explain, diagnose, recommend | E | events are flat messages |
| Smart popup alerts in the console | E | no popup or toast component |
| Frontend error boundary and client error reporting | E | none |
| Safe self-healing actions with verification and record | B | restarts exist; no catalogue of safe, verified, recorded repairs |

## Proposed phases (subject to the missing part of the prompt)

1. **Audit** (this document) — done.
2. **Multi-account foundation:** account set instead of one active account; per-account risk profile (DEMO/LIVE
   templates kept as defaults); per-account connector binding; per-account reconciliation, kill switch and state;
   migration preserves today's active accounts as the enabled ones.
3. **Multi-account execution:** per-account evaluation of each signal (eligibility, sizing, Risk Engine), routing to
   that account's connector; desktop and connector support for several terminals; Account Manager page.
4. **AI provider control:** Single Active AI selector, global fallback switch, per-provider last success and error,
   Claude Pro status.
5. **Configuration versioning and adaptation:** versioned regime profiles with validation gates and rollback; pre-trade
   thesis and post-trade review records; normal-loss vs defect statistics; DEMO auto-promotion policy with an off
   switch.
6. **Error detection and self-healing:** incident model, popups, frontend error boundary, safe repair catalogue.
7. Further phases from the rest of the prompt, then integration, DEMO validation and LIVE readiness.

Nothing in this plan changes LIVE's locked state or weakens a risk control.
