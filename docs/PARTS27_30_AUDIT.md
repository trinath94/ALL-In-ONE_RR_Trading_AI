# Parts 27–30: source audit, feature inventory and gap analysis

**Scope:** the enhancement prompt for Parts 27–30 (autonomous research and trading intelligence; pattern and market
structure intelligence; strategy discovery, self-learning and automated trading; execution reliability, data
governance, capital protection and self-recovery).
**Baseline:** commit `abe3d7f` (RR Trading One after Phases 1–26).
**Date:** 2026-11-17.

**Classification:**
* **A:** exists and works: skip.
* **B:** exists but incomplete: extend the existing module.
* **C:** exists but not integrated: connect it.
* **D:** exists but defective: repair.
* **E:** missing: implement within the current architecture.

Evidence is the file that implements it. A or B means I read the code, and tests exist where named.

## 0. Baseline tests (before any change)

Run on commit `abe3d7f`, each suite on its own:

| Suite | Command | Result |
|---|---|---|
| Engine, incl. the 215-strategy preservation tests | `cd backend && python -m pytest -q` | **323 passed** (11 min 24 s) |
| MT5 bridge (original connector) | `cd mt5-connector && python -m pytest -q` | **17 passed** |
| RR MT5 Connector | `cd windows-connector && QT_QPA_PLATFORM=offscreen python -m pytest -q tests` | **36 passed** |
| Desktop core | `cd desktop && cargo test -p rr-one-core` | **130 passed** |

No baseline failure. Any later failure is new.

## 1. What the platform already is (summary of the audit)

| Area | Existing implementation |
|---|---|
| **Roles and lifecycle** | Analysis role: scan, regime, strategies, consensus, probability, fusion, AI review, signals. Trading worker: confirmed signals → Risk Engine → MT5. Monitor: heartbeat, startup reconciliation, position management, signal expiry. Learner: trade sync, failure analysis, drift, training, adaptive weights, discovery; isolated job processes. |
| **Agents** | 21 registered agents (`app/orchestration/agents.py`), each run recorded (`agent_runs`); only 2 on the execution path |
| **Strategies** | 215 original strategies in 12 categories (price action 30, indicators 25, ICT 20, SMC 20, liquidity 20, quantitative 20, trend 15, volume 15, sessions 15, combinations 15, fibonacci 10, reversal 10); hash-verified; data requirements declared (`requires`) |
| **Patterns** | Implemented as strategies: classical (17: H&S, double/triple tops, triangles, flags, wedges, channel), candlestick (6), SMC (≈35: BOS, CHoCH, OB, FVG, sweeps), ICT (23), Wyckoff (5), VSA (3), gaps/imbalances (3). **No harmonic, no Elliott Wave.** |
| **Regime** | `app/analysis/regime.py` + HMM (`app/intelligence/regime.py`): strong/weak bull/bear, sideways, consolidation, high/low volatility, breakout, reversal, mean-reverting/persistent |
| **AI** | Provider gateway (`app/ai`): Anthropic and any OpenAI-compatible API (Qwen, Ollama presets); per-task routes with fallback; retries; per-minute limits; cooldown; token and cost ledger; health; encrypted write-only keys; schema-validated output; AI may only confirm or downgrade |
| **Probability** | Signed, calibrated model "take-profit before stop-loss"; predictions recorded with outcomes (`model_predictions`); fusion never more aggressive than the engine |
| **Learning** | Trade collection from broker deals (DEMO/LIVE apart), attribution, features incl. MAE/MFE, failure analysis, learning memory, missed-opportunity counterfactuals (hypothetical, excluded from statistics), drift (PSI), model registry with signing, walk-forward, champion comparison, rollback; adaptive weights in shadow |
| **Discovery** | LLM research agent → experimental strategies → static validator → resource-limited sandbox → validation pipeline (OOS, walk-forward, Monte Carlo, costs, slippage, forward shadow) → approval; never in the production registry |
| **Backtesting** | Bar-by-bar engine (next-open fills, spread, same-bar stop-first, gaps), optimiser with OOS, walk-forward, Monte Carlo |
| **Risk** | Deterministic Risk Engine (`app/risk/engine.py`): per-trade risk, daily/weekly/monthly loss, drawdown, positions, per-symbol limit, total exposure, lot cap, spread, slippage, correlated exposure (incl. currency legs), consecutive losses, margin, leverage, min R:R, news, emergency position policy (PROTECT/REDUCE/CLOSE_ALL), break-even, trailing, partial close |
| **Execution** | Idempotent journal, outbound relay with HMAC and nonces, broker identity check per order, lost-response reconciliation (never blind resend), execution analytics (slippage, latency, rejections, requotes, partial fills) |
| **Reliability** | Startup reconciliation (identity, positions, pending orders, journal, protect unprotected); role locks; monitor heartbeat; isolated learner jobs; resource monitoring; restart recovery |
| **Data quality** | Anomaly rules: stale price, abnormal spread, flash move, volatility spike, gap, unexpected jump, tick-volume anomaly, feed interruption, news volatility (critical ones block entries); news status UNKNOWN blocks by default |
| **Backup/DR** | Encrypted backups, one-transaction restore, safety backups, backup key, verified Ubuntu import with migration report |
| **Frontend** | 30 console pages incl. Command Centre, Control Room, Scanner, Analysis, Signals, Trading Worker, Learning Centre, Adaptive, Strategy Performance/Correlation, Predictive, Backtesting, Optimisation, Library, Research Lab, Risk, Portfolio, AI Studio, News, System Monitor, Backup |

