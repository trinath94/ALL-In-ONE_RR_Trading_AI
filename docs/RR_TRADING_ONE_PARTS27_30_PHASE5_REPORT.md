# RR Trading One — Parts 27–30, Phase 5 Report (Part 28: chart pattern and market structure intelligence)

**Date:** 2026-11-17 · **Branch:** `claude/focused-cori-b8em6u` · **Code commit:** `7e84573`
**Plan:** `docs/PARTS27_30_AUDIT.md` (Part 28 rows)

## 1. Summary

| Req. | Class | Delivered |
|---|---|---|
| 28.1 Preserve existing patterns | A | Untouched. The registry still has exactly 215 strategies (tested). |
| 28.1 Catalogue | E | `app.patterns.catalogue`, derived at run time from each strategy's own metadata, never redefining it. 215 originals in 13 pattern families (classical, candlestick, price structure, SMC, ICT, liquidity, Fibonacci, Wyckoff, VSA, order flow, gaps/imbalances, …; non-pattern strategies flagged `is_pattern: false`), plus the 6 new detectors. |
| 28.2 Harmonic | E | Gartley, Bat, Butterfly, Crab (XABCD) and AB=CD, using the commonly published ratio ranges (±3 % tolerance, stated in the code and in every record) |
| 28.2 Elliott | E | Conservative impulse counts. Only the three hard rules decide validity; Fibonacci guidelines set quality only. Every record says counts are subjective and other valid counts may exist. |
| 28.3 Detection records | E | `pattern_detections` (migration 0013) holds points, ratios, levels, lifecycle history, quality, regime / session / spread / data context, detector version, the bar at which it became known, and found live vs reconstructed from history. No duplicates. |
| 28.4 Multi-timeframe | B | Alignment across **all** scanned timeframes (latest regime direction of each), in the decision trace and at `/api/patterns/alignment` |
| 28.5 Pattern-to-strategy | E | For each open formation, the strategies on the same bar that support it (same direction) or contradict it |
| 28.6 Pattern learning | E | Outcomes per pattern × timeframe × regime: target reached, invalidated, false breakouts (confirmed then invalidated), expired, did not complete, success rate, average MFE/MAE in R. Fewer than 30 decided outcomes is flagged insufficient. |
| 28.7 Dashboard | E | **Pattern Intelligence** page |
| Agent | E | `pattern_recognition` in the same registry (27 agents); a `patterns` stage and a `timeframe_alignment` stage in each opportunity's decision trace |

**Advisory only.** The detectors are new and unvalidated, so they never produce strategy signals, never enter the signal engine, and never change a decision. The pattern service does not even receive the signal result.

## 2. How the detectors avoid look-ahead and noise

* **Pivots:** a ZigZag over the engine's existing causal swing detection. A pivot is only known `strength` bars after its extreme. Legs smaller than 1 ATR are merged away.
* **Lifecycle:** FORMING → COMPLETED → CONFIRMED → TARGET_REACHED, or INVALIDATED / EXPIRED.
  * FORMING ends INVALIDATED if price closes through the completion zone without forming D.
* **Measurement:** the entry is the **close of the bar at which the formation became known**, never the pivot's own price. R = |entry − invalidation|.
  * A bar that touches both the invalidation and the target counts as invalidated (conservative).
  * These are price-bar measurements, labelled "not trades" in the API and UI.
* **Harmonic levels:**
  * invalidation beyond X (Gartley, Bat) or 0.272 × CD beyond D (Butterfly, Crab, AB=CD);
  * targets 38.2 % / 61.8 % of AD (of CD for AB=CD);
  * confirmation: a close 23.6 % back;
  * expiry after 30 bars.
* **Elliott:**
  * FORMING expects wave 5 and is invalidated by a close inside wave 1's territory.
  * COMPLETED expects a correction, is confirmed beyond the end of wave 4, and is invalidated by a new extreme beyond wave 5. Target: a 38.2 % retracement of the impulse; expiry after 40 bars.
* **Records:**
  * deduplicated by pivot **times**, not indices, so the same formation is found again on the next analysis;
  * forward-only, and a closed record is final;
  * an open formation that disappears because a pivot was revised is closed as INVALIDATED ("pivots revised"), not left open.

## 3. Tests and results

