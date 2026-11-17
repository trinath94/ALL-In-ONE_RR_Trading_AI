# RR Trading One — Phase 22 Report

**Phase:** 22 — Testing (specification Part 22)
**Date:** 2026-11-17
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 22 asks to preserve and run every existing test, including the original engine's baseline and preservation
tests. It names 25 areas to test and 14 safety conditions that must hold.

Phase 22 mapped every item to the specific tests that prove it and wrote tests for the items no existing test
covered directly. It added an upgrade check to the Windows installer CI, and a checker that reads the **actual test
results** and fails if any mapped test is missing or did not pass. The checker runs in CI.

| Result | Value |
|---|---|
| Part 22 items proven by passing tests | **25 / 25 areas, 14 / 14 safety conditions** (121 test references; `docs/TEST_MATRIX.md`) |
| Engine suite, incl. the original 215-strategy preservation tests | **323 passed** (316 + 7 new) |
| MT5 bridge (original connector) suite | **17 passed** |
| RR MT5 Connector suite | **36 passed** |
| Desktop core (Rust) | **120 passed** |
| Every original test of the baseline engine | preserved and passing. Compared with commit `5c6c26b`: no original test file removed, no test skipped, no assertion changed. Two original *helpers* were extended: the test-database cleanup list includes the newer tables, and the simulated MT5 terminal records the deal reason real MT5 records (plus an opt-in moving price series). |
| CI | Engine, both connector suites and the matrix: backend workflow. Rust tests on Linux and Windows, plus the matrix: desktop workflow. Windows install, **upgrade** and uninstall: desktop workflow. Dependency scans: security workflow. |

## 2. What was missing, and the new tests

| Part 22 item | Gap | New test |
|---|---|---|
| **Existing API routes** | No test checked that the original engine's routes still exist | `test_every_route_of_the_original_engine_still_exists`: the route inventory of the original engine (commit `5c6c26b`, generated from its OpenAPI document and kept as a fixture) compared with today's app. **All 112 original HTTP routes are present with their methods, and both WebSocket routes keep their paths.** (The engine now serves 153.) |
| **Database migrations** | Exercised only indirectly (every test session migrates a fresh database) | `test_migrations_form_one_linear_chain` (0001 → 0011, one head, no merges); `test_downgrade_to_base_and_upgrade_to_head_and_schema_matches_models` (full downgrade, full upgrade, and Alembic's comparison finds **no table or column** differing between the migrations and the models) |
| **Application upgrades** | The Windows CI installed once and uninstalled; it never installed over an existing installation | Windows CI: user settings and a backup are created and one program file is altered (an "older version"), then the installer runs again. Required: **every program file matches the new version** (manifest hashes), and **settings and backups are unchanged**, also after the uninstall. |
| **Safety 13**: existing positions are not blindly closed by an emergency stop | The existing end-to-end test used the explicit CLOSE_ALL policy | `test_emergency_stop_blocks_entries_but_does_not_blindly_close_positions`. With no policy given (the default PROTECT), the position **stays open at the broker** and new entries are refused. The PC's emergency button and identity failures (which carry no broker connection) touch no position at all. |
| **Safety 1–3**: the learner cannot submit orders; AI cannot enable LIVE or modify execution permissions | Covered at the gateway and registry level, not as a whole-code boundary | `test_learner_ai_and_agents_have_no_path_to_orders_live_or_permissions`: a static check of every module of the learner, AI layer, agents, orchestration, intelligence and news (35 files). None references order submission, the kill switch or position policies, account activation or LIVE approval, or roles, permissions and permission checks. A companion test proves the check catches a violation. `test_learner_and_analysis_containers_cannot_trade`: those containers get only the read-only bridge credential. |

## 3. The test matrix

`scripts/test_matrix.py` lists, for each of the 25 areas and 14 safety conditions, the tests that prove it:
* engine tests;
* the original MT5 bridge tests;
* RR MT5 Connector tests;
* desktop Rust tests;
* named Windows CI checks.

It reads the real results (pytest JUnit XML and the `cargo test` output) and fails when a mapped test is missing,
failed or was skipped. Checked: a test marked failed makes it exit 1, and missing tests are listed by name.
`docs/TEST_MATRIX.md` is the generated table from this run.

Examples of what the safety conditions map to:
* **4. Wrong broker identity:** engine start-up identity check; the bridge refusing a mismatched account; the connector disconnecting a wrong account instead of trading it.
* **8. Duplicate submissions:** the same signal never executes twice; the bridge executes an order once; the trading role executes a confirmed signal once.
* **11. Model promotion:** approval required with rollback; a candidate must not be worse than production; signed artefacts, with promotion refused for viewers and traders.
* **14. Preservation:** the original three preservation tests (215 IDs, configuration and code hashes, experimental code kept out).

## 4. Findings

* **No new defect** in the engine. Every safety condition already held, and the new tests confirm it on today's code.
* **The default emergency policy is safe,** but only an explicit CLOSE_ALL had been tested end to end. Now the default is.
* **The Phase 21 Windows CI failure** (the ACL test compared text with Windows line endings) was fixed before this phase; run 44012842457 passed on Linux and Windows.

## 5. Files

**Created**
* `backend/tests/test_part22.py` (7 tests)
* `backend/tests/fixtures/baseline_routes.json` (the original engine's routes)
* `scripts/test_matrix.py`
* `docs/TEST_MATRIX.md`
* this report

**Modified**
* `.github/workflows/backend.yml`: JUnit results for the three Python suites; the matrix check.
* `.github/workflows/desktop.yml`: the `cargo test` output and the matrix check (Linux); the upgrade check; settings and backups survive the uninstall.
* `docs/RR_TRADING_ONE_PHASE21_REPORT.md`: CI result of the fix.

**Preserved:** every existing test, unchanged.

## 6. Known limitations

* **The upgrade check reinstalls the same version over an "older" file,** because no earlier release has been published. It proves the upgrade path (files replaced, data kept), not a jump between two real releases.
* **The Windows install, upgrade and uninstall are tested on GitHub's Windows runner,** without Docker Desktop or a real MT5 terminal. The engine stack is tested on Linux Docker; the connector against a simulated terminal.
* **Performance tests** (`test_performance.py`) measure latency on the machine running them; they are not a load test of a real broker catalogue.

## 7. Rollback

Revert the Phase 22 commit: only tests, a fixture, a script and CI steps change.

## 8. Next phase

**Phase 23 — Migration from the existing Ubuntu deployment (Part 23).** The verified database import exists (Phases
18–19). The remaining work is to audit Part 23's full migration and cut-over requirements against it.
