# RR Trading One — Phases 24–26 Report

**Phase:** Parts 24 (development phases), 25 (required deliverables) and 26 (implementation reporting)
**Date:** 2026-11-17
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

The last three parts of the specification are about completeness rather than new features:
* **Part 24:** the 17 development phases are done and verified;
* **Part 25:** the 29 deliverables exist;
* **Part 26:** every phase was reported honestly.

This phase audited all three against the repository, wrote the documents that were missing, shipped them inside the
installer, and recorded what is not delivered and why.

| Part | Result |
|---|---|
| 24: development phases | All 17 mapped to delivered phases (`docs/rr-trading-one/README.md`). The exception is the **updater** half of phase 16. |
| 25: deliverables | **28 of 29 delivered.** Missing: **19, the application updater** (needs your release hosting and signing decisions). |
| 26: reporting | Every phase has a report: files, database changes, tests and real results (including failed runs), limitations, security, rollback, next phase. Earlier claims later found wrong carry dated corrections. |

## 2. Documents written

`docs/rr-trading-one/`, shipped with the application in `docs\` of the install folder:

| Deliverable | File |
|---|---|
| 22. Installation manual | `INSTALLATION_MANUAL.md` (requirements, folders, first-run wizard, upgrade, uninstall) |
| 23. User manual | `USER_MANUAL.md` (desktop window, typed phrases, the console's pages, DEMO trading, stopping) |
| 24. Administrator manual | `ADMINISTRATOR_MANUAL.md` (roles, DEMO/LIVE, risk, models, AI, supervisor, secrets, monitoring, maintenance) |
| 25. Technical architecture | `ARCHITECTURE.md` (components, flows, data, design decisions) |
| 26. Security document | `SECURITY.md` (threat model, controls, keys, limitations) |
| 27. Migration manual | `../MIGRATION_FROM_UBUNTU.md` (Phase 23) |
| 28. Recovery manual | `RECOVERY_MANUAL.md` (backups, restore, new PC, common situations, lost PC) |
| 29. Release notes | `RELEASE_NOTES.md` (0.2.0) |
| Index and deliverables audit | `README.md` |

The engine's original documents (`docs/ARCHITECTURE.md`, `INSTALL.md`, `RECOVERY.md`, …) describe the Ubuntu deployment
and are unchanged. The root README gains an RR Trading One section at the top, with the original text kept below it.

## 3. Findings while writing the manuals

* **LIVE cannot be completed from RR Trading One.**
  * The PC's MT5 connector has its own LIVE lock, bound to the account; the desktop window shows it as "LIVE on this PC: locked" and has no control to release it.
  * Every engine-side LIVE step exists and is tested in-process, but a LIVE order would be refused by the connector.
  * This matches the rule "do not enable live trading during development". It is now stated plainly in the user and administrator manuals and the release notes. Building that control, with a typed confirmation bound to the account, is the remaining step before LIVE.
* **`desktop/README.md` was stale:** it said the main window had five commands; it has 24. Corrected.
* **Exact figures in the manuals** were read from the code, not recalled: backup retention 30/10/10, resource limits, typed phrases, folders.

## 4. The updater (deliverable 19)

A safe updater must:
* download from a release location the app can reach without a token, which a private GitHub repository is not;
* verify an update signature whose public key is built into the app;
* ideally, carry a Windows code-signing certificate so SmartScreen trusts the installer.

Each needs a decision or an asset from you. Until then, updating is installing the newer installer over the old one;
CI verifies this keeps every setting, backup and database (Phase 22).

## 5. Verification

* **Bundle staging:** 246 files, including the manuals, all listed in the bundle manifest. Windows CI checks that every manifest entry is installed with the same hash. Two manuals are named explicitly in the install check.
* **Links:** every relative link in the new documents, the migration manual and the root README resolves (scripted check).
* **No code behaviour changed** in this phase; the suites from Phases 22–23 stand (engine 323, desktop 130, connectors 17 + 36).

## 6. Files

**Created**
* `docs/rr-trading-one/` (8 files)
* this report

**Modified**
* `README.md` (new section on top)
* `desktop/README.md`
* `desktop/scripts/stage_bundle.py` (ships the manuals)
* `desktop/src-tauri/tauri.bundle.conf.json` (`docs/` resource)
* `.github/workflows/desktop.yml` (manuals in the install check; docs changes trigger the build)

## 7. Rollback

Revert this commit: documentation, the bundle list and a CI check only.

## 8. Next

1. **Acceptance testing on your PC** with a DEMO account, using the installer this commit builds.
2. Your decisions on the **updater and code signing**.
3. **Releasing the PC's LIVE lock** from the desktop window. This is the remaining step before LIVE, to be done only when you decide LIVE should become possible.