## 2. Gap analysis

### Part 27: autonomous market research and trading intelligence

| Req. | Requirement | Class | Evidence / gap |
|---|---|---|---|
| 27.1 | Orchestrated lifecycle (data → validation → regime → patterns → strategies → research → probability → entry/exit → failure → portfolio → risk → MT5 → monitoring → results → learning) | **B** | Every stage exists across the roles. **Missing:** one **decision trace per opportunity** linking every stage, so a decision is auditable end to end; pattern and failure-prediction stages (Parts 28/27.5) |
| 27.1 | Continuous, scheduled, multi-symbol, multi-timeframe, parallel, prioritised, health, recovery, no duplicate jobs or orders | **A** | Scanner scheduler (Phase 8), role locks, idempotency, isolated jobs (Phase 20) |
| 27.1 | Event-driven analysis | **E** | Analysis is per closed bar. Missing: re-analysis triggered by events (news release, anomaly cleared, connector reconnect) |
| 27.2 | Technical research (price action, structure, trend, S/R, liquidity, volatility, momentum, breakouts, false breakouts, reversals, sessions, volume) | **A** | `app/analysis/*`, strategies, regime |
| 27.2 | Economic calendar, central banks, inflation, employment, rates | **B** | Calendar adapter with central-bank and macro keywords (`app/news/service.py`); one generic JSON provider; no provider configured by default |
| 27.2 | Financial news, geopolitical events | **E** | No news-headline source. Needs a provider (and its terms). Never fabricated: without a source, the research says "unavailable". |
| 27.2 | Correlations, cross-market | **A** | Peer symbols, correlation (Phases 8, 10) |
| 27.2 | Per-source provenance (provider, timestamps, reliability, quality, delay, availability) | **B** | Calendar fetch time and staleness exist. Missing: one **source registry** recording these fields for every external input |
| 27.3 | Specialists: research, technical, regime, structure, liquidity, strategy selection, risk, portfolio, execution quality, trade review, learning, discovery | **A** | 21 agents |
| 27.3 | Fundamental analysis, chart-pattern recognition, entry validation, exit optimisation, trade-failure prediction, AI consensus | **E** | Missing as responsibilities. They will be added to the **existing** registry and orchestrator, not as a second agent system. |
| 27.3 | Structured, validated, auditable responses; orchestrator can choose NO_TRADE | **A** | Agent contract and run log; NO_TRADE in the signal engine and fusion |
| 27.4 | Claude through the gateway | **A** | `AnthropicProvider` |
| 27.4 | OpenAI | **B** | Works through "Other OpenAI-compatible API". Missing: a first-class **OpenAI preset** (endpoint, model discovery) |
| 27.4 | Claude-only, OpenAI-only, fallback, retries, timeouts, rate limits, tokens, cost, health | **A** | Routes per task, fallback chain, ledger, health (Phases 9, 14) |
| 27.4 | **Dual-provider mode and configurable consensus** | **E** | One provider answers per request. Missing: ask both independently and combine by rule (agree / either may veto / require both) |
| 27.4 | AI → quantitative validation → Risk Engine → MT5; Risk Engine final authority | **A** | Enforced boundaries; static test (Phase 22) |
| 27.5 | Calibrated probability with model version, horizon, inputs, outcome | **A** | TP-before-SL model; `model_predictions` |
| 27.5 | Continuation, reversal, breakout, false breakout, volatility expansion/contraction, sweep, regime transition, holding period, expected adverse/favourable move | **E** | Only one target is modelled. New targets reuse the same training, signing and calibration pipeline. |
| 27.6 | Opportunity ranking | **B** | Candidate ranking in the scanner. Missing: the listed factors as an explicit score breakdown |
| 27.6 | Opportunity states (RESEARCHING → … → EXECUTED) with evidence per transition | **E** | Signals have NEW/EXPIRED/REJECTED/EXECUTED; there is no opportunity lifecycle with recorded transitions |
| 27.7 | Regimes: strong/weak trend, sideways, high/low volatility, breakout, reversal | **A** | Both regime engines |
| 27.7 | Accumulation, distribution, volatility expansion/contraction, news-driven, uncertain | **B** | Wyckoff strategies detect accumulation/distribution, but they are not regime labels; expansion/contraction and news-driven are not labels; uncertain = UNKNOWN only for insufficient data |
| 27.7 | Strategy selection adapts to regime | **A** | Strategy regimes, adaptive weights (Phase 12) |

