# RR Trading One — Phase 10 Report

**Phase:** 10 — historical data and trade intelligence (specification Part 10)
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 10 asks to **reuse** the Part 25/26 learning engine and its tables, with no duplicate trade, experiment,
execution or model tables. Phase 10 therefore began with an audit of what the existing modules already compute against
the 23 required items, and then filled the gaps **without adding a single table or column**. The schema stays at 0007.

| Part 10 item | Before this phase | Now |
|---|---|---|
| Wins / losses / break-even | ✅ `trade_features.outcome` | + counts and break-even rate in every metric block |
| Partial exits | ✅ count per position (from the deal ledger) | + aggregated; "known for N" shown, because unknown is not counted as 1 |
| **Stop-loss / take-profit / manual exits** | Partly: SL/TP/stop-out only; manual and program closes all stored as "CLOSED" | **Exit type from the broker's own deal reason** (`mt5_deals.reason`): STOP_LOSS, TAKE_PROFIT, STOP_OUT, MANUAL (terminal/mobile/web), PROGRAM (API/EA), or UNKNOWN when nothing is recorded |
| Position duration | Per trade only | + median/mean and a duration dimension |
| **Entry quality / exit quality** | Not computed | Entry quality = 1 − min(MAE R, 1). Exit efficiency = realised R / MFE R, clipped to [−1, 1], and undefined below 0.1 R MFE. Both are reported with their samples. |
| MAE / MFE, realised R, profit factor, drawdown | ✅ | Unchanged |
| Spread, slippage | Per trade only | + averages with samples; trades without a measurement are left out, not filled in |
| Commission, swap | Inside net profit only | + separate totals |
| Regime, session, symbol, timeframe, strategy | ✅ dimensions | Unchanged |
| **Strategy attribution** | Strategy codes when linked | + an attribution dimension: ENGINE_SIGNAL, ENGINE_MANUAL or **UNLINKED** (no engine order linked: never assigned to a strategy) |
| DEMO / LIVE separated | ✅ per mode | Unchanged; verified |
| **Sources not mixed** | No single view | **Sources view:** closed trades per mode, and backtest, out-of-sample, walk-forward and paper records, each separate and never summed |
| Coverage | Not visible | How many trades actually have R, MAE/MFE, slippage, spread, timeframe, regime and session |

**Also**
* **AI trade research** goes through the Phase 9 gateway (task `trade_research`) over one mode's aggregated statistics only. It is stored as an OPEN recommendation in the existing `ai_recommendations` table, and changes nothing.
* **Measured peer correlation** (the Phase 8 limitation): computed from the peer bars each analysis already fetches, reported with its sample, or with the reason it is undefined.
* **A Trade Intelligence console page.**

| Result | Value |
|---|---|
| Phase 10 end-to-end: a real DEMO trade through the engine, the fake broker and the learner | **17 / 17** (third run; see section 3) |
| Backend suite, including the 215-strategy preservation test | **250 passed** (244 + 6 new) in one full run |
| Frontend `next build` (strict TS) | Passed, **28 pages** (new: Trade Intelligence) |

## 2. What was built (all additive)

**`backend/app/learning_engine/performance.py`** (the original Part 25 module, extended)
* `exit_types()`: the reason of each position's last closing deal, from the existing `mt5_deals` ledger. Without a ledger deal, the collector's own close reason is used, and only when it is specific.
* `attribution()`, `entry_quality()`, `exit_efficiency()`, `duration_bucket()`.
* `load_trades()` adds these per-trade facts, `None` wherever nothing was recorded.
* `compute_metrics()` keeps every original key and adds `trade_quality()`:
  * outcomes, exit types, partial exits, holding time;
  * commission and swap totals;
  * slippage, spread, entry quality and exit efficiency, each with its sample;
  * attribution and coverage.
* Three new dimensions: `exit_type`, `attribution`, `duration`.

**`backend/app/api/routes/learning.py`**
* `/api/learning/trades` adds the per-trade facts.
* New `GET /api/learning/sources`.
* New `POST /api/learning/trade-research` (requires `strategy.manage`; audited; recorded as a Post-Trade Learning agent run).

**`backend/app/scanner/service.py` / `api/routes/intel.py`**
* `peer_correlation()`: the correlation of returns over the last 200 common closed bars, or `None` with a reason: fewer than 30 common bars, or no price variation.
* Each symbol's latest measurement is shown in `GET /api/symbols`.

**Console**
* **Trade Intelligence** (DEMO/LIVE toggle):
  * the verdict, which is never given below 30 trades;
  * win/loss/break-even, profit factor with its confidence interval, expectancy with R sample, drawdown and costs;
  * exits by broker reason, quality with samples, attribution and coverage;
  * any dimension;
  * the evidence sources (kept apart);
  * AI trade research;
  * the trade list.
* **Broker Symbols** gains a "Measured ρ" column.

**Test fixture:** the fake broker's deals now carry DEAL_REASON_EXPERT (3), which is what MT5 records for API orders.
The original collector still maps it to "CLOSED", so no existing test changed meaning.

