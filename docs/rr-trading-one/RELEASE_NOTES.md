# RR Trading One: release notes

## In development: Parts 27-30 (autonomous research and AI orchestration)

**Trader discipline: daily stop, key levels, A+ setups only (2026-11-27).**

Set these in the Risk Centre. Each one is off (0) until you set it, or apply the "Professional trader discipline" profile
(it keeps your own loss limits); the FTMO-style profile includes them too.

1. **Daily discipline** (Risk Engine, on every order):
   * the number of new trades per day, and per symbol per day, is limited;
   * the engine is done for the day after N losing trades (this is not a risk lock: it resets the next day);
   * after N losses in a row, risk per trade is reduced (half by default; it is never raised).
2. **Key levels**, computed by the engine itself from the bars it already loads, with no AI:
   * previous day high, low and close;
   * week open, previous week high and low;
   * today's Asian range;
   * round numbers.

   They are recorded with every analysis, with the distance from the price to the nearest level in ATR.
3. **A+ setups only.** A seven-point checklist, scored by the engine:
   * grade A+;
   * the higher timeframe agrees;
   * the entry is at a key level;
   * two or more strategies agree;
   * the playbook bias agrees;
   * confidence of at least 55 %;
   * reward:risk of at least 2.

   A candidate below the minimum score, or too far from a key level, is held back in the scanner before any AI call, so
   it costs no tokens. The score is recorded on every candidate.

All of this can only hold back a trade or reduce its size. Nothing forces a trade, and the Risk Engine still decides
every order.

**Market playbooks: trader plans the engine saves and reuses (2026-11-27).**

For each symbol and market regime, the AI writes one trading plan like a professional trader:
* the directional bias and the directions to trade;
* the strategy families that suit the market;
* conditions to avoid;
* the risk posture: normal, reduced (half size or less) or stand aside.

It writes the plan from the engine's own facts: multi-timeframe analysis, regime, volatility, spread, sessions, news and the symbol's recent results.

**How the engine uses a plan:**
* It applies the plan to every signal on that symbol itself, with no AI call per trade.
* A plan can only hold back a trade or reduce size. It never forces a trade, never raises size and never bypasses the Risk Engine.
* Each plan is graded from the trades it governed. A plan that made money is reused for the same symbol and regime without asking the AI again.
* A regime change retires the current plan.
* Limits: at most 12 new AI plans a day and 12 hours of validity (both adjustable in AI Studio → Market playbooks), within the AI budget.
* Without an AI key, the engine trades exactly as before.

Migration 0025 (additive).

**FTMO-style risk profile and risk lock reset (2026-11-27).**