### Part 28: chart pattern and market structure intelligence

| Req. | Requirement | Class | Evidence / gap |
|---|---|---|---|
| 28.1 | Preserve existing patterns | **A** | All are strategies, hash-verified |
| 28.1 | A pattern catalogue | **E** | No catalogue separate from strategies. One will be derived from the existing strategies (id, family, data requirements), never redefining them. |
| 28.2 | Classical, candlestick, SMC, ICT, Wyckoff, VSA, gaps, volatility, quantitative | **A** | See §1 |
| 28.2 | **Harmonic patterns** (Gartley, Bat, Butterfly, Crab, AB=CD) | **E** | None |
| 28.2 | **Elliott Wave structures** | **E** | None. Only a conservative impulse/correction labelling with explicit invalidation is honest. |
| 28.2 | Documented data requirements | **A** | `requires` per strategy; unavailable data reported (Phase 7) |
| 28.3 | Detection records (id, levels, confirmation, invalidation, quality, regime, version, data quality) with states FORMING → … → FAILED, without duplicates | **E** | Strategy signals are per bar and not persisted as formations |
| 28.4 | Multi-timeframe relationships | **B** | One higher-timeframe bias per analysis. Missing: alignment across all configured timeframes (higher, intermediate, lower) |
| 28.5 | Pattern-to-strategy (supporting, contradicting, regimes, outcomes) | **E** | |
| 28.6 | Pattern learning (success, failure, false breakouts, MAE/MFE, regime, session, spread) | **E** | Depends on 28.3. Trade MAE/MFE exist and are reused. |
| 28.7 | Pattern dashboard | **E** | |

### Part 29: strategy discovery, self-learning, automated trading