## 3. End-to-end verification (the upgraded install from Phase 9, no migration)

**Start:** Start All succeeded; the schema stayed at 0007; no closed trades.

**A real DEMO trade through the engine against the fake broker**
* A BUY of 0.1 lot EURUSD.r (SL 1.0980, TP 1.1050) was confirmed.
* A 0.05 partial close and a final close were confirmed.
* Set-up note: the sandbox has no news feed, so the DEMO risk policy "block when news risk is unknown" was turned off through the risk API (audited), as a user could do.

**Learner:** a triggered trade sync rebuilt the closed position from the broker's deals: 1 entry and 2 exits, all with
reason EXPERT (3). The position was then enriched.

**Intelligence**
* Exit type **PROGRAM** (closed through the API); **2 partial exits**.
* Commission **−1.40** (2 exits × −0.70, from the broker).
* Attribution **ENGINE_MANUAL**, with the strategy dimension **empty**: nothing attributed by guesswork.
* Verdict **INSUFFICIENT_DATA**.
* Coverage stated: R 1/1, MAE/MFE 0/1, with the reason recorded: "no M1 bars covering trade (history depth)".

**Sources:** DEMO 1 trade, LIVE 0.

**AI trade research (fake provider)**
* Stored as an OPEN recommendation.
* The answer states the sample is too small.
* The provider received aggregates only: no ticket, no account login.

**Peer correlation:** after a re-analysis of EURUSD.r H1 (peer XAUUSD.r), the measurement on the fake broker's flat
prices is `rho: None`, "undefined: no price variation in the window", over 200 bars. That is correctly not a number.

**Console:** the Trade Intelligence page showed the trade, its exit, its attribution, the sources and the research;
Broker Symbols showed the measurement; no console errors.

**How the runs went:** the first two runs failed because **the Risk Engine correctly refused my test orders**:
"Take profit required", then "R:R 0.96 < 1.0". After I corrected the test order, the third run passed 17 / 17.

## 4. Findings

| # | Finding | Outcome |
|---|---|---|
| 1 | Manual and program closes were indistinguishable ("CLOSED"), although the broker records who closed each position. | The exit type comes from the deal reason. |
| 2 | Raw exit efficiency (R / MFE) lets one losing trade dominate an average: −1 R after +0.4 R scores −2.5. Found by my own test. | Clipped to [−1, 1], with the definition stated on the page and in the code. |
| 3 | A trade with no linked engine order cannot honestly be called "external": its order may simply not have matched. | Reported as UNLINKED, never as a strategy. |
| 4 | On flat prices, correlation is undefined (zero variance), not "too few bars". | The measurement carries its reason. |
| 5 | Unknown partial-exit counts default to 1 in the original enrichment. | Not changed (original behaviour). The intelligence counts partial exits from the position's own record and reports "known for N". |

## 5. Files

**Created**
* `backend/tests/test_trade_intelligence.py` (6)
* `frontend/app/(console)/trade-intelligence/page.tsx`
* this report

**Modified**
* `backend/app/learning_engine/performance.py`
* `backend/app/api/routes/learning.py`
* `backend/app/api/routes/intel.py`
* `backend/app/scanner/service.py`
* `backend/app/ai/gateway.py`: `trade_research` is now used by the engine
* `backend/tests/test_ai_gateway.py`: that route is now "used"
* `mt5-connector/tests/fake_mt5.py`: deal reason 3
* `frontend/app/(console)/symbols/page.tsx`
* `frontend/components/Shell.tsx`

**No migration; no new table.** All 215 strategies are preserved, and every original test passes.

## 6. Known limitations

* **Real trading history has not been exercised.** One fake trade proves the pipeline, not any statistics. Every verdict needs ≥ 30 trades and says so.
* **MAE/MFE need bars covering the trade** (1-minute bars for short trades). The simulated feed only has hourly bars, so MAE/MFE, and therefore entry quality and exit efficiency, were "no data" in the end-to-end run. With a real terminal they depend on its history depth, and the reason is recorded per trade.
* **Engine-initiated closes look like PROGRAM to the broker.** A close through the console and a close by the trade manager are both PROGRAM. Telling them apart would need the engine's own close orders joined in; that is planned for Phase 14 (execution).
* **Peer correlation** is measured per analysis on that analysis's timeframe; there is no longer-horizon study.

## 7. Security implications

* **Read-only analytics.** Nothing here can trade or change settings.
* **Trade research** sends only one mode's aggregated statistics (no tickets, accounts or other mode's figures) through the gateway's redaction, validation and ledger. It needs `strategy.manage`, is audited, and only creates a recommendation.

## 8. Rollback

Revert the Phase 10 commits. Nothing in the database changes, because no schema change was made.

## 9. Next phase

**Phase 11 — AI learning and model validation**
* Review the existing model training, validation and promotion (Part 25/26 `model_registry`, governance with approvals).
* HMAC-sign model artefacts (Phase 1 gap 7.8).
* Move the learner's weekly research onto the gateway (Phase 9 finding 5).
* Validate on out-of-sample data only, with every claim backed by its sample.
