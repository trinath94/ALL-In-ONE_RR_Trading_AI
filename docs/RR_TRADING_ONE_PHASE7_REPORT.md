# RR Trading One — Phase 7 Report

**Phase:** 7 — original strategy library integration
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

The console's existing Strategy Library now shows, for each of the 215 original strategies, what the engine actually
knows about it. Everything is read from the registry, the scanner's own selection logic, the frozen baseline inventory
and stored records. Nothing is estimated.

| Fact shown | Where it comes from |
|---|---|
| Metadata: category, scoring component, timeframes, regimes, minimum bars, parameters, code hash, version | The registry (strategy classes); unchanged |
| **Data status**: can the engine supply what the strategy needs? | The strategy's own `requires` and what the scanner actually passes to strategies |
| **Scanned in DEMO / LIVE** | The scanner's own `enabled_strategies()` for each mode |
| **Baseline integrity**: identity, parameters and source identical to the frozen inventory? | `tests/baseline/strategy_inventory.json`, shipped in the backend image |
| **Activity**: signals and errors in the last 7 days | Stored analysis results |
| **Performance** | Recorded trades only, **kept apart by source** (out-of-sample, walk-forward, paper, live). "No recorded trades" when there are none. |

**One safety defect was found and fixed: a risky strategy enabled for DEMO was also scanned in LIVE** (section 5).

| Result | Value |
|---|---|
| Phase 7 end-to-end from a **fresh install** (real stack, real headless connector with FakeMT5) | **15 / 15 passed** |
| Backend suite, including the 215-strategy preservation test | **210 passed** (203 + 7 new) |
| MT5 connector / Windows connector | 17 / 36 passed |
| Frontend `next build` (strict TS) | Passed, 25 pages |
| CI, Phase 7 WIP commits | backend 43459001130 and frontend 43459289662 passed |

## 2. What the library reports (verified against the running engine)

| | Count | Meaning |
|---|---|---|
| Registered / baseline | **215 / 215** | Every category at its baseline count |
| Identical to the frozen baseline | **215 / 215** | Checked inside the container, not only in tests |
| Runnable on standard MT5 data | **195** | OHLC bars plus tick volume |
| Data not supplied by the engine | **11** | 5 need peer-symbol data: MT5 has it, but the scanner does not pass it yet (Phase 8). 6 need a news feed, a trained model, an AI agent or a portfolio allocator, which are not passed to strategies. |
| MetaTrader 5 does not provide the data | **9** | Order flow ×4, market depth, open interest, options IV, HFT ×2 |
| Risky (grid / martingale / anti-martingale / DCA) | **4** | Disabled by default; now never scanned in LIVE |
| Scanned in DEMO (fresh install) | **196**, of which **191 can produce signals** | The 5 peer-data strategies are enabled by the original seed but cannot fire until Phase 8. The page states this instead of implying 196. |
| Declared timeframes | M1 2 · M5 33 · M15 204 · M30 7 · H1 176 · H4 176 · D1 6 · W1 0 | Shown as declared, with the note that the scanner does not filter by timeframe yet (gap 7.1, Phase 8) |

These counts match the Phase 1 audit exactly; the unit tests assert them, so a change to any strategy's requirements
would be caught.

## 3. What was built

### 3.1 Backend (additive)

**`backend/app/services/strategy_library.py` (new, read-only)**
* `data_status(requires)`: maps each requirement to the honest status and a plain explanation.
* `baseline_status()`: compares the same fields as the preservation test against the shipped inventory. If the inventory is missing, it says "baseline inventory not available" rather than claiming a result.
* `effective(db)`: the scanner's own selection for DEMO and LIVE.
* `activity(db)`: signals and errors per strategy from stored analysis results.
* `summary(db)`: the totals above.

**`GET /api/strategies`**: all original keys are unchanged. It adds `data_status`, `data_notes`, `min_bars`,
`scanned {DEMO, LIVE}`, `baseline`, `activity` and `performance_by_source`.

**`GET /api/strategies/summary`** (new, any logged-in user): totals, categories, baseline integrity, data status,
risky list, scan counts, timeframe coverage (`timeframes_enforced: false` with a note), and activity.

**`RR_STRATEGY_BASELINE_PATH`**: optional; it defaults to the inventory in the backend tree.

### 3.2 Scanner fix (`backend/app/scanner/service.py`)

`enabled_strategies(db, "LIVE")` now always skips risky strategies. The original DEMO behaviour and the original
LIVE behaviour for all other strategies are unchanged; a test asserts LIVE equals DEMO minus the risky four.

### 3.3 Console (`frontend/app/(console)/library/page.tsx`, the existing page extended)

**Summary**
* Stat tiles: strategies, baseline integrity, runnable count, scanned in DEMO and LIVE with how many can produce signals, recent signals.
* Clickable "data the engine can supply" and "declared timeframes" panels, which act as filters.

**Table:** data status, timeframes, DEMO / LIVE scan state, risk, 7-day signals and errors, and baseline status.
Filters: search, category, scanned-in-mode, risky, recent signals or errors, and differs from baseline.

