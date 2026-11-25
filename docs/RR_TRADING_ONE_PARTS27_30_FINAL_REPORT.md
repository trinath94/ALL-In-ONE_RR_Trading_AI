# RR Trading One — Parts 27–30: Final Development Report (Section 9)

**Date:** 2026-11-17 · **Branch:** `claude/focused-cori-b8em6u` · **From** `7a64be3` (audit) **to** `2bc29dd` (Phase 10)
**Scope:** 95 files changed, +6,883 / −68 lines. Phase reports: `docs/RR_TRADING_ONE_PARTS27_30_PHASE{4..8}_REPORT.md`
(Phases 9 and 10 are reported here, §9–§10). Audit and plan: `docs/PARTS27_30_AUDIT.md`.

This was an **incremental enhancement**:
* **Not done:** no rebuild, no replacement, no second scanner, gateway, scheduler, learning engine or execution path; no deleted strategy, no database reset.
* **Risk controls:** none weakened. Every new limit is off by default; the LIVE switch became **stricter**.
* **LIVE:** never enabled, and **nothing ran against a real LIVE account**.

---

## 1. Existing functionality reused (deliberately unchanged)

| Area | Kept as is |
|---|---|
| Strategies | The 215 original strategies: IDs, source, configuration, hashes (preservation tests). New detectors live **beside** them (`app/patterns`), not in the registry. |
| Signal engine, consensus, probability, fusion, adaptive weights | `signals/engine.py`, `intelligence/{consensus,probability,fusion,regime,anomaly}`, `learning_engine/adaptive.py`: decisions unchanged in the default (advisory) modes |
| Risk Engine checks | Every existing check and limit (`risk/engine.evaluate`), emergency policies, break-even, partial, trailing |
| Execution | `execution/manager.py`: idempotency, journal, `_send`, `_apply_result`, `manage`; the MT5 connectors' journal, HMAC and replay checks |
| AI boundaries | `enforce_boundaries`: AI may only confirm, downgrade or reject, for every provider |
| Learning, models, experiments | Learner jobs, signing, walk-forward, approval and rollback; the experimental sandbox and its lifecycle |
| DEMO/LIVE separation, account verification and approval, kill switch, PC emergency stop | Unchanged; the LIVE switch only gained a stricter pre-condition |
| Desktop supervisor | Allow-listed actions, backups (encrypted), restore, import, secrets |

## 2. Existing functionality extended

| Module | Change |
|---|---|
| `orchestration/agents.py` | 21 → **27** agents, same closed registry: fundamental, entry validation, exit optimisation, failure prediction, AI consensus, pattern recognition |
| `scanner/service.py` | Per analysis: data-quality check; pattern detection; opportunity record with a 23-stage decision trace; selection and health stages; multi-timeframe alignment; event-driven re-analysis; supersede instead of duplicate signals |
| `ai/gateway.py`, `ai/providers.py` | OpenAI preset; parameter fallback for models that reject `max_tokens`; route mode **consensus** (two providers must both confirm) |
| `analysis/regime.py` | Extra tags: volatility expansion/contraction, accumulation/distribution, uncertain, news-driven (primary label unchanged) |
| `risk/engine.py` | Strategy concentration, floating-loss limit, per-strategy risk allocation, volatility sizing (only reduces); optional time, volatility, structure and invalidation exits. **All off by default.** |
| `workers/main.py`, `workers/roles.py` | Optional exits through the journalled manager; startup reconciliation **compares** pending orders (unknown engine orders block entries); job-backlog alerts; error counting |
| `execution/manager.py` | Kill switch also cancels the engine's own pending orders (journalled); volatility percentile passed to the Risk Engine |
| `api/routes/trading.py` | The LIVE switch now requires the readiness checklist |
| `learning_engine/{scheduler,models,consensus_weights,adaptive}.py` | Dataset versions on every model; variant evaluation in the experiments job; **learner recovery** of jobs left RUNNING (defect fix) |
| `news/service.py` | Records calendar health in the source registry |
| Windows connector (`headless.py`) | The PC LIVE lock can be authorised or revoked from RR Trading One, through its own unchanged `authorize_live` |
| Desktop (`supervisor.rs`, `connector.rs`, `lib.rs`, UI) | Daily automatic backups and weekly restore tests; PC LIVE-lock control; two new commands (24 → 26) |
| Console | AI Studio (consensus mode), Strategy Library (DNA, variants), Risk Centre (new parameters), System Monitor (reliability panels), Signal Centre (decision trace), Command Centre (opportunity panel) |

