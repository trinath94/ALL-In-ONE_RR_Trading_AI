# RR Trading One — Phase 16 Report

**Phase:** 16 — DEMO and LIVE separation (specification Part 16)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 16 asks for both account modes, with LIVE locked by default and separated from DEMO in every respect. The
separation was already largely in place. Phase 16 audited it item by item, added the test this project lacked (the
**whole LIVE lifecycle** against a terminal reporting a REAL account, in-process), checked every refusal on the
installed app, and fixed three gaps. **LIVE trading was never enabled on the install.**

| Part 16 item | Where | Evidence |
|---|---|---|
| DEMO is the initial mode | trading state default; desktop first-run registers DEMO only | **end-to-end**: DEMO, auto trading OFF, no approved LIVE account; desktop local API agrees |
| DEMO: analysis, learning, backtesting, controlled execution after explicit authorisation | Phases 8–15 | Phase 15: worker typed phrase + auto trading + AI confirmation + Risk Engine |
| LIVE locked by default | no LIVE account; execution requires an approved, verified LIVE account | **end-to-end**: every switch attempt refused |
| Separate broker profile, identity and credentials | one record per login@server with its own broker connection and Fernet-encrypted password; login@server unique | **end-to-end**: separate connection, password encrypted and distinct; the DEMO identity cannot also be LIVE (**fixed**: a clear 409, not a server error) |
| Separate risk configuration | `risk_configs` per mode; LIVE default 0.25% per trade | `test_risk_config_validation_and_isolation`; **lifecycle test**: the LIVE order was sized with the LIVE configuration |
| Administrator approval | `approve-live` needs `trade.live` and prior verification | lifecycle test; **end-to-end**: refused before verification and for a trader |
| Typed confirmation | `APPROVE <login>`; `ENABLE LIVE TRADING`; explicit confirmation for manual LIVE orders | lifecycle test; **end-to-end** (428 without the phrase) |
| Audit trail | creation, verification, approval, **revocation (new)**, activation, mode switch | lifecycle test; **end-to-end**: every LIVE-related change was made by a signed-in person |
| Validate login **and** exact server | `trading.sync_account` compares login, exact server string and account type (REAL for LIVE; DEMO/CONTEST for DEMO) | `test_identity_is_exact_login_exact_server_and_account_type` (wrong server, wrong login, DEMO record on a REAL account, LIVE record on a DEMO account); **end-to-end**: "Connected terminal is 1001@Demo-Server, expected 2002@Live-Server", through the relay |
| A mode switch blocks new submissions until verification and reconciliation | switch clears `startup_checks_passed` and auto trading; `open_position` refuses until the startup checks pass; a switch is refused while executions are unresolved | lifecycle test: entry refused after the switch, accepted only after the checks; `test_mode_switch_refused_while_executions_are_unresolved`; **end-to-end** 409 "unresolved" |
| AI agents cannot enable LIVE or approve their own permissions | AI tasks return schema-checked text only and hold no session; LIVE needs `trade.live`; role changes need `users.manage` and an administrator cannot demote themselves | registry test (Phase 14): no AI-using agent on the execution path; **end-to-end** agents endpoint; Phase 9 boundary tests |

| Result | Value |
|---|---|
| End-to-end on the running install (LIVE never enabled) | **17 / 17** (section 3) |
| New tests | 3: the full LIVE lifecycle, exact identity, unresolved executions block a switch |
| Backend suite, including the 215-strategy preservation test | **295 passed** (292 + 3 new) in one full run |
| Frontend `next build` | Passed, 29 pages |
| CI (`36ddfed`) | backend 43914609413 and frontend 43914609525 passed. Desktop 43914609789 **failed**: the installer's silent-uninstall check raced the NSIS uninstaller (it tested the connector folder before the uninstaller had removed it). No desktop code had changed; the check was fixed in Phase 17 (it now waits for both paths, as it already did for the app). |

## 2. Gaps found and fixed

**1. Registering an identity twice gave a server error.**
* Adding the DEMO identity (1001@Demo-Server) again as a LIVE account hit the database's unique constraint and returned an unhandled error.
* The data was protected, but the API gave no explanation.
* Now: a 409 that says which mode the identity is already registered in. Confirmed on the original code (UniqueViolation) and fixed.

**2. LIVE approval could not be revoked.**
* Once approved, a LIVE account stayed approved, with no way to re-lock it short of editing the database.
* Now: `POST /api/accounts/{id}/revoke-live` (administrator, `trade.live`, audited) and a "Revoke live" button in Settings.
* New LIVE entries are refused at once. If the account is the active LIVE account while LIVE is active, auto trading is switched off.

