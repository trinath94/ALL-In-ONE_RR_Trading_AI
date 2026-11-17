# RR Trading One — Parts 27–30, Phase 4 Report (Part 27: autonomous research and AI orchestration)

**Date:** 2026-11-17 · **Branch:** `claude/focused-cori-b8em6u` · **Code commit:** `0a18c6e`
**Plan:** `docs/PARTS27_30_AUDIT.md` (Phase 4 rows)

## 1. Summary

Phase 4 extends the existing engine. Nothing was rebuilt or replaced:
* no second scanner, gateway, scheduler or execution path;
* the 215 strategies, the Risk Engine and MT5 execution are untouched.

| Plan item (class) | Delivered |
|---|---|
| Decision trace per opportunity (B) | One stage-by-stage trace per opportunity (data → regime → strategies → timeframe → news → calendar → signal engine → adaptive → consensus → probability → anomalies → fusion → entry validation → exit plan → failure risk → decision; then AI review, Risk Engine, execution as they happen) |
| Opportunity lifecycle and score breakdown (E/B) | States RESEARCHING, MONITORING, WAITING_FOR_CONFIRMATION, QUALIFIED, REJECTED, NO_TRADE, EXPIRED, EXECUTED, with a closed transition table and a transition log. The breakdown lists only existing components; no new composite score is invented. |
| OpenAI preset (B) | `openai` preset (`https://api.openai.com/v1`). The adapter retries once with `max_completion_tokens` (and no temperature) when a model rejects `max_tokens`/`temperature`. |
| Dual-provider consensus (E) | Route mode `consensus` for candidate review: the first two different providers answer independently; see §3 |
| New agents (E) | `fundamental_analysis`, `entry_validation`, `exit_optimisation`, `failure_prediction`, `ai_consensus`, added to the **same** registry (21 → 26). The pattern agent is Phase 5. |
| Source registry (B) | `data_sources` with observed health (MT5 market data, economic calendar); AI providers' health merged from the existing usage ledger, not duplicated |
| Regime labels (B) | Tags VOLATILITY_EXPANSION / VOLATILITY_CONTRACTION, ACCUMULATION / DISTRIBUTION, UNCERTAIN, NEWS_DRIVEN. The primary label is unchanged, and no strategy declares these tags, so suitability and scoring are as before (tested). |
| Event-driven re-analysis (E) | High-impact news release, anomaly (flash move, volatility spike, gap, jump), price moving into a waiting entry |

## 2. What can change a trading decision

Only to be more cautious, never to upgrade:
* **Always:** a BUY/SELL whose stop or target is on the wrong side of the entry (or that has no levels) becomes NO_TRADE. The opportunity becomes REJECTED.
  * The engine's WAIT without levels is not a defect and stays WAIT. The tests found that my first version turned these into NO_TRADE; fixed before commit.
* **Only with `RR_OPPORTUNITY_GATE=enforce`** (default `advisory`): a chased entry, a spread above a fraction of the stop distance, or HIGH failure risk downgrades BUY/SELL to WAIT. This follows the convention of the existing `RR_CONSENSUS_MODE` and `RR_FUSION_MODE`.
* **AI consensus**, when the administrator selects it for candidate review.
* **Failure prediction** is a named count of risk factors with their evidence, labelled "not a probability". It never says LOW without at least 30 out-of-sample trades.
* **Fundamental view:** without a healthy calendar it says "unavailable", never "no news". Actual vs forecast is reported; its market effect is not inferred.

## 3. Consensus mode

* Each answer is schema-validated and boundary-checked on its own (the AI may only confirm, downgrade or reject).
* **Confirm** only when both confirm the engine's direction. **Reject** if either rejects (a reversal is already a rejection under the boundaries). Otherwise **wait**.
* If either provider gives no valid answer, the candidate stays **UNREVIEWED** and is never auto-executed on one opinion.
* The API refuses consensus for other tasks, and refuses it without two different providers first in the chain.
* Each consensus review is recorded as an `ai_consensus` agent run with both votes.
* API access is billed separately: a Claude.ai or ChatGPT subscription does not include API credits (stated in the preset notes and the manual).

## 4. Event-driven re-analysis: guards

* At most one request per symbol per `RR_REANALYSIS_THROTTLE_SECONDS` (300). Each news event and anomaly fires once.
* News and anomalies re-analyse only timeframes whose latest analysis **predates** the event.
  * The first version re-analysed bars whose own analysis had just recorded the anomaly. The existing test "a cycle with nothing due records nothing" caught it; fixed.
* Price-at-entry fires only when the price **moved into** the zone since the analysis.
* A re-analysis of an already-analysed bar **supersedes** that bar's still-NEW signal (marked EXPIRED, with the reason). There is never more than one NEW signal per symbol, timeframe and bar.
* If the bar's signal was already **executed**, no second signal is created and the opportunity is REJECTED with that reason.
* The scanner's per-job in-flight lock still prevents concurrent analyses.

## 5. Database (migration 0012, additive)

* **New tables:**
  * `opportunities` (FKs: symbols CASCADE; analysis and signal SET NULL; four indexes);
  * `opportunity_transitions` (CASCADE from opportunities);
  * `data_sources`.