## 3. Newly implemented functionality

* **Opportunity lifecycle** with a stage-by-stage **decision trace**.
* **Data-source registry** with observed health.
* **Event-driven re-analysis:** news release, anomaly, or price moving into a waiting entry.
* **Pattern intelligence:**
  * harmonic (Gartley, Bat, Butterfly, Crab, AB=CD) and conservative Elliott detectors;
  * detection lifecycle, strategy relations, outcome statistics, pattern catalogue;
  * multi-timeframe alignment.
* **Strategy research:** selection decisions; systematic parameter variants as experiments; bias report (look-ahead, leakage, survivorship, multiple testing); strategy DNA.
* **LIVE readiness checklist** (required by the LIVE switch).
* **Data quality and versions:** market data-quality score; dataset versions.
* **Governance:** model validation expiry; strategy-suspension recommendations.
* **Monitoring:** API latency and errors, error logs per service, DB connections, backlog.
* **Simulation Laboratory:** scenario stress tests and counterfactual alternatives.
* **Desktop:** scheduled backups and restore tests.
* **DEMO validation report** and runbook.
* **PC LIVE-lock control** from the desktop window.

## 4. Files changed

**Engine: new modules**
* **Opportunities, sources, events:**
  * `app/db/models_opportunity.py`: tables of §5;
  * `app/intelligence/opportunity.py`: lifecycle, trace, the four deterministic views;
  * `app/services/sources.py`: source registry;
  * `app/scanner/triggers.py`: event-driven re-analysis.
* **Patterns:** `app/patterns/{pivots,lifecycle,harmonic,elliott,catalogue,service}.py`.
* **Selection, research, readiness:**
  * `app/intelligence/selection.py`: selection and health;
  * `app/strategies/experimental/variants.py`: variants and bias report;
  * `app/services/readiness.py`: LIVE readiness.
* **Part 30:**
  * `app/execution/reconcile.py`: pending-order comparison, emergency cancel;
  * `app/intelligence/data_quality.py`;
  * `app/learning_engine/{versioning,governance}.py`;
  * `app/services/monitoring.py`;
  * `app/backtesting/scenarios.py`.
* **DEMO validation:** `app/services/demo_validation.py`.

**Engine: modified.** The files in §2, plus `api/routes/{ai,intel}.py` (endpoints, §6), `core/config.py` (settings), `db/models{,_ai}.py` and `main.py` (latency middleware).

**Migrations:** `migrations/versions/0012_opportunities_sources.py`, `0013_pattern_detections.py`, `0014_simulation_runs.py`.

**Tests**
* **New:** `tests/test_part27.py` … `test_part30.py`, `test_parts27_30_integration.py`, `test_part31_demo_validation.py`.
* **Changed** (justified in the phase reports):
  * `test_orchestration.py`: the 21 originals are still checked, plus the new agents;
  * `test_live_separation.py`: now asserts the stricter LIVE switch, then provides the DEMO record;
  * `conftest.py`: new tables.
* **Simulated broker:** `mt5-connector/tests/fake_mt5.py`, now with pending orders and partial fills.
* **Windows connector:** `windows-connector/tests/test_headless.py`.

**Desktop:** `core/src/{supervisor,actions,connector}.rs`, `src-tauri/{src/lib.rs,build.rs,capabilities/main.json}`, `ui/{backup.js,mt5.js,index.html}`, `README.md`.

**Console:**
* new pages: `opportunities`, `patterns`, `readiness`, `simulation`, `validation`;
* modified: Command Centre, Signal Centre, AI Studio, Strategy Library, Risk Centre, System Monitor, Agents, Shell.

