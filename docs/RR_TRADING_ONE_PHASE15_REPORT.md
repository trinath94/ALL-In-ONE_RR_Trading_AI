# RR Trading One — Phase 15 Report

**Phase:** 15 — Trading worker and execution (specification Part 15)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 15 asks to **preserve** the existing worker, bridge, execution manager, relay and Risk Engine, without creating a
second execution engine, and to verify their safety properties and initial state.

Phase 15 is therefore an audit with two fixes. The audit maps every Part 15 item to the code and to the test that
proves it. It also adds the end-to-end run this project had never done: **the installed trading-worker container
auto-executing a DEMO candidate through every gate**. That run found one real gap.

| Part 15 item | Where | Evidence |
|---|---|---|
| Idempotent order submission | `execution.manager._reserve` (dedupe key reserved before sending) | `test_duplicate_signal_never_executes_twice`; **end-to-end**: the same candidate offered again → still one order, one position |
| Execution journal | `trade_orders` + `trade_executions` | **End-to-end**: order CONFIRMED, retcode 10009, filled volume and price |
| HMAC verification | `execution.bridge` (method, path, timestamp, nonce, body hash); connector verifies | `mt5-connector/tests/test_connector.py::test_rejects_unsigned_and_bad_signature`; relay `test_socket_rejects_bad_key_and_replayed_hello` |
| Nonce replay protection | connector | `test_replayed_nonce_rejected`; `test_refused_read_request_does_not_burn_a_nonce_for_the_full_secret` |
| Broker retcode verification | `manager._apply_result` | `test_order_confirmed_by_mt5_and_synchronised`; **end-to-end** retcode 10009 |
| Unknown execution reconciliation | `manager.reconcile_unknown`; startup checks block entries while any is unresolved | `test_lost_response_is_reconciled_not_resent`, `test_entries_blocked_until_startup_checks_pass` |
| Position adoption | `trading.sync_positions` at startup | `test_restart_adopts_broker_positions_and_protects_unprotected` |
| Stop protection | startup PROTECT policy; the order carries its stop-loss | same test; **end-to-end**: order and broker position carry the stop-loss |
| Partial close, break-even, trailing | `risk.manage_position` via `worker.manage_positions` (journalled, idempotent) | `test_position_management_partial_and_break_even`, `test_break_even_and_trailing` |
| Daily loss, drawdown, consecutive losses | Risk Engine; trips the risk lock | `test_daily_loss_limit_locks_trading`, `test_drawdown_and_consecutive_losses` |
| Emergency stop | `manager.kill_switch` with PROTECT / REDUCE / CLOSE_ALL | `test_kill_switch_closes_and_blocks`; **end-to-end**: position closed at the broker, later candidates not executed, auto trading cannot be re-enabled while it is set |
| Worker independently controlled | own container, compose profile `trading`, never in a plain start; typed phrase | **end-to-end**: wrong phrase refused, correct phrase starts it |
| Initial state: worker STOPPED, auto trading OFF, DEMO, LIVE LOCKED | defaults + desktop supervisor | **end-to-end** on the install (section 3) |
| Only explicitly authorised execution | auto trading + AI confirmation + Risk Engine + idempotency | **two gaps fixed** (section 2) |
| All trades pass the original Risk Engine | `manager.open_position` → `risk.evaluate` | **end-to-end**: approved and sized; poor reward:risk refused with "R:R 0.43 < 1.0" |
| Learner and research agents never trade | read-scoped bridge credential; no execution path | `test_read_scope_reads_but_can_never_execute`, `test_analysis_never_executes_...`; registry test: no AI-using agent on the execution path (Phase 14) |

| Result | Value |
|---|---|
| End-to-end on the running install (real trading-worker container, DEMO) | **20 / 20** (section 3) |
| Backend suite, including the 215-strategy preservation test | **292 passed** (289 + 3 new) in one full run |
| CI (`017bf9c`) | backend 43909702027, desktop (installer build + silent install/uninstall) 43909702217. Both passed (no frontend change, so the frontend workflow did not run). |

## 2. Defects found and fixed

**1. A candidate produced while auto trading was OFF was executed once it was switched ON.**
* **Found end-to-end.** A confirmed candidate created while auto trading was off stayed NEW and unexpired. The moment auto trading was switched on, the worker executed it.
* The worker already refuses candidates created *before it started*, for exactly this reason: no retroactive execution. It did not apply the same rule to auto trading being off.
* The user switching auto trading on authorises what comes next, not a backlog.

**Fixed**
* The API records when auto trading was last switched on (`trading_state.auto_trading_since`, migration 0010, additive).
* The worker executes only candidates created after it. Re-sending "on" does not move the time.
* Regression test: fails on the original code, passes now. Verified end-to-end.

