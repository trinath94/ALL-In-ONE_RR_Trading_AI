# RR Trading One — Parts 27–30, Phase 6 Report (Part 29: strategy selection, discovery, learning, automation)

**Date:** 2026-11-17 · **Branch:** `claude/focused-cori-b8em6u` · **Code commit:** `a5142d9`
**Plan:** `docs/PARTS27_30_AUDIT.md` (Part 29 rows)

## 1. Summary

| Req. | Class | Delivered |
|---|---|---|
| 29.2 Strategy selection | B | A **selection decision** per analysis (SELECT / COMBINATION / WAIT / REJECT / NO_TRADE) with each supporting strategy's out-of-sample evidence: trades, win rate, profit factor, expectancy, regime suitability, category. It also states whether the evidence is "proven" (≥ 30 trades) or "unproven". Recorded in the decision trace. |
| 29.4 Discovery | B | Systematic **parameter variants** of original strategies (one numeric parameter at a time, ×0.75 and ×1.25), recorded as DISCOVERED experiments. Only parameters change: no new code runs, and the originals are untouched (215, tested). |
| 29.5 Strategy DNA | B | `GET /api/strategies/{code}/dna`: definition, versions, fingerprints, configurations, evidence by source, variants, pattern family. Shown in the Strategy Library. |
| 29.6 Bias report | B | Per variant: look-ahead/repaint (trades before a cut must be identical when later bars do not exist), leakage (no out-of-sample entry before the cut), survivorship (symbols removed by the broker), multiple testing (variants tried and the required significance level) |
| 29.9 Model/provider health → NO_TRADE | B | A health stage in the trace: production models with drift events in the last 24 h; AI providers cooling down |
| 29.10 LIVE readiness checklist | B | Required before the typed LIVE switch; LIVE Readiness page (§3) |
| 29.11 Exits | E | Time, volatility, structure and invalidation exits, all **off by default** (§4) |
| 29.12 Strategy concentration | E | `max_positions_per_strategy` in the Risk Engine, **off by default** |

**What changes trading behaviour by default:** only the LIVE switch, which is now **stricter** (§3).
* Everything else is recorded, or off until an administrator sets it.
* The `RR_OPPORTUNITY_GATE=enforce` mode from Phase 4 also downgrades BUY/SELL to WAIT on a selection REJECT or an unhealthy production model.
* An unhealthy AI provider already left candidates unreviewed (never auto-executed).

## 2. Variant evaluation

* The learner's existing experiments job evaluates up to two waiting variants per run on 3,000 H1 broker bars.
  * It runs the base and the variant, in-sample and out-of-sample, using the existing backtester, plus the bias report.
  * Results are stored in the experiment's evidence.
* The job's existing branch for code-based experiments is unchanged.
* Promotion follows the experiment lifecycle and administrator approval.

## 3. LIVE readiness

**Required items:**
* a verified, administrator-approved, active LIVE account;
* a LIVE risk configuration that is valid and **no looser than DEMO's** (risk per trade, daily loss, drawdown, lot size);
* kill switch and risk lock clear, and a valid emergency policy;
* no unresolved executions;
* at least `RR_LIVE_MIN_DEMO_TRADES` (20) closed DEMO trades.

**Informational items:** connection health, the PC connector's own LIVE lock (RR Trading One still cannot release it; Phase 10), and the AI review provider.

**How it behaves:**
* The checklist can only refuse the switch; it never enables LIVE.
* `GET /api/live/readiness` needs `trade.control`.

**Existing test changed (deliberately, per 29.10):** `test_live_separation.py::test_live_account_flow` switched to LIVE without any DEMO history. It now first asserts that the switch is **refused** with "closed DEMO trades" in the reason, then records 20 closed DEMO trades and continues exactly as before.

**Finding on the installed app: needs your decision.** The install's DEMO history is **401 closed trades with a net result of −263.00**. DEMO acceptance currently checks the count only, and says "review the DEMO results before approving". If DEMO acceptance should also require a non-negative (or minimum) DEMO result, that is a policy you should set; I have not decided it for you.

## 4. Optional exits (Risk Centre, per mode)

| Parameter | Exit |
|---|---|
| `max_holding_minutes` | held at least this long |
| `volatility_exit_atr_ratio` | ATR now ≥ ratio × the ATR its signal's analysis measured (must be > 1) |
| `structure_exit` | a change of character against the position after it opened, on the signal's timeframe |
| `invalidation_exit` | the engine's latest analysis on that timeframe, made after the position opened, is the opposite direction or NO_TRADE |