**CI and docs:**
* `scripts/test_matrix.py`, `.github/workflows/backend.yml`;
* `docs/TEST_MATRIX.md`, the manuals, `docs/rr-trading-one/DEMO_VALIDATION.md`, phase reports.

## 5. Database changes

| Migration | Adds |
|---|---|
| 0012 | Tables `opportunities` (indexes: signal, created_at, symbol+tf+id, state+id), `opportunity_transitions`, `data_sources` (unique key); column `ai_task_routes.mode` (NOT NULL, server default `fallback`, the original behaviour) |
| 0013 | Table `pattern_detections` (unique `key`; indexes: created_at, symbol+tf+id, pattern+state) |
| 0014 | Table `simulation_runs` (index created_at) |

* **Purely additive:** no existing table, column or row is changed or removed. Foreign keys use CASCADE / SET NULL so deleting a symbol or analysis stays consistent.
* **Rollback:** `alembic downgrade 0011` drops only these.
* **Existing data preserved, verified on the installed application at each step:**
  * 0011 → 0012: 68 analyses, 25 signals, 215 strategies kept;
  * 0012 → 0013: 72 / 25 / 215 kept;
  * 0013 → 0014: 78 analyses, 25 signals, 215 strategies, 401 closed trades kept.

## 6. API changes

**New**

| Endpoint | Access |
|---|---|
| `GET /api/opportunities` (filters: state, symbol, timeframe, signal_id) · `GET /api/opportunities/summary` · `GET /api/opportunities/{id}` | session |
| `GET /api/data-sources` | session |
| `GET /api/patterns/catalogue` · `/patterns/detections` · `/patterns/detections/{id}` · `/patterns/stats` · `/patterns/alignment` | session |
| `POST /api/strategies/{code}/variants` | `strategy.manage`, audited |
| `GET /api/strategies/{code}/dna` | session |
| `GET /api/live/readiness` | `trade.control` |
| `GET /api/governance` · `GET /api/data-quality` · `GET /api/system/monitoring` | session |
| `POST /api/simulations` | `backtest.run`, audited |
| `GET /api/simulations` | session |
| `GET /api/validation/demo` | session |

**Modified**
* `GET/PUT /api/ai/routes[/{task}]`: field `mode` (fallback | consensus; consensus for candidate review only, needs two different providers).
* `GET/PUT /api/risk/config/{mode}`: the new risk parameters, validated and audited like the others.
* `POST /api/control/mode`: LIVE now refused (409) unless the readiness checklist passes.

**Unchanged:** all 112 original HTTP routes and both WebSockets still exist (tested).

## 7. Testing report

**Commands** (repository root; services: PostgreSQL, Redis):

```
cd backend && python -m pytest -q --junitxml=backend.xml
cd mt5-connector && python -m pytest -q --junitxml=mt5.xml
cd windows-connector && QT_QPA_PLATFORM=offscreen python -m pytest -q --junitxml=wincon.xml
cd desktop && cargo test -p rr-one-core --locked > cargo.txt && cargo clippy -p rr-one-core --all-targets -- -D warnings
python scripts/test_matrix.py --junit backend/backend.xml --junit mt5-connector/mt5.xml --junit windows-connector/wincon.xml --cargo desktop/cargo.txt --ci .github/workflows/desktop.yml --ci .github/workflows/frontend.yml --markdown docs/TEST_MATRIX.md
```

| Suite | Baseline (before Parts 27–30) | Now |
|---|---|---|
| Engine | 323 passed | **402 passed**, 0 failed |
| MT5 connector | 17 | **17** |
| Windows connector | 36 | **38** |
| Desktop core | 130 | **134**; clippy clean |
| Test matrix | 25 areas, 14 safety conditions | **25 areas, 15 safety conditions, 35 Section 6 validations: all proven by passing tests** (CI gate) |
| CI on `2bc29dd` | — | backend ✅ 44076490181, desktop ✅ 44076489974 (Windows installer build, install, upgrade, uninstall), windows-connector ✅ 44076489904; frontend ✅ 44072036971 (last frontend change, `fd73b40`) |

**Baseline failures:** none. The baseline was all green, and no baseline test fails now.