*Apply profile → FTMO-style* (Risk Centre). The profile fills the form for you to review before saving. Its values:
* trading locks at 4 % daily loss and 9 % drawdown (FTMO's limits are 5 % and 10 % of the initial balance);
* floating loss is counted (3 %);
* 0.5 % risk per trade;
* at most 3 positions, and 1 per symbol;
* reward:risk at least 1.5;
* take profit required;
* no entries when news risk is unknown.

Initial capital, lot size, spread and trade-management values keep their current values. Set initial capital to your FTMO account size, and verify the current rules of your FTMO account type. There is also an *Engine default* profile.

*Reset risk lock* (Risk Centre). It needs risk.manage and the typed confirmation "RESET RISK LOCK"; in LIVE it also needs trade.live. Every reset is audited.
* It bypasses nothing: every loss limit is checked again on the next order, and trading locks again if a limit is still breached.
* The kill switch is separate (Control Center).

**Trades not executing: built-in economic calendar and execution diagnosis (2026-11-26).**

*Built-in economic calendar.* The installed engine had no economic calendar configured. News risk was therefore UNKNOWN for every symbol, and the Risk Engine refused every entry ("News feed unavailable"). The engine now uses a built-in calendar (the public weekly economic calendar; no key needed; refreshed hourly). Entries are allowed except around high-impact events for the symbol's currencies. The protection itself is unchanged: if the calendar cannot be reached, news risk is UNKNOWN again and entries wait.

*Execution diagnosis.* Ask "Why are trades not executing?" in the Engine Assistant, or open System Check. It shows every gate a signal must pass, with the fix for each closed one:
* trading worker running;
* Auto trading on;
* kill switch, risk lock and paused entries clear;
* startup checks passed;
* execution account available and reconciled;
* candidates not waiting for the AI;
* news calendar available.

It also counts why the last 24 hours' signals were not executed, including the Risk Engine's actual rejection reasons, each with its fix. Nothing is changed by the diagnosis.

**Engine-first operation: much lower Anthropic token usage (2026-11-26).** The engine now makes every routine decision itself. The Anthropic API improves the engine and is no longer part of every trade decision.

* **Scan candidates:** they are no longer sent to the AI one by one. The engine's validated signal goes to the Risk Engine.
  * AI Studio → Engine autonomy → *AI review of scan candidates*: Off (default), Selective (only candidates the engine is uncertain about, with a daily cap) or All (the previous behaviour).
  * A signal the AI rejected is never executed.
* **Engine Assistant:** it answers from the engine's records without the AI. AI phrasing is optional. Analyses you ask for ("market summary", "review my trades"…) still use the AI.
* **Automatic improvement research.** Once a day, the engine looks for strategies losing more than their validated evidence explains. For each, it asks Anthropic for one configuration proposal (at most 2 a day by default, within the budget).
  * The proposal is saved, versioned and validated by the engine before any use, with rollback available.
  * You can also ask in the chat: "ask Anthropic to improve <strategy>".
* **New chat commands:**
  * "can the engine operate without Anthropic?" and "reduce token usage": autonomy report;
  * "identify weak strategies";
  * "discover new strategy candidates";
  * "complete the autonomous upgrade".
* **Engine autonomy panel** (AI Studio):
  * internal workload versus AI calls;
  * engine versus AI decisions;
  * tokens, cost and budget, split into routine and improvement;
  * Claude Code tasks;
  * improvement proposals and their validation.

Migration 0024 (additive) sets the AI review of scan candidates to Off on every installation.

**System check agent and DEMO test trade (2026-11-26).**

*System check.* It runs when the console opens and every 5 minutes, and you can run it from the **Check** button in the header. It checks:
* the database;
* engine services and Redis;
* the MT5 connector and broker account, with a live probe of connection, account identity and quotes;
* broker symbols and market data;
* the Risk Engine and trading controls;
* strategies;
* AI (Anthropic API);
* open incidents.

When a system is failing, a pop-up shows what is wrong, the evidence, the impact on trading, what to do, and where.

*Execute a DEMO test trade.* This button, inside the check, proves the whole execution path:
1. pre-flight check;
2. Risk Engine approval, with every control applying (kill switch, risk lock, limits, stop loss);
3. order sent through the MT5 connector;
4. broker fill;
5. position visible in the engine;
6. position closed again and the close confirmed.

The pop-up stays open until the test has finished and shows each step. The test trade:
* is DEMO only and refused in LIVE;
* uses the broker's minimum volume, with a stop loss and take profit;
* costs the spread;
* needs trade.manual and is audited;
* is never attributed to a strategy, and never counted toward DEMO acceptance or LIVE readiness.

**Remove an AI configuration (2026-11-26).** AI Studio → Anthropic API → **Remove** asks for confirmation. It then:
* deletes the configuration and its stored key;
* takes it out of every task route;
* clears it from Active AI and its fallback.

Afterwards it says what was affected. For example, if it was the Active AI provider, it asks you to choose another. The usage and cost history is kept, and the key itself stays valid in the Claude Console until you revoke it there.

**Anthropic API connection test pop-up (2026-11-26).** Saving a key, or clicking Test, opens a pop-up that stays open until the test has finished. It shows each step:
1. key stored;
2. key readable;
3. Anthropic API reached;
4. valid answer.

If the test fails, the pop-up names the reason and the fix: no API credits, key rejected, key not permitted, model not available, rate limited, Anthropic API unreachable, or budget reached. It also offers Edit key and Test again. A manual test is never blocked by a cooldown from earlier failures.

**Installer: upgrades with the app and MT5 connectors running (2026-11-26).** Before writing any file, the installer
now stops the desktop app first (so it cannot restart the connector), asks every MT5 connector instance to stop, and
after 20 seconds stops anything still running from the install folder. This fixes "Error opening file for writing …
RR_MT5_Connector.exe" during an upgrade. Data and settings are untouched.

**Anthropic API only; easier key management (2026-11-25).**
* **Other connectors removed.** The engine now uses only the Anthropic API. The Qwen, OpenAI, Ollama and other
  OpenAI-compatible connectors are gone, and migration 0023 deletes any such provider with its stored key and route
  entries.
* **API key.** Edit lets you replace the key or remove it. **Test** no longer needs a model: it uses the engine's
  configured Claude model. Saving a new key tests it automatically.
* **Guide.** AI Studio has a step-by-step guide to creating an Anthropic API key in the Claude Console and adding it.

**Claude Code and the Anthropic API in the Engine Assistant (2026-11-25).**

The chat now routes each request to one of three handlers:
* **Claude Code**, through the official Claude Agent SDK, for development work: start a request with `/code`.
* **The Anthropic API**, for analyses: market summary, trade review, performance, strategy research, error explanation.
* **The engine itself**, for everything about the running engine.

**Claude Code tasks.**
* Each task works in an isolated git branch of a copy of the engine source, in the new `claude-code` service.
* Every file, shell and tool call is checked by the engine: no secrets, nothing outside the task's workspace, no
  web, only listed commands.
* The chat shows live progress, with **Cancel** and **Resume**.
* When Claude finishes, the engine runs the test suite itself, against a separate test database.
* The patch is shown with its approval gates: migration, order execution, risk, LIVE, credentials, deletions.
* An administrator merges only after the tests pass and each gate is acknowledged. It is never deployed from there.

**Anthropic API analyses.** Each analysis is its own AI task with its own model choice, and uses the engine's records
only.

**Budget alert.** New threshold, with an incident when it is reached. The usage ledger now records prompt-cache
tokens.

**Defaults and changes.**
* Off by default (AI Studio → Claude Code). The engine trades without it.
* Migration 0022 (additive).
* New dependency: `claude-agent-sdk`; `pytest` is now in the engine image.

**Engine Assistant commands, AI budget, continue without AI (2026-11-25).**
* **Commands in the Engine Assistant**: "Audit my entire trading engine", "Find and repair all errors", "Is the engine
  ready for autonomous trading?", "Inspect all connected broker accounts", "Check all available trading symbols",
  "Find missing features", "Test the engine using the DEMO account", "Prepare the engine for LIVE trading",
  "Complete the upgrade and hand control back". The agent inspects the whole engine (database and migrations,
  services, MT5 connectors and accounts, broker symbols, AI and budget, risk configuration, trading controls,
  strategies against the baseline, data quality, scanner, learner, incidents, DEMO validation, LIVE readiness) and
  lists every finding with evidence, impact and the fix: a safe action it can run, your step, or a repair proposal for
  the developer. Safe actions run only after an administrator clicks **Approve plan**; each is verified and recorded.
  The agent never edits code, runs migrations, enables LIVE, changes risk limits or trading controls, starts or stops
  the trading worker, or places orders (owner's decision).
* **AI API budget** (AI Studio → Active AI): daily and monthly USD and a daily token limit; at the limit AI calls are
  skipped and the engine keeps running. Identical AI requests are answered from a short cache.
* **If AI is unavailable**: pause auto-execution (default, unchanged) or continue without AI, so the engine keeps
  trading on its own validated signals through the Risk Engine when the provider is down, rate-limited, out of budget
  or not configured. A signal the AI rejected is never executed.
Migration 0021 (additive).

**Engine Assistant (2026-11-25).** "Ask the engine" (bottom right of every console page) and **Ask why** on a signal
(Signal Centre). It reads the engine's own records and walks a signal through the trading worker's gates in order:
decision, validity, trading worker, auto trading, AI review (or engine-only), kill switch, risk lock, paused entries,
execution accounts; then per account eligibility, reconciliation, the Risk Engine's decision and reasons, and the order
and broker result. It says where the signal stopped and the next step (the page or desktop action). Questions about
the engine's state get the current status and what blocks automatic trading. Read-only: it never changes anything. With
an AI provider (task `engine_assistant`, or Single Active AI) the AI phrases the answer from these facts only; without
one, the engine answers by itself.

**Error detection, pop-up alerts and self-healing (2026-11-25).**
* **Incidents** (System Monitor → Incidents): every warning, error and critical event the services already report
  (connector, execution, worker, risk, monitor), unhandled API errors, console errors and new detectors (MT5 bridge
  down, learner jobs stuck, memory/CPU pressure, low disk, AI providers cooling down, data sources failing, API 5xx
  rate) become classified incidents, de-duplicated (one incident with a count, re-opened if it comes back), each with
  an explanation, a diagnosis and the recommended fix.
* **Pop-up alerts** in the console for new, re-opened and escalated incidents (warning and above); critical ones stay
  until dismissed. Acknowledge from the pop-up.
* **Safe self-healing** (on by default, switch in the Incidents panel): reconcile the execution journal (never
  resubmits), re-discover the symbol catalogue, mark jobs stuck beyond twice their timeout as interrupted, prune
  history beyond retention, re-check the MT5 connection. Every repair is verified and recorded; three failures
  escalate to a critical notification. Incidents also close by themselves when their condition clears (for example
  MT5 reconnected, or an administrator reset the kill switch). Self-healing never changes the kill switch, risk lock,
  orders, positions, accounts or LIVE, and never restarts the trading worker.
* **Diagnostic assistance**: Ask AI to diagnose (the active AI explains causes and manual steps; advice only, never
  executed).
* **Console error boundary**: a failing page shows a message and reports itself; other pages keep working.
Migration 0020 (additive).

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