* Exits are CLOSE commands through the existing journalled, idempotent `execution.manager.manage`, and are notified.
* **Missing data never causes an exit.**
* With all exits off, position management makes exactly the same broker calls as before; the extra bars are fetched only when an exit is enabled.

## 5. Tests and results

| Suite | Result |
|---|---|
| Engine, full (alone, local) | **378 passed** (354 + 24), 0 failed |
| `tests/test_part29.py` + `tests/test_live_separation.py` | 27 passed |
| CI on `a5142d9` | backend 44053582160 ✅, frontend 44053582126 ✅, desktop 44053582100 ✅ |

New tests cover:
* **Risk Engine:**
  * the new parameters default off and are validated;
  * each exit, triggering and not triggering, including "unknown data: no exit";
  * concentration end to end: a second order from the same strategy is refused by the Risk Engine through the real bridge;
  * the time exit end to end: not taken while off, and the position is closed through the journal once `max_holding_minutes` is set.
* **Selection and health:** all five selection decisions; health with a drifting production model; the scan trace's selection and health stages.
* **Variants and bias report:**
  * one-parameter variants; generation without duplicates and without touching the strategy (still 215);
  * evaluation with a passing bias report;
  * the bias report flagging repainting and leakage. Those two checks are exercised with a simulated faulty backtester and split; I could not construct a real repainting strategy, because strategies only ever see bars up to the current one.
* **API and readiness:**
  * DNA and variants API: permissions, 404s, audit;
  * readiness: refusal without a DEMO track record, then ready; a LIVE risk configuration looser than DEMO's is refused; the endpoint's permission.

## 6. End-to-end on the installed application

* **Deployment:** new images through `supervise restart_all`. Still DEMO, auto trading off, 215 strategies, migration 0013 (this phase has no migration).
* **Readiness through the gateway:** not ready, failing `identity` ("no active LIVE account").
  * The LIVE risk configuration passes (0.25 % per trade, 1 % daily loss, 10 % drawdown).
  * DEMO acceptance passes on count (401 trades, net −263.00: see §3).
  * Connection: connected; AI review: configured.
* **DNA and risk defaults:**
  * `trend_ema_9_21` DNA: family trend, 1 version, 1 fingerprint;
  * the DEMO risk configuration shows the five new parameters at 0 / off.
* **Not done on the install:**
  * **The LIVE switch.** The environment's safety check refused the command that would have attempted it, and I did not retry it. The refusal is covered by the automated tests.
  * **Variant generation.** It writes experiments, and it is covered by tests.

## 7. Limitations

* Selection evidence comes from the existing performance records (out-of-sample, walk-forward, paper, live). Most strategies have little, so most decisions say "unproven".
* The volatility exit needs the ATR from the position's originating signal; positions opened manually have none, so that exit does not apply to them.
* The repaint check guards the backtester and data path. It cannot detect a strategy that reads data from outside the bars it is given.

## 8. Security

* **New endpoints:**
  * `POST /api/strategies/{code}/variants`: `strategy.manage`, audited;
  * `GET /api/strategies/{code}/dna`: session;
  * `GET /api/live/readiness`: `trade.control`.
* **Risk parameters** use the existing `PUT /api/risk/config/{mode}` (`risk.manage`, validated, audited).
* **No risk limit was loosened.** New limits default off. LIVE is stricter.

## 9. Files

* **Created:**
  * engine: `backend/app/intelligence/selection.py`, `backend/app/strategies/experimental/variants.py`, `backend/app/services/readiness.py`;
  * tests: `backend/tests/test_part29.py`;
  * console: `frontend/app/(console)/readiness/page.tsx`;
  * this report.
* **Modified:**
  * engine: `risk/engine.py`, `services/trading.py`, `workers/main.py`, `scanner/service.py`, `learning_engine/scheduler.py`, `api/routes/intel.py`, `api/routes/trading.py`, `core/config.py`;
  * tests: `tests/test_live_separation.py`;
  * console: the Strategy Library, Risk Centre and Shell;
  * manuals: administrator, user, release notes.

## 10. Rollback

Revert the commits. No migration to undo; the new risk parameters stored in a configuration are ignored by older code (`from_dict` skips unknown keys).

## 11. Next

**Phase 7 (Part 30):** reliability, data governance, Risk Governor, simulation and counterfactuals, model governance, monitoring, disaster recovery.