**Failures introduced and fixed during development** (each recorded in its phase report):
* **Advisory gate dropping WAIT signals:** the first entry-validation gate turned the engine's level-less WAIT into NO_TRADE (Phase 4). Found by a new test.
* **Redundant event re-analysis:** caught by an existing test (Phase 4).
* **Test-path errors** in pattern tests (Phase 5).
* **Desktop CI:** an MSRV (minimum Rust version) clippy failure (Phase 7).
* **Learner jobs stuck RUNNING:** a real defect, found on the installed stack by the new monitoring (Phase 7).

**Baseline tests changed:** three, all for intended behaviour changes, never to hide a failure (§4).

## 8. Integration report

One path, in the existing loops:

1. **Market research:**
   * analysis role: bars with a data-quality check;
   * regime with the new tags;
   * the 215 strategies;
   * pattern detectors;
   * multi-timeframe alignment;
   * news and calendar (fundamental view);
   * the source registry records feed health.
2. **Quantitative validation:**
   * signal engine, adaptive weights, consensus, probability, anomalies, fusion;
   * then entry validation, exit plan, failure prediction, strategy selection, model/provider health;
   * all recorded in the opportunity's decision trace.
3. **AI analysis:** shortlisted candidates go through the gateway (fallback or two-provider consensus). Output is schema-validated and boundary-checked, and can only confirm, downgrade or reject.
4. **Risk Engine:** final authority. Every existing check, plus the new limits when enabled. The trace records APPROVED/REJECTED. The integration test shows it refusing orders that the AI consensus had confirmed.
5. **MT5 execution:**
   * the trading role's auto-execution goes through the journalled, idempotent execution manager and the connector;
   * on the PC, the connector's own guards apply;
   * reconciliation at start compares positions and pending orders;
   * the kill switch protects positions and cancels the engine's pending orders.
6. **Management:**
   * monitor role: break-even, partial and trailing;
   * optional exits;
   * the opportunity follows its signal to EXECUTED / REJECTED / EXPIRED.
7. **Learning:**
   * **learner role:** trade sync, failure analysis, models (now with dataset versions), drift, governance (validation expiry, suspension recommendations), variant evaluation with bias reports;
   * **Simulation Laboratory:** scenarios and counterfactuals, research only.

**Proof:** `test_parts27_30_flow_end_to_end_in_demo` runs this whole path once and checks every stage.

## 9. DEMO readiness

**What is proven:**
* **Autonomous DEMO in-process:** the integration test, with the simulated broker behind the real bridge and scripted AI providers, runs scan → consensus → Risk Engine → execution → EXECUTED → management → kill switch, staying in DEMO.
* **The installed application** (a sandbox install with the test connector) ran the live roles:
  * every migration preserved its data;
  * startup reconciliation passed with the pending-order comparison;
  * event-driven re-analysis and data quality (score 100) worked;
  * the backlog monitor found and led to fixing a real defect.
* **DEMO Validation page** on that install: 6 PASS, 2 ATTENTION (a net-negative DEMO record of −263.00 over 401 trades; two scan errors during a deliberate restart), 3 PENDING.

**What is not proven, and cannot be from a development environment:**
* operation against **your** broker's real DEMO feed and fills, on your PC, over trading days;
* no computer was attached to this session.

**Acceptance procedure:** `docs/rr-trading-one/DEMO_VALIDATION.md`. Install the latest build, connect your DEMO account, run at least 3 trading days, then read the **DEMO Validation** page (PENDING never counts as a pass).

**Assessment:** the system is **ready for DEMO acceptance**. DEMO acceptance itself is **not yet done**.

## 10. LIVE readiness

**Assessment: not ready for LIVE.** This is not claimed on the basis of development or backtests.

**Built in Phase 10 (and not used):**
* the PC LIVE lock can be authorised or revoked from the window (MT5 Manager → LIVE on this PC…);
* the typed LIVE account number must equal the connected LIVE account, plus the typed phrase;
* the check is enforced by the connector's own, unchanged code;
* each request is used once and expires after 60 s.