**3. A mode switch said nothing about positions left behind.**
* Positions open on the account being left keep their broker stops, but they are not managed (break-even, partial close, trailing) while the other mode is active.
* Now: the switch returns that warning, records the count in the audit trail, and states that new entries are blocked until verification and reconciliation. The Control Center shows both.

The Risk Engine, identity checks, execution path and existing permissions are unchanged.

## 3. End-to-end verification

Run on the running install: DEMO account 1001@Demo-Server, with the PC's connector logged in to it through the relay.

0. **Initial state.**
   * DEMO, auto trading OFF, no approved LIVE account; the startup reconciliation had passed.
   * The desktop's local supervisor API reports the same.
1. **Separate identity.**
   * A LIVE record 2002@Live-Server was added: unapproved and inactive, with its own broker connection and an encrypted password distinct from DEMO's.
   * Registering the DEMO identity as LIVE was refused with a clear 409.
2. **Exact verification.**
   * Verifying the LIVE record against the PC's terminal was refused with "Connected terminal is 1001@Demo-Server, expected 2002@Live-Server". The record stayed unverified.
   * Approval before verification was refused (409).
3. **Switching to LIVE, refused at every step:**
   * without the phrase (428);
   * with the phrase but no approved account (409);
   * by a trader (403 for approve, switch and revoke).

   Revoking as an administrator was audited.
4. **Blocking during a switch.**
   * With an unresolved execution, even with a LIVE record made to look approved directly in the database (test setup only, undone afterwards), the switch was refused: "unresolved".
   * A DEMO-to-DEMO switch changed nothing.
5. **AI.**
   * No AI-using agent is on the execution path.
   * Every LIVE-related change in the audit trail was made by a signed-in person.
   * The only change without a user is the desktop app registering the DEMO account on first run (`via: local-supervisor`), which is not LIVE.
6. **Pages.** Settings shows the LIVE account unapproved with "Approve live"; the Control Center shows DEMO and the verified account. No console errors.
7. **Clean-up.** The test LIVE record was removed and the test user disabled.

**How the runs went:** the first run passed 14 / 17. All three failures were in my script:
* the test setup did not make the LIVE record active, which a switch also requires;
* my audit check counted the desktop's own DEMO registration as if it were a LIVE change;
* the page check read the Control Center while the mode still showed "…" (loading).

After those fixes the rerun passed 17 / 17.

**Never done:** enabling LIVE, approving a real LIVE account, or any LIVE order on the install. The positive LIVE path
(verify, approve, activate, switch, reconcile, confirmed order with the LIVE risk configuration, revoke) runs only
in-process, against a fake terminal reporting a REAL account (`test_live_lifecycle_end_to_end`).

## 4. Files

**Created**
* `backend/tests/test_live_separation.py` (3)
* this report

**Modified**
* `backend/app/api/routes/trading.py`: duplicate identity refused (409); `revoke-live`; mode switch warnings and audit detail.
* `frontend/app/(console)/settings/page.tsx`: "Revoke live".
* `frontend/components/ControlCenter.tsx`: switch warnings.
* `docs/RR_TRADING_ONE_PHASE15_REPORT.md`: CI results.

**Preserved:** the Risk Engine, identity checks, execution path, permissions, all 215 strategies and every original
test. No migration.

## 5. Known limitations

* **One terminal at a time.** The PC's connector is logged in to one account. Positions left on the other mode's account are protected by their broker stops but not managed until that mode is active again (now stated at the switch).
* **Approval is by one administrator.** Part 16 asks for administrator approval; a second approver (four-eyes) is not implemented.
* **A switch to LIVE with the wrong terminal logged in** fails the startup identity check and trips the kill switch. This is deliberately conservative; an administrator clears it after logging the terminal in to the right account.

## 6. Security implications

* Stricter or clearer only: a re-lock (revocation) exists, the duplicate refusal is explicit, and switch warnings are recorded.
* LIVE stays locked on the install. No AI path can reach any of these actions.

## 7. Rollback

* Revert the Phase 16 commit. No migration.
* An account revoked with the new endpoint stays unapproved after a revert, which is the safe direction.

## 8. Next phase

**Phase 17 — Windows user interface (Part 17)**
* Audit the desktop console against the 27 navigation items and the Command Centre / Control Room fields Part 17 lists.
* Map each to its page and fill the missing ones from data the engine already has.