**Detail panel**
* Every metadata field, with plain data notes.
* A risk explanation and baseline status.
* Performance per source, or "No recorded trades. No statistics are shown until there are some."
* The Enable button is disabled for risky strategies in LIVE.
* For strategies that cannot run, a warning that enabling them has no effect.

All original controls are kept: enable/disable per mode, parameters, version history and experiments.

## 4. End-to-end verification (fresh install)

**Setup:** fresh install, then the Phase 5 setup and the forex symbol enabled.

**Library facts through the gateway**
* 215 strategies, with every category at its baseline count.
* **215 / 215 identical to the baseline, checked inside the container.**
* Data status: 195 runnable, 11 not supplied, 9 unsupported.
* Timeframes reported as declared and not enforced.
* No performance figures (no trades recorded).
* The DEMO scan count equals the engine's enabled production strategies (196).

**Risky strategies stay out of LIVE**
* Enabling `quant_martingale` for LIVE was refused with 409.
* After enabling it for DEMO and saving LIVE parameters, the library reported DEMO yes, LIVE no.
* **The analysis service's own selection, run inside the container, confirmed it is not in the LIVE set.**

**Activity:** the analysis service scanned EURUSD.r from the fake broker (3 analyses, 2 signals). The library's
totals **equalled a direct SQL count** of the stored records.

**Console:** logged in as the administrator, the page showed:
* the integrity tile, the timeframe note and the data panel;
* the DEMO tile reading "196, enabled; 191 can produce signals", the same 191 the API reports;
* Martingale's detail with the risky note and "No recorded trades";
* no console errors.

## 5. Findings

| # | Finding | Outcome |
|---|---|---|
| 1 | **Safety: a risky strategy enabled for DEMO was also scanned in LIVE.** Enabling for DEMO sets the shared `strategies.enabled` flag, and LIVE scanning used it whenever no LIVE configuration row existed. Saving LIVE parameters also created an *enabled* LIVE row. The 409 on "enable for LIVE" did not cover either path. | Fixed in the scanner selection; a regression test covers both paths, and it was confirmed inside the running analysis service. LIVE trading itself stays locked; this closes the gap before LIVE is ever unlocked. |
| 2 | The original seed marks the 5 peer-data strategies "available", because its capability set includes peer data, but the scanner never supplies it. They are enabled yet can never fire. | Not changed in the seed, to avoid altering original state. The library reports them as "data not supplied" and counts them separately ("191 can produce signals"). Fixing the supply belongs to Phase 8. |
| 3 | The original "performance" figure sums out-of-sample and walk-forward results without saying so. | The original key is kept for compatibility; the console now shows results per source. |
| 4 | Pre-existing UI bug: the library's search and category filters never appeared, because `Panel` renders its header only when it has a title. | The table panel now has a title and the filters sit in the panel body. The shared component is unchanged. |
| 5 | My page check first waited on text that also appears in the subtitle (a case-insensitive match), so it read the page before the data loaded. | The check now waits for both data requests to render. Final run 15 / 15. |

## 6. Files

**Created**
* `backend/app/services/strategy_library.py`
* `backend/tests/test_strategy_library.py` (7 tests)
* this report

**Modified**
* `backend/app/api/routes/intel.py`: additive keys and the summary route
* `backend/app/scanner/service.py`: risky strategies never in the LIVE selection
* `backend/app/core/config.py`: `strategy_baseline_path`
* `frontend/app/(console)/library/page.tsx`
* `docs/RR_TRADING_ONE_PHASE6_REPORT.md`: CI results recorded

**No migration.** No strategy class, registry entry or baseline file was touched; the preservation tests pass.

## 7. Not verified here / known limitations

* **Activity counts signals and errors only.** The scanner does not store "no signal" or "unavailable" outcomes, so per-strategy evaluation counts and per-symbol unavailability cannot be shown yet. Storing them is a scanner change for Phase 8.
* **Real-volume availability per symbol** is detected at scan time but not recorded, so the "per symbol" status cannot yet name the symbols. No original strategy needs real volume today (count 0).
* Timeframe compatibility is **displayed, not enforced** (Phase 8).
* No real Windows or MetaTrader 5 (unchanged). FakeMT5 offers two symbols.

## 8. Security implications

* The new routes are read-only and require a logged-in user. Changes still go through the original `PATCH /api/strategies/{code}`, which requires `strategy.manage` and is audited (a test confirms read-only users are refused).
* The risky-strategy fix narrows what the LIVE scanner may run, and nothing widens it. AI still cannot enable LIVE, and LIVE remains locked during development.

## 9. Rollback

Revert the Phase 7 commits. There is no schema change.

Reverting `scanner/service.py` alone would reopen finding 1, so if part of this phase must be reverted, keep that
change.

## 10. Next phase

**Phase 8 — scanner scheduling and strategy compatibility**
* Enforce declared timeframes, reporting "not applicable on this timeframe" rather than "no signal" (gap 7.1).
* Supply peer-symbol data, so the 5 peer strategies can run (gap 7.2).
* Record per-symbol data capabilities and evaluation outcomes.
* A priority queue with budgets, backpressure and timeouts across many enabled symbols (gap 7.3).
