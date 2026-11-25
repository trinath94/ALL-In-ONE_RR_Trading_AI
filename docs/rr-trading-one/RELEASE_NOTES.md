# RR Trading One: release notes

## In development: Parts 27-30 (autonomous research and AI orchestration)

**Adaptive configuration and trade reviews (2026-11-25).**
* **Configuration profiles** (Strategy Library → a strategy → Configuration profiles): versioned parameter profiles per
  market regime, symbols and timeframes, created by hand or proposed by the active AI (bounded to 0.5x–2x of each
  default; untrusted). The strategy's code and defaults never change. A profile is used only after it passes every
  gate (out-of-sample sample size, expectancy at least the defaults', profit factor, drawdown, in-/out-of-sample
  stability, walk-forward, doubled-spread cost test, look-ahead and leakage checks) and is activated: on DEMO by an
  authorised user or, if the administrator allows it, automatically; on LIVE only by a user with LIVE permission,
  with the typed phrase and DEMO forward evidence. Roll back at any time (the validated parent version returns).
  Each signal records the configuration version it used.
* **Pre-trade analysis** (Opportunity Lifecycle → an opportunity): why the opportunity exists, what supports and
  contradicts it, invalidation, entry/stop/target justification, failure risks and whether it should be rejected.
* **Post-trade review** (Trade History → review): every closed trade answers the same questions (strategy, entry,
  regime, stop, exit, execution, prediction, net of costs, mistake, improvement). Losses are classified as normal
  variance, a mistake with evidence, or a suspected recurring defect (binomial test of the strategy's recent loss rate
  against its validated evidence): a loss is not assumed to be a mistake.
* **Automation policy**: automatic DEMO activation of validated profiles and automatic DEMO shadow tests of validated
  experiments, both off by default; LIVE is never automatic.
Migration 0019 (additive).

**Single Active AI (2026-11-25).** AI Studio → **Active AI**: one provider (Anthropic API, OpenAI, Qwen or Ollama) and
model serve every AI task, with an optional fallback used only when enabled. Without fallback, a failed provider stops
AI analysis: candidates stay unreviewed and are not auto-executed in With AI mode. The gateway never switches to an
unconfigured provider; every analysis records the provider and model that answered. Each provider shows its last
successful request and last error. Claude Pro (a Claude.ai subscription) is shown as not available: there is no
official way for an application to use it; use the Anthropic API. Off by default (per-task routes as before).
Migration 0018 (additive).

**Multi-account execution (2026-11-25).** The engine trades every execution account of the active mode at once: the
active account and every other account enabled for trading (verified; a LIVE account also approved, and only while
LIVE is the active mode). Each signal is evaluated separately for every account: its symbols and strategies, its own
Risk Engine limits and equity, its own idempotency key, through its own MT5 connector. Each account is reconciled at
start through its own connector before its first entry; the kill switch blocks entries everywhere and applies the
position policy on every account. In the desktop window each MT5 profile now runs its own connector (one terminal, one
account). Settings → Execution accounts shows what is traded now. Migration 0017 (additive).

**Multi-account foundation (2026-11-25).** Every trading account now has its own name, a trading switch (execution
account or research-only), eligible symbols and strategies, and its own risk limits over its mode's template
(Settings → Trading accounts → Settings). Without per-account values nothing changes. Enabling a LIVE account needs
`trade.live` and its LIVE approval; LIVE stays locked. Migration 0016 (additive): the active account of each mode
becomes its execution account. The engine still trades the active account; trading several accounts at once is the
next step.