| Suite | Result |
|---|---|
| Engine, full (alone, local) | **354 passed** (343 + 11 new), 0 failed, 0 skipped |
| `tests/test_part28.py` | 11 passed |
| CI on `7e84573` | backend 44038442555 ✅, frontend 44038442524 ✅, desktop 44038442487 ✅ (including the Windows installer build/install/upgrade/uninstall) |

New tests use synthetic price paths with known geometry:
* **Gartley:** a bullish one (full lifecycle to target; ratios within 0.02 of 0.618 / 0.786), its bearish mirror (SELL), and price running through the zone (did not complete).
* **Failures:** a completed Gartley that confirms and then breaks X (a false breakout).
* **Other patterns:** a Butterfly, and AB=CD.
* **Causality:** on the bars before D was confirmed, nothing is COMPLETED.
* **Elliott:** a valid impulse is counted; a wave-4 overlap and a shortest wave 3 are not.
* **Catalogue:** 215 originals exactly; no detector code collides with a strategy code.
* **Records:**
  * deduplication; forward movement of the same record;
  * a later, older view never reopens it;
  * relations to strategies; revision → INVALIDATED;
  * learning statistics, including false breakouts and the insufficient flag.
* **Scan and API:** a scan records the agent and both trace stages; the API (401, 404, 422, catalogue size, stats basis, alignment).

**Fixes during testing:** both failures were in my test paths, not the detectors. One "failure" path never formed a D pivot; the other had AB=CD legs in the wrong order. While fixing them, a formation that never completes now records `{"outcome": "INVALIDATED", "completed": false}` instead of no outcome at all.

**Existing test changed:** `test_orchestration.py`'s registry list gains `pattern_recognition`.

## 4. End-to-end on the installed application

1. **Upgrade:** the database was at 0012 (72 analyses, 25 signals, 215 strategies). Through `supervise restart_all` it is now at **0013** with every row kept.
2. **Live run:** an event-driven re-analysis of both symbols ran the pattern agent on the live analysis role: `pattern_recognition ok {"detections": 0, "new": 0, "active": 0}`.
   * **The test connector's synthetic bars contain no harmonic or Elliott formation**, so nothing was recorded on this install. Detection itself is verified by the tests above, not by this install.
3. **Through the gateway:**
   * `/api/patterns/catalogue` → 221 entries (215 + 6);
   * `/api/patterns/stats` → empty, with the "not trades" basis;
   * `/api/patterns/alignment?symbol=EURUSD.r` → H1 CONSOLIDATION, not aligned (only H1 is scanned on this install);
   * `/api/agents` → 27.
4. **Console:** Pattern Intelligence renders from the rebuilt console image with honest empty states (catalogue 122 pattern entries in 13 families).

## 5. Limitations

* Detection quality on real broker data is unknown until formations accumulate. The statistics show sample sizes and flag fewer than 30 decided outcomes.
* Elliott labelling is deliberately narrow: impulses only (no diagonals, no named corrections such as zigzags or flats).
* Harmonic thresholds (the ±3 % tolerance, the 1-ATR minimum leg) are fixed constants. Tuning them is a research task, not a production change.
* Patterns are advisory. Letting them influence decisions would need out-of-sample validation and administrator approval, as for every model.

## 6. Security

* **New endpoints** are read-only and use the existing session authentication: `GET /api/patterns/catalogue`, `/detections`, `/detections/{id}`, `/stats`, `/alignment`.
* **No AI and no broker access** in this phase.

## 7. Files

* **Created:**
  * `backend/app/patterns/` (`__init__`, `pivots`, `lifecycle`, `harmonic`, `elliott`, `catalogue`, `service`);
  * `backend/migrations/versions/0013_pattern_detections.py`;
  * `backend/tests/test_part28.py`;
  * `frontend/app/(console)/patterns/page.tsx`;
  * this report.
* **Modified:**
  * `backend/app/db/models_opportunity.py`, `orchestration/agents.py`, `scanner/service.py`, `api/routes/intel.py`;
  * `tests/conftest.py`, `tests/test_orchestration.py`;
  * `frontend/components/Shell.tsx`;
  * manuals (user, README, release notes).

## 8. Rollback

Revert the commits and run `alembic downgrade 0012`.

## 9. Next

**Phase 6 (Part 29):** strategy selection, discovery and learning, and DEMO/LIVE automation.