| Req. | Requirement | Class | Evidence / gap |
|---|---|---|---|
| 29.1 | Preserve all strategies | **A** | Preservation tests |
| 29.2 | Strategy selection on regime, suitability, history, OOS, drawdown, expectancy, PF, costs, correlation | **B** | Consensus, strategy stats, adaptive weights exist. Missing: an explicit **selection decision** (SELECT / COMBINATION / WAIT / REJECT / NO_TRADE) with its metrics recorded |
| 29.3 | Strategy combinations | **B** | 15 original combination strategies. Missing: evidence-based evaluation of new combinations, each validated separately (through the experimental pipeline) |
| 29.4 | Discovery (indicator combinations, parameters, rules, stops, filters, …) | **B** | LLM research + optimiser + sandbox. Missing: systematic **parameter/filter variants of existing strategies** as experimental candidates (no LLM needed) |
| 29.5 | Strategy DNA and versions with rollback | **B** | `strategy_versions`, fingerprints, experiments with lifecycle and rollback. Missing: one DNA record view joining them |
| 29.6 | Champion/challenger with full validation; overfitting and leakage checks | **A** (experiments), **B** (bias report) | Pipeline exists (Phase 11, sandbox). Missing: an explicit look-ahead/leakage/survivorship check report per candidate |
| 29.7 | Continuous trade learning (fields incl. MAE/MFE, costs, regime, rationale, AI references) | **A** | Part 25/26 collection + Phase 10 attribution. Pattern references come with Part 28. |
| 29.8 | Isolation of DEMO, LIVE, backtests, shadow, experiments | **A** | Tested (Phases 10, 16, 22) |
| 29.9 | Workflow with NO_TRADE on stale data, spread, missing confirmation, uncertainty, risk, exposure, R:R, conflicts, execution conditions | **A** | Signal engine, fusion, anomalies, Risk Engine |
| 29.9 | NO_TRADE when model or provider health is inadequate | **B** | An unhealthy provider leaves candidates unreviewed (so not executed); model health does not gate yet |
| 29.10 | Autonomous DEMO (selection → order → SL/TP → partial/BE/trailing → journal → learning) | **A** | Verified end to end in Phase 15 |
| 29.10 | LIVE after explicit admin activation, with a readiness checklist (identity, connection, config, LIVE limits, emergency config, DEMO acceptance, health) | **B** | Engine side exists (verify, approve, typed switch, Phase 16). **Missing:** releasing the **PC connector's LIVE lock** from RR Trading One; a **readiness checklist** that must pass before activation. It stays locked until you approve. |
| 29.11 | SL, TP, break-even, trailing, partial, emergency | **A** | Risk Engine + monitor |
| 29.11 | Time-based exit, maximum holding period, structure-based, volatility-based, invalidated-setup exit | **E** | |
| 29.12 | Exposure, correlation, currency, direction, aggregate stop risk, positions, daily/weekly loss, drawdown, margin | **A** | Risk Engine |
| 29.12 | Strategy concentration | **E** | |

### Part 30: reliability, data governance, capital protection, self-recovery

| Req. | Requirement | Class | Evidence / gap |
|---|---|---|---|
| 30.1 | Monitoring of MT5, account, workers, backend, frontend, PostgreSQL, Redis, AI providers | **A** | Phases 17, 20 |
| 30.1 | Reconnection, worker recovery, persistent execution state, recovery after restart | **A** | Relay reconnect, role locks, journal |
| 30.1 | Mandatory reconciliation (identity, positions, pending orders, history, compare, resolve, then resume) | **B** | All steps except **comparing pending orders** with the database (only counted) |
| 30.1 | Queue / scheduled-task monitoring | **B** | Learner job states recorded. Missing: backlog and overdue alerts |
| 30.2 | Spread, slippage, latency, rejection, partial fills, requotes, filling policy, restrictions, stop/freeze levels, costs | **A** | Execution analytics; symbol specs; pre-trade checks |
| 30.2 | Pre-validation, idempotency, state tracking, broker verification, post-execution reconciliation, safe retry | **A** | Phase 1–5 execution |
| 30.2 | Using execution history to improve handling | **B** | Analytics are reported, not fed back (e.g. per-symbol slippage budget) |
| 30.3 | Stale prices, spreads, gaps, feed interruption | **A** | Anomaly rules |
| 30.3 | Duplicate / out-of-order candles, broker server-time offset, historical gaps, quality score, source identification, historical data versioning | **B** | Missing: these specific checks, a per-symbol data-quality score, and a dataset version recorded with training data |
| 30.4 | Risk Governor independent of AI, able to reject what every AI recommends | **A** | The Risk Engine is that governor. It is deterministic, has no AI input, and covers per-trade, daily, weekly and monthly loss, drawdown, correlation, currency, margin, positions, lot size and consecutive losses. |
| 30.4 | Maximum floating loss, strategy-level risk allocation, volatility-adjusted sizing, cancel pending orders in emergency | **B** | Missing: these four controls |
| 30.4 | Emergency policies configurable and tested in DEMO | **A** | PROTECT/REDUCE/CLOSE_ALL; tests (Phase 22) |
| 30.5 | Scenario simulation (digital twin): continuation, reversal, volatility, spread and slippage shocks, gaps, alternative entries/exits | **E** | Backtest engine reused; isolated from execution |
| 30.6 | Counterfactual learning: rejected opportunities | **A** | `missed_opportunities`, hypothetical, excluded |
| 30.6 | Alternative entries, stops, targets, exit timing, strategies, timeframes | **E** | |
| 30.7 | Versions, calibration, drift, rollback, champion/challenger | **A** | Phase 11 |
| 30.7 | Dataset versioning, validation expiry, model and strategy suspension, degradation reducing influence | **B** | Missing: these rules |
| 30.8 | CPU, memory, disk | **A** | Phase 20 |
| 30.8 | DB connections, API latency, queue backlog, error rates, backup status, log anomalies | **B** | |
| 30.9 | Backup, integrity, restore, validation, DR | **A** | Phases 17, 21, 23 |
| 30.9 | Scheduled automatic backups and periodic restore tests | **B** | Backups are on request and before changes only |

