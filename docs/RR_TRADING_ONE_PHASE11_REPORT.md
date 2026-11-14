# RR Trading One — Phase 11 Report

**Phase:** 11 — AI learning and model validation (specification Part 11; Phase 1 gap 7.8)
**Date:** 2026-11-14
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 11 asks to **reuse** the existing learner, validation criteria and model lifecycle. Phase 11 therefore began
with an audit against the 21 Learning Centre items and the validation rules. Most were already met by the Part 25/26
code; Phase 11 fixed what was missing and found two serious defects.

| Part 11 requirement | Before | Now |
|---|---|---|
| Learner independent of the trading worker; never places orders | ✅ separate service with a read-only bridge credential (Phase 3/4) | Unchanged; still read-scoped and without the trading key (verified) |
| Items 1–14 (deal import and sync, partial exits, enrichment, MAE/MFE, outcomes, failure patterns, performance by strategy, symbol, timeframe, regime and session, memory, datasets) | ✅ Part 25/26 + Phase 10 | Unchanged |
| Chronological datasets, embargo, no future data, realistic costs | ✅ features from closed bars only; labels include spread; SL first on ambiguous bars; embargoed splits | Unchanged |
| **Walk-forward evaluation** | ❌ one train/validation/test split | **Expanding-window walk-forward** (4 folds, embargo). The candidate must show skill across folds, with an **effect-size floor** |
| Never promote on training score | ✅ out-of-sample significance + calibration | + walk-forward + comparison with production |
| **Compare candidates with the current baseline** | Partly: the base rate only | **Also the production model**, on the candidate's own held-out data; a worse candidate is rejected |
| Validation before shadow mode; admin approval for production | ✅ | + signature checks |
| Drift detection | ✅ | Unchanged |
| **Recommend retraining** | ❌ drift notified, nothing recommended | **One OPEN retraining recommendation** per model with critical drift (not duplicated) |
| Model history and rollback | ✅ | + a rollback target must be authentic |
| **Model authenticity (gap 7.8)** | ❌ pickled artefacts with a SHA-256 in the same row | **HMAC-signed artefacts** with a DPAPI-protected key; unsigned or altered artefacts are never loaded, approved, promoted or rolled back to |
| Learner's research on the AI gateway (Phase 9 finding 5) | ❌ called Claude directly | **Through the gateway** (routes, validation, ledger) |

| Result | Value |
|---|---|
| End-to-end as an **upgrade** of the running install (real learner, analysis and api containers) | **5 / 5** upgrade checks + **16 / 16** for the rest (see section 3) |
| Backend suite, including the 215-strategy preservation test | **260 passed** (250 + 10 new) in one full run |
| Desktop core (secrets format 5 upgrade) | **91 passed**; clippy clean |
| Frontend `next build` | Passed, 28 pages |
| CI | backend 43541003935 (`27e47a0`: 260 tests + the image build, where all 5 algorithms load on a normal network); desktop (installer build + silent install/uninstall) 43539694697 (`856e83a`, the same Phase 11 code); frontend 43518897566. All passed. |

## 2. Defects found

**1. Model training could not run in the installed engine.**
* The backend image (and the original `docker/backend.Dockerfile`) lacks `libgomp1`, the OpenMP runtime LightGBM needs.
* Importing LightGBM raised `OSError`, but the trainer caught only `ImportError`, so **every training run in the container crashed**.
* Unit tests never saw it, because the development machine has the library. The first end-to-end training run in a real container exposed it.

**Fixed**
* `infrastructure/backend.Dockerfile` installs `libgomp1`.
* The trainer treats any optional library that fails to load as unavailable, and logs it.
* **CI now builds the shipped image and asserts that all 5 candidate algorithms load.**
* The original Ubuntu deployment file is left unchanged, as for every original file; it has the same defect.

**2. A cached model could skip signature verification.**
* The probability estimator cached loaded models by row contents.
* The full test run exposed it: a model identical to one cached earlier, under rules where signing was not yet enforced, was served from the cache **with its signature removed**.
* In production that is a window where a stripped or forged signature goes unnoticed until the process restarts.

**Fixed:** the signature is verified on **every** use, before the cache is consulted; only the unpickled object is
cached. Two regression tests cover it.

**3. Walk-forward on pure noise passed "skill > 0".**
* Found by my own test: a random forest on a no-signal dataset reached a mean Brier skill of 0.0021, with 3 of 4 folds barely positive.

**Fixed:** a minimum mean skill of 0.01 (1% better than the base rate). A test now checks 4 noise datasets × every
candidate algorithm, and walk-forward never passes. The planted-signal tests still validate.

## 3. End-to-end verification

**Upgrade (first run, 5 / 5)**
* Starting point: format-4 secrets.
* Start All succeeded; schema 0008 **with a fresh pre-migration backup**.
* **Secrets format 5 in place:** the database password and the AI keys key were kept, and the signing key was added.
* The signing key is only in api, analysis and learner. The learner and analysis services are still read-scoped, with no trading key.

