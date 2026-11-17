# RR Trading One — Parts 27–30, Phase 8 Report (integration)

**Date:** 2026-11-17 · **Branch:** `claude/focused-cori-b8em6u` · **Commits:** `7439715`, `269baaa`

## 1. Summary

Phase 8 checks that what Phases 4–7 added works **as one system**, closes the one Section 6 validation no test
covered, and makes all 35 required validations a CI gate.

| Item | Delivered |
|---|---|
| End-to-end flow | `test_parts27_30_flow_end_to_end_in_demo` (§2) |
| Guards hold even after AI approval | `test_an_inconsistent_feed_is_refused_by_the_risk_engine_even_after_ai_consensus` (§3) |
| Partial fills (Section 6, item 21) | `test_partial_fill_is_recorded_and_the_filled_volume_is_what_gets_managed`; the simulated broker gained an optional partial fill (MT5 DONE_PARTIAL, 10010) |
| Section 6 in CI | `scripts/test_matrix.py` now maps all **35** required validations to tests and fails CI if any is missing or failing; the frontend build is checked through its workflow |
| Dashboard (Section 3) | The Signal Centre shows each signal's decision trace (state, reason, every stage). The Command Centre has an opportunity lifecycle panel (24 h counts, qualified ones). Links open an opportunity (`/opportunities?id=…`). The API filters opportunities by signal. |

## 2. The end-to-end flow (DEMO, real code)

Only the broker (FakeMT5 behind the real bridge) and the two AI providers (scripted) are simulated. One test:
1. **Scan:** strategies, regime, patterns, multi-timeframe alignment, data quality, calendar, signal engine, consensus, probability, anomalies, fusion, entry validation, exit plan, failure prediction, strategy selection, health, decision.
2. **AI review in consensus mode:** two providers answer independently; both confirm. The review is recorded with both votes.
3. **Execution:** auto-execution goes through the Risk Engine (APPROVED) and the journalled execution manager; the order is confirmed by the broker.
4. **Lifecycle:** the monitor's sync moves the opportunity to EXECUTED and records the Risk Engine and execution stages. The trace has exactly 23 stages, in order: `data, market_regime, strategies, multi_timeframe, timeframe_alignment, patterns, news, fundamental, signal_engine, adaptive, consensus, probability, anomalies, fusion, entry_validation, exit_plan, failure_prediction, strategy_selection, health, decision, ai_review, risk_engine, execution`.
5. **Agents:** fundamental, entry validation, exit optimisation, failure prediction, AI consensus, pattern recognition and strategy ranking all recorded runs. Pattern records have no duplicates.
6. **Research:** a scenario simulation of the executed setup runs on the later bars and **does not touch the broker**.
7. **Readiness:** the LIVE readiness checklist is not ready (no LIVE account).
8. **Kill switch:** it trips; auto trading off, positions kept with their stops (PROTECT), **still DEMO**.

## 3. A finding: the guards hold after AI approval

The first run of the flow test executed nothing. The test feed's bars are a day old and its quote is 9–13 ATR away
from their closes. Everything up to the AI consensus approved, and the **Risk Engine still refused every order**:
signal expiry, critical anomaly ("unexpected jump"). Nothing reached the broker.

That is the required behaviour (the Risk Engine has final authority), so it is now its own test. The flow test uses a
consistent feed instead: current bars, and the quote at the last close, as a real broker gives.

## 4. Results

| Suite | Result |
|---|---|
| Engine, full (local, alone) | **398 passed** (394 + 3 integration tests + the Phase 7 learner-recovery test), 0 failed |
| MT5 connector / Windows connector | 17 / 36 passed |
| Desktop core | 133 passed |
| Test matrix from these results | **all 25 areas, 14 safety conditions and 35 Parts 27–30 validations proven by passing tests** (74 rows, 0 FAIL); `docs/TEST_MATRIX.md` regenerated |
| CI on `7439715` | backend ✅ 44066035334 (including the matrix step with the 35 validations), desktop ✅ 44066035113, frontend ✅ 44066035208, windows-connector ✅ 44066035420 |

**On the installed application:** the new images were deployed through `supervise restart_all`.
* The Command Centre shows the opportunity lifecycle panel.
* The Signal Centre shows the decision-trace section. For the install's signals, which predate Parts 27–30, it truthfully says there is no opportunity record.

## 5. Limitations

* The end-to-end execution uses a consistent simulated feed. A real broker's data and fills are exercised only in DEMO acceptance on your PC (Phase 9).
* The PC connector's own LIVE lock is unchanged; RR Trading One cannot release it (Phase 10).

## 6. Files

* **Created:** `backend/tests/test_parts27_30_integration.py`; this report.
* **Modified:**
  * tests and CI: `mt5-connector/tests/fake_mt5.py` (optional partial fills), `scripts/test_matrix.py`, `.github/workflows/backend.yml` (frontend workflow given to the matrix), `docs/TEST_MATRIX.md`;
  * engine: `backend/app/api/routes/intel.py` (opportunity filter by signal);
  * console: the Signal Centre, Command Centre and Opportunity Lifecycle pages;
  * manuals: user manual, release notes.

## 7. Rollback

Revert the commits. Tests, CI and console only, plus one optional API filter.

## 8. Next

**Phase 9: DEMO validation.** Acceptance on your PC with a real DEMO account: the readiness checklist's DEMO track record, data quality on a live feed, opportunity traces from real setups, AI review with your providers if configured. It needs your machine and your DEMO account.