**Separate consents LIVE requires, each explicit and administrator-only:**
1. A LIVE account verified against the broker and approved by an administrator (typed `APPROVE <login>`).
2. The readiness checklist passing:
   * identity;
   * LIVE risk settings no looser than DEMO's;
   * emergency controls clear;
   * no unresolved executions;
   * ≥ 20 closed DEMO trades.
3. The typed `ENABLE LIVE TRADING` switch in the console, by a user with `trade.live`.
4. The PC's own LIVE lock, released in the window.
5. Auto trading turned on in LIVE, separately.

**Unresolved risks:**
* **No DEMO acceptance on your PC yet** (§9).
* **DEMO results:** the install's DEMO record is net negative (−263.00 over 401 trades). The checklist counts trades only. Whether LIVE must also require a non-negative or minimum DEMO result is **your decision, still open**. The DEMO Validation page already flags it.
* **Unvalidated new detectors and gates:** the pattern detectors, selection decisions and failure prediction are new and unvalidated. They are advisory by default (`RR_OPPORTUNITY_GATE=advisory`); enabling enforcement is a decision for after DEMO evidence.
* **Unsigned installer:** Windows SmartScreen will warn, and no auto-updater exists (needs your hosting and signing decisions).
* **AI costs:** AI review requires API keys with billing; consensus doubles the calls.

**Remaining approval requirements:**
* your DEMO acceptance;
* your decision on the DEMO-result rule;
* administrator approval of a LIVE account;
* conservative LIVE risk settings;
* the explicit LIVE consents above.

## 11. Operational instructions (Windows, local mode)

The desktop app keeps the stack's secrets protected with Windows DPAPI, so **start, stop and restart go through the RR
Trading One window** (allow-listed actions). Read-only checks use the fixed container names (project `rr-trading-one`)
in PowerShell.

| Task | How |
|---|---|
| Start the application | Open **RR Trading One** → **Start All** (starts PostgreSQL, Redis, API, console, monitor, analysis, learner; migrations run after a safety backup). The trading worker is started separately: **Start Trading Worker**, typed `START TRADING WORKER`. |
| Stop the application | Window → **Stop All** (stops the trading worker first). Or tray icon → Quit after Stop All. |
| Restart workers | Window → **Restart All**, or **Stop / Start Trading Worker**, or **Stop / Start Learner**. Read-only check after: `docker ps --filter "name=rr-trading-one" --format "table {{.Names}}\t{{.Status}}"` |
| Check logs | `docker logs --since 30m rr-trading-one-analysis-1` (also `-monitor-1`, `-learner-1`, `-api-1`, `-trading-worker-1`). In the console: **System Monitor** → error logs per service, events, audit. |
| Check database health | `docker exec rr-trading-one-postgres-1 pg_isready -U rr -d rr_trading` and `docker exec rr-trading-one-postgres-1 psql -U rr -d rr_trading -Atc "select version_num from alembic_version"` (expect `0016`). Console: **System Monitor** → database connections. |
| Check MT5 connectivity | Window → **MT5 Manager** (connector state, account, mode, LIVE on this PC). `Invoke-RestMethod http://127.0.0.1:47810/api/health`. Console: **Command Centre** status strip, **DEMO Validation** → Broker. |
| Verify autonomous DEMO trading | Console → **DEMO Validation** (window 72 h or more): Broker, Scanning, Opportunities, Execution and Protection must be PASS. **Opportunity Lifecycle** shows each setup's trace to EXECUTED; **Trading Worker** shows auto trading (DEMO). |
| Verify emergency controls | Console → **Risk Centre** → kill switch (stops new entries, applies the emergency policy, cancels the engine's pending orders), then reset (needs `system.manage`). Window → MT5 Manager → PC emergency stop. Check: **DEMO Validation** → Protection; **System Monitor** → events. Do this in DEMO only. |
| Perform recovery checks | Window → **Backup and Recovery**: last automatic backup and last restore test (weekly; "passed" expected). **Back up now** at any time. Restoring needs the typed `RESTORE DATABASE`. The recovery manual covers new-PC and lost-PC cases. |

---

**Next step is yours:** run the DEMO acceptance (`DEMO_VALIDATION.md`), and decide the DEMO-result rule. LIVE stays
locked on every installation until you decide otherwise.