**2. The worker accepted an AI review in either direction.**
* `auto_execute_factory` required the AI review to be BUY or SELL, not to equal the signal's direction.
* The gateway's boundaries (Phase 9) already refuse a reversed review, so this could not happen through normal use. But the execution path depended on that alone.

**Fixed:** the review must confirm the same direction. Regression test (fails on the original code); verified end-to-end.

Neither change touches the Risk Engine, the bridge, the journal or sizing.

## 3. End-to-end verification

Run on the running install (DEMO account 1001 on the fake MT5 terminal, through the relay), with the real monitor,
analysis and trading-worker containers.

Candidates were inserted as the scanner stores them (decision, grade, stop-loss, target, AI review). The test feed's
flat prices rarely produce an A-grade candidate, so waiting for the scanner would not reliably exercise the
execution path.

1. **Upgrade and initial state.**
   * Schema 0010.
   * Worker STOPPED, auto trading OFF, DEMO, no administrator-approved LIVE account.
   * The monitor's startup reconciliation had passed.
2. **Worker start.** The wrong phrase was refused; `START TRADING WORKER` started it.
3. **Auto trading OFF.** A confirmed candidate was not executed.
4. **Auto trading ON (DEMO, audited).**
   * The new confirmed candidate was executed: the Risk Engine approved and sized it, the source was AUTO, and the stop-loss was sent.
   * The broker confirmed (retcode 10009) with fill volume and price.
   * The position appeared from the broker with its stop-loss.
   * Not executed: an unreviewed candidate, a review in the other direction, a WAIT, a candidate from before the worker started, and the candidate from while auto trading was off.
   * A poor reward:risk candidate was refused by the Risk Engine with its reasons ("R:R 0.43 < 1.0", plus the position already open).
   * Agent runs were recorded.
5. **Idempotency.** The executed candidate was set back to NEW; there was still exactly one order and one position.
6. **Emergency stop (CLOSE_ALL).**
   * The position was closed at the broker.
   * A later confirmed candidate was not executed.
   * Re-enabling auto trading was refused (409).
7. **Restored.** Auto trading OFF, kill switch cleared by the administrator, worker stopped, DEMO.

**How the runs went**
* The first run found defect 1: the "while off" candidate was executed, and the Risk Engine then refused the intended one because a position was already open.
* After the fix, the second run passed 20 / 20.
* During verification I briefly ran two copies of the backend suite against the same test database, which deadlocked. I stopped them and reran the suite on its own (section 4).

**Not covered end-to-end:**
* Break-even, partial close and trailing, because the test terminal's price does not move with time. They are covered against the fake terminal and in unit tests.
* Daily loss and drawdown limits, which are unit-tested.
* LIVE: never enabled.

## 4. Tests

* New:
  * `test_trading_role_never_executes_a_direction_the_review_did_not_confirm`;
  * `test_signals_from_while_auto_trading_was_off_are_never_executed_later`;
  * `test_switching_auto_trading_on_records_when` (role tests: 16 passed).
* Full backend suite, run on its own, including the 215-strategy preservation test: **292 passed** (289 + 3 new).

## 5. Files

**Created**
* `backend/migrations/versions/0010_auto_trading_since.py`
* this report

**Modified**
* `backend/app/workers/main.py`: two checks in `auto_execute_factory`.
* `backend/app/api/routes/trading.py`: record the switch-on time.
* `backend/app/db/models.py`: one column.
* `backend/tests/test_roles.py`: 3 tests.

**Preserved, unchanged:** the Risk Engine, the bridge, the relay, the execution manager, the journal, sizing, the
connector, all 215 strategies and every original test.

## 6. Known limitations

* **Mode switching** (Part 16) is the next phase. Today a switch turns auto trading off and requires reconciliation again; Phase 16 audits the rest of DEMO / LIVE separation.
* **Upgrade edge case.** If auto trading was already ON before this upgrade, there is no switch-on time, and candidates created after the worker started are executed as before. Switching auto trading off and on again sets it.
* **Manual execution** from Live Signals ("Execute via Risk Engine") is unaffected by design: a person chooses the signal explicitly. It still goes through the Risk Engine.

## 7. Security implications

* Execution is stricter: one more condition (switch-on time) and one tighter one (same direction). Nothing was loosened.
* LIVE stays locked. The end-to-end run used DEMO only and restored the initial state.

## 8. Rollback

* Revert the Phase 15 commit; `alembic downgrade 0009` drops the column.
* Without the column, the worker falls back to its original behaviour (worker-start rule only).

## 9. Next phase

**Phase 16 — DEMO and LIVE separation (Part 16)**
* Audit separate profiles, identities, credentials and risk configuration; administrator approval and typed confirmation; audit trail.
* Exact login and server validation.
* New submissions blocked during a mode switch until verification and reconciliation complete.
* AI agents cannot enable LIVE or approve their own permissions.
* Verified without ever enabling LIVE trading.