**The rest (resumed run, 16 / 16)**
* **Training in the learner container:** two planted-signal datasets were VALIDATED and signed; noise was REJECTED by walk-forward.
* **The analysis container verified and loaded the learner's model** with the same installation key.
* **Tampering:** the artefact was altered *and its SHA-256 recomputed*, as an attacker with database access would do. The API reported "invalid", approval was refused (409), and **the analysis container refused to load it**.
* **Governance:**
  * a signed model was approved and promoted through the API (administrator);
  * the next candidate was compared with production on its own held-out data: Brier 0.1697 against 0.1736, so it was not worse;
  * a model with no signature was reported "unsigned".
* **Retraining:** one OPEN recommendation, not duplicated.
* **Research:**
  * the learner's weekly research job ran through the gateway against the fake provider;
  * the proposal was stored as DISCOVERED;
  * the provider key stored in Phase 9 still decrypted after the upgrade;
  * the ledger and the agent run were recorded.
* **Learning Centre:** valid, invalid and unsigned signatures, walk-forward folds and the production comparison were shown; no console errors.

**How the runs went**
* The first run's training step exposed defect 1.
* After the fix, a sandbox restart interrupted the run. The rerun resumed on the upgraded install (the 5 upgrade checks had passed and are one-time).
* Its first two attempts failed on my script: shell quoting, and a lookup by version that matched two different models.

**CI failures on the way (none weakened):**
* `730b674`: CI caught defect 2 independently (the cache test failed). Fixed in `856e83a`.
* `856e83a`: all 260 tests passed, but the new image-build step ran from `backend/` instead of the repository root. Fixed in `27e47a0`.

**Sandbox-only:** this sandbox's network blocks Debian's package mirrors, so its copy of the image exposes the OpenMP
runtime that scikit-learn's wheel already bundles instead of running `apt-get install libgomp1`. The shipped
Dockerfile change is verified by the new CI step on a normal network.

## 4. Files

**Created**
* `backend/migrations/versions/0008_model_signatures.py` (one column, `model_registry.artifact_hmac`)
* `backend/tests/test_model_governance.py` (10)
* this report

**Modified**
* `backend/app/learning_engine/models.py`: signing, verification, walk-forward, production comparison, tolerant library loading
* `backend/app/intelligence/probability.py`: verified on every use; unusable means unavailable
* `backend/app/learning_engine/drift.py`: retraining recommendations
* `backend/app/learning_engine/scheduler.py`, `backend/app/agents/strategy_discovery/agent.py`: research and development through the gateway
* `backend/app/api/routes/learning.py`: signature state; research endpoints check the gateway
* `backend/app/core/config.py`, `backend/app/db/models_intel.py`
* `desktop/core/src/{secrets,supervisor}.rs`: format 5
* `infrastructure/compose.yml`, `infrastructure/backend.Dockerfile`
* `.github/workflows/backend.yml`: key holders; image build with model libraries
* `frontend/components/Intel.tsx`: models panel

**Preserved:** all 215 strategies; the original validation criteria and lifecycle (only stricter); every original
test.

## 5. Known limitations

* **No model has been trained on real broker data.** The sandbox's simulated feed has flat prices, so strategies do not fire and the weekly training job finds no dataset. Planted datasets prove the pipeline, not market skill.
* **Models from before Phase 11 are unsigned** and therefore unusable once the key is set. There is deliberately no "re-sign" button, because re-signing an artefact of unknown origin would defeat the purpose: **retrain**.
* **Walk-forward folds use raw (uncalibrated) probabilities** against the training base rate; the hold-out test uses the calibrated model.
* **The production comparison uses the candidate's test window,** which may overlap the production model's own training period if that model was trained recently.
* **Costs:** labels include the spread but not slippage or commission (the label is "take-profit before stop-loss", which commission does not change).

## 6. Security implications

* **Closes gap 7.8.** Writing to the database is no longer enough to get code executed through an unpickled model. The HMAC key is DPAPI-protected and held by only 3 services, and verification happens on every use.
* The learner gains the AI keys key (for its research through the gateway). It stays read-scoped, without the trading encryption key, and cannot place orders (verified).
* Unchanged: production still requires an administrator, and LIVE stays locked.

## 7. Rollback

* Revert the Phase 11 commits; `alembic downgrade 0007` drops the one column.
* Secrets format 5 is backward compatible.
* **Without reverting:** unsetting `RR_MODEL_SIGNING_KEY` returns to SHA-256-only checking (not recommended).

## 8. Next phase

**Phase 12 — adaptive strategy intelligence**
* Use the validated evidence (Phase 10 trade intelligence, Phase 11 models) to adapt strategy weighting per regime, symbol and timeframe.
* Only through proposals with evidence, administrator approval and rollback.
* Never by changing strategy code.