* **New column:** `ai_task_routes.mode`, NOT NULL, server default `fallback`, i.e. the original behaviour.
* **Retention:** finished opportunities older than `RR_OPPORTUNITY_RETENTION_DAYS` (90) are pruned hourly, alongside the agent run log.
* **Rollback:** `alembic downgrade 0011` drops only these.

## 6. Tests and results

| Suite | Result |
|---|---|
| Engine, full (alone, local) | **343 passed** (baseline 323 + 20 new), 0 failed, 0 skipped |
| `tests/test_part27.py` | 20 passed, none skipped |
| CI on `0a18c6e` | backend 44033884588 ✅, frontend 44033884734 ✅, desktop 44033884576 ✅ (Linux/Windows Rust tests, Windows installer build/install/upgrade/uninstall) |

**New tests:** cover entry-validation outcomes, exit-plan R multiples from the Risk Engine's parameters, failure-risk factors, fundamental "unavailable", regime tags, lifecycle transitions, plus:
* **Scanning:** one opportunity per analysis with a setup; the full trace; the five agents' runs; advisory mode keeps every engine decision; the APIs (including auth 401 and validation 422); the source registry.
* **AI and gates:** AI review → QUALIFIED; sync → EXECUTED with the Risk Engine and execution stages; enforce gate only downgrades.
* **Event triggers:** throttling; superseding; the already-executed case; a news release firing once.
* **Providers:** the OpenAI preset and parameter fallback (mocked HTTP); consensus (agree, reject, wait, reversal, missing answer); route validation.

**How the tests produce signals.** Random-walk test bars almost never make the real engine emit BUY/SELL. Four tests therefore wrap the real `build_signal` so that analyses with setups become a BUY with ATR levels. Everything after the signal engine is the real code.

**Existing tests changed** (stated, not hidden): `test_orchestration.py` pinned the registry at 21 agents. It now checks that the 21 originals are unchanged and first, in order, plus exactly the five new ones; the LLM-agent set gains only `ai_consensus`. `conftest.py` cleans the three new tables.

## 7. End-to-end on the installed application (sandbox install from earlier phases)

1. **Upgrade with data:** the database was at **0011** with 68 analyses, 25 signals and 215 strategies. Through `supervise restart_all` with the new image it is now at **0012**. All rows are kept; strategy sync reported `strategies_new: 0`. The five existing AI routes read `mode = fallback`.
2. **Through the gateway** (127.0.0.1:47810, owner login):
   * `/api/agents` → 26 (the five new ones last);
   * `/api/ai/presets` includes `openai`;
   * `/api/ai/routes` shows modes (consensus supported for candidate review only);
   * `/api/opportunities/summary` → gate `advisory`;
   * `/api/data-sources` → MT5 `ok`, calendar `not_configured`.
3. **Event-driven re-analysis on the live roles:** a request queued in the stack's Redis made the analysis role re-analyse symbol 2's already-analysed H1 bar (06:00). The other symbol stayed not due, and the request was consumed.
4. **Source registry honesty:** while I restarted the stack, one fetch hit "RR MT5 Connector agent 1 is offline". The registry showed ERROR with that message. After the next fetch it showed OK, and the error stays counted.
5. **Console:** the new Opportunity Lifecycle page and AI Studio's mode column render from the rebuilt console image.
6. **Not shown here:** the fake connector's bars produced no strategy setups in these analyses, so the install shows **0 opportunities**. That is correct (no setup, no opportunity), so creation was verified in the test suite, not on this install.

## 8. Limitations

* With no calendar provider configured, the fundamental agent and news triggers have nothing to work on (they say so).
* Consensus doubles AI cost per shortlisted candidate.
* The opportunity gate is advisory by default; enforcing it is an administrator decision.
* Pattern intelligence (harmonic, Elliott, detection records) is Phase 5.

## 9. Security

* **New read endpoints** use the existing session authentication: `GET /api/opportunities`, `/api/opportunities/summary`, `/api/opportunities/{id}`, `/api/data-sources`.
* **The route mode** is changed through the existing `PUT /api/ai/routes/{task}` (`system.manage`, audited, includes the mode).
* **AI payloads** go through the existing redaction. No AI output can size, submit, enable LIVE or change limits.
* **Initial state unchanged:** DEMO, auto trading off, LIVE locked.

## 10. Files

* **Created:**
  * engine: `backend/app/db/models_opportunity.py`, `backend/app/intelligence/opportunity.py`, `backend/app/scanner/triggers.py`, `backend/app/services/sources.py`;
  * migration and tests: `backend/migrations/versions/0012_opportunities_sources.py`, `backend/tests/test_part27.py`;
  * console: `frontend/app/(console)/opportunities/page.tsx`;
  * this report.
* **Modified:**
  * engine: `ai/gateway.py`, `ai/providers.py`, `analysis/regime.py`, `api/routes/ai.py`, `api/routes/intel.py`, `core/config.py`, `db/models.py`, `db/models_ai.py`, `news/service.py`, `orchestration/agents.py`, `scanner/service.py`;
  * tests: `tests/conftest.py`, `tests/test_orchestration.py`;
  * console: `components/Shell.tsx`, the AI Studio and Agents pages;
  * manuals: user, administrator, README, release notes.

## 11. Rollback

Revert the commits and run `alembic downgrade 0011`.

## 12. Next

**Phase 5 (Part 28):** pattern intelligence. Harmonic and Elliott detection, pattern detection records, the pattern agent in the same registry, and a Pattern Intelligence view.