**Engine with or without AI (2026-11-23).** A switch in the Live Trading Control Center runs the engine *With AI*
(default, unchanged: only AI-confirmed candidates are auto-executed) or *Without AI* (no AI review or cost; the
engine's own BUY/SELL signals are auto-executed). The Risk Engine and all risk limits apply in both. LIVE engine-only
needs `trade.live` and the typed phrase `RUN WITHOUT AI`. Migration 0015 adds `trading_state.ai_mode` (default `AI`;
existing data unchanged).

Incremental additions to 0.2.0; nothing existing is replaced.

* **Phase 4 (Part 27):**
  * **Opportunity Lifecycle:** each researched setup moves through states (monitoring, waiting, qualified, rejected, no trade, expired, executed), with a stage-by-stage decision trace;
  * **five new agents** in the same registry: fundamental (economic calendar), entry validation, exit plan, failure-risk factors, AI consensus;
  * **OpenAI preset**, and an optional **consensus mode** in which two AI providers must agree;
  * **regime tags**: volatility expansion/contraction, accumulation/distribution, uncertain, news-driven;
  * **data-source registry** with observed health;
  * **event-driven re-analysis** on news releases, anomalies, and price reaching a waiting entry.
  * Database migration 0012 only adds tables and one column.
* **Phase 5 (Part 28):**
  * **Pattern Intelligence:** harmonic (Gartley, Bat, Butterfly, Crab, AB=CD) and conservative Elliott impulse detectors beside the 215 strategies (not added to them);
  * detection records with a lifecycle (forming, completed, confirmed, target reached, invalidated, expired), no duplicates;
  * the strategies that support or contradict each formation;
  * outcome statistics measured on price bars, with sample sizes;
  * alignment across all scanned timeframes;
  * a pattern catalogue derived from the strategies;
  * a pattern agent (27 agents).
  * Detections are advisory and never change a decision. Migration 0013 only adds one table.
* **Phase 6 (Part 29):**
  * **strategy selection** decisions (select, combination, wait, reject, no trade) with their evidence;
  * model and provider **health** in the decision trace;
  * systematic **parameter variants** as experiments, with a **bias report** (look-ahead, leakage, survivorship, multiple testing);
  * a **strategy DNA** view;
  * optional **time, volatility, structure and invalidation exits** and a **strategy concentration** limit (all off by default);
  * a **LIVE readiness checklist**, which the LIVE switch now requires, including a DEMO track record. No database migration.
* **Phase 7 (Part 30):**
  * **reconciliation:** startup compares pending orders with the journal; the kill switch cancels the engine's own pending orders;
  * **capital protection:** floating-loss limit, per-strategy risk allocation and volatility-adjusted sizing (off by default);
  * **data quality:** duplicate/out-of-order candles, invalid prices, gaps, quote age, with a quality score; trained models record **dataset versions**;
  * **model governance:** model validation expiry; strategy suspension recommendations;
  * **monitoring:** API latency and error rate, error logs per service, database connections, job backlog alerts;
  * a **Simulation Laboratory** (scenario stress tests, counterfactual alternatives);
  * daily **automatic backups** and weekly **restore tests** in the desktop application.
  * Migration 0014 only adds one table.
* **Phase 8 (integration):**
  * one end-to-end test of the whole flow in DEMO, from the scan to an executed, traced trade;
  * partial fills verified;
  * the 35 required validations proven in CI by the test matrix;
  * decision traces in the Signal Centre and the Command Centre.
* **Phase 9 (DEMO validation):** the DEMO Validation page and its runbook (`DEMO_VALIDATION.md`); acceptance itself happens on your PC.
* **Phase 10 (LIVE readiness):**
  * the PC LIVE lock can be authorised and revoked from the window (typed, account-bound, checked by the connector);
  * the readiness checklist shows the lock's real state;
  * **nothing was unlocked:** LIVE stays locked until you decide.

## Version 0.2.0 (development release, 2026-11-17)

The first complete development release of RR Trading One: the RR Trading AI Engine as one Windows desktop
application. **It is for DEMO use and acceptance testing**, not yet for LIVE trading (see Known limitations).

### What it contains

* **One Windows application** (Tauri 2) that installs, starts and supervises the whole engine locally:
  * PostgreSQL, Redis, the FastAPI engine and the Next.js console in Docker Desktop;
  * the MT5 connector natively in your Windows session.
* **The original engine, preserved:**
  * all 215 strategies, verified by ID, configuration and code hashes;
  * risk, execution, learning, intelligence and backtesting unchanged in behaviour;
  * all 112 original API routes kept.
* **Broker symbol discovery** with your choice of what is scanned. **A scanner** that analyses each closed bar once, within a time budget, with only the strategies valid for each timeframe.
* **21 specialist agents** with a Control Room, and an **AI Studio** supporting several providers (Anthropic, OpenAI-compatible, Qwen, local Ollama). These have routes, fallbacks, limits and a cost ledger, and are always advisory.
* **Historical trade intelligence and learning:** broker-sourced trade history, attribution, signed models with walk-forward validation and administrator approval, adaptive strategy weighting in shadow until approved, and calibrated probability and signal fusion that never makes a decision more aggressive.
* **DEMO and LIVE separation:**
  * initial state DEMO, worker stopped, LIVE locked;
  * LIVE needs verification, approval and typed confirmations, and can be revoked.
* **A 30-page console** following the specification's navigation, and a desktop Control Centre with a first-run wizard.
* **Backup and recovery:**
  * encrypted backups, with an exportable backup key;
  * one-transaction restores that refuse damaged or modified backups.
* **Migration from the Ubuntu deployment:** a read-only server export, plus a verified, content-checked import with an explicit replace policy and a migration report.
* **Security:**
  * loopback-only engine; signed and replay-protected local and connector traffic;
  * secrets DPAPI-protected and redacted from logs; secret rotation;
  * dependency scanning in CI.
* **Resource management:** CPU priority for position protection and execution over learning, learning jobs that are really stopped on timeout or cancel, hot-path database indexes, and per-role CPU, memory and disk monitoring.
* **Tests:** engine 323, MT5 bridge 17, RR MT5 Connector 36, desktop 130. Every area and safety condition of the specification's Part 22 is mapped to passing tests, checked in CI. The Windows installer is installed, upgraded and uninstalled in CI.

### Known limitations

* **LIVE trading (updated in Parts 27–30 Phase 10).** The PC's MT5 connector keeps its own LIVE lock. RR Trading One can now authorise it from the window, with the typed account number and phrase checked by the connector against the connected LIVE account. Every LIVE step is tested against a simulated REAL account only; **none was performed on a real LIVE account**, and LIVE remains locked on every installation until its administrator decides otherwise.
* **No automatic updater, and the installer is not code-signed.** Both need your decisions: release hosting, an update-signing key and, ideally, a code-signing certificate. Updating means installing the newer installer (data is kept). Windows SmartScreen may warn.
* **Not run on a real Windows PC with a real broker during development.**
  * The engine was tested on Linux Docker, the connector against a simulated MT5 terminal.
  * The installer was tested on GitHub's Windows runner, without Docker Desktop or MT5.
  * **Acceptance testing on your PC, with a DEMO account, is the next step.**
* **Remote engine mode** is only a setting; local mode is the supported mode.
* **Data-encryption keys** (broker passwords, AI keys, model signatures, backups) cannot be rotated yet.
* **History is not pruned.** The database grows with use (about 1.2 GB per 40 days for 50 symbols in our measurement).
* **Upgrade testing** used a reinstall of the same version over modified files, because no earlier release exists.

### Upgrading from the Ubuntu deployment

See the [migration manual](../MIGRATION_FROM_UBUNTU.md). Keep the server until you accept the PC.

### Documentation

[Installation](INSTALLATION_MANUAL.md) · [User](USER_MANUAL.md) · [Administrator](ADMINISTRATOR_MANUAL.md) ·
[Architecture](ARCHITECTURE.md) · [Security](SECURITY.md) · [Migration](../MIGRATION_FROM_UBUNTU.md) ·
[Recovery](RECOVERY_MANUAL.md) · Phase reports `docs/RR_TRADING_ONE_PHASE*_REPORT.md`.