### Section 3: dashboard

**Already there:** Command Centre, Control Room, Scanner, Signals, Trading Worker, Learning Centre, Adaptive,
Strategy Performance and Correlation, Predictive, Risk, Portfolio, AI Studio, System Monitor, Backup.

**New views needed:**
* **Pattern Intelligence** (Part 28);
* **Simulation Laboratory** (scenarios and counterfactuals);
* the opportunity lifecycle and decision trace (Command Centre / Signal Centre);
* capital-protection additions (Risk Centre);
* data-quality and execution-quality panels (System Monitor);
* model governance additions (Learning Centre).

Existing pages are extended; no separate application.

### Defects found (class D)

None in this audit. The defects of earlier phases were repaired and are recorded in their reports.

## 3. Totals

| Class | Count (rows above) |
|---|---|
| A: exists and works (skip) | 33 |
| B: extend | 22 |
| C: integrate | 0 (integration gaps are listed as B) |
| D: repair | 0 |
| E: implement | 17 |

71 requirement rows; one (29.6) is A for experiments and B for the bias report, so it is counted twice.

## 4. Implementation plan (Section 7, phases 4–10)

Every item extends an existing module. **New tables only where nothing existing can hold the data.** Planned
migration 0012 (additive): opportunity lifecycle, decision trace, pattern detections, source registry, scenario and
counterfactual results.

| Phase | Contents (class) | Extends |
|---|---|---|
| **4: Part 27** | Decision trace per opportunity (B); opportunity lifecycle and score breakdown (E/B); OpenAI preset (B); **dual-provider consensus** in the gateway (E); new agents: fundamental, pattern, entry validation, exit optimisation, failure prediction, AI consensus (E); source registry (B); regime labels: accumulation/distribution, volatility expansion/contraction, news-driven, uncertain (B); event-driven re-analysis (E) | `orchestration`, `ai/gateway`, `scanner`, `analysis/regime`, `news` |
| **5: Part 28** | Pattern catalogue from the existing strategies (E); **harmonic** and conservative **Elliott** detectors as new pattern detectors, added outside the 215 (E); detection records, lifecycle and dedupe (E); full multi-timeframe alignment (B); pattern-to-strategy (E); pattern learning (E); Pattern Intelligence page (E) | `strategies` (new module beside the 215), `scanner`, `learning_engine` |
| **6: Part 29** | Strategy selection decisions with metrics (B); systematic variants as experimental candidates (B); DNA view (B); bias report (B); time-, structure-, volatility- and invalidation-based exits (E); strategy concentration (E); model/provider health → NO_TRADE (B); LIVE readiness checklist (B) | `scanner`, `strategies/experimental`, `risk`, `workers` |
| **7: Part 30** | Pending-order comparison (B); queue backlog alerts (B); data checks (duplicates, order, server time) and quality score (B); dataset versions (B); floating-loss limit, strategy allocation, volatility sizing, cancel pending in emergency (B); scenario simulation (E); counterfactual alternatives (E); validation expiry and suspension (B); monitoring additions (B); scheduled backups and restore test (B) | `risk`, `workers`, `intelligence`, `backtesting`, `learning_engine`, desktop supervisor |
| **8: integration** | End-to-end decision trace from research to learning | — |
| **9: DEMO validation** | Autonomous DEMO run on the install (simulated broker), every stage recorded | — |
| **10: LIVE readiness** | Readiness report and the admin-only PC LIVE-lock control (typed, account-bound). **LIVE stays locked.** | desktop, connector |

## 5. Constraints restated

* No rebuild and no duplicates: the agent registry, gateway, scanner, Risk Engine, learner, execution and frontend are extended, not copied.
* The 215 strategies, their IDs, code and configuration are untouched; new detectors are added beside them.
* The Risk Engine stays final; nothing AI can loosen limits, enable LIVE, or promote its own work.
* **External data needs providers and keys** (OpenAI, Anthropic, news, calendar). Where none is configured, the engine says "unavailable" and never invents data.
