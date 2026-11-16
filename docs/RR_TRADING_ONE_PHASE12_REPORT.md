# RR Trading One — Phase 12 Report

**Phase:** 12 — Adaptive strategy intelligence (specification Part 12)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 12 asks to **reuse** the existing regime, consensus, probability, performance, portfolio and optimisation
modules, keep the original signal weights as the baseline, test adaptive changes in shadow first, and use only
independently validated evidence. Phase 12 adds one module, `app/learning_engine/adaptive.py`, that sits on those
modules. It adds **no new tables and no migration**:

* weight profiles are rows in `model_registry`, with the same signed artefacts, lifecycle, approval and rollback as
  every model (Phase 11);
* shadow and applied decisions are rows in `model_predictions`.

| Part 12 item | Built on | What Phase 12 adds |
|---|---|---|
| Best strategies per symbol, timeframe, regime, session | Phase 10 `performance.load_trades` / `compute_metrics` | Strategy × slice tables; "best" only where the lower 95% bound of expectancy is above zero |
| Strategy ranking | same | Ranked on the **lower bound** of realised expectancy; fewer than 30 trades is listed as INSUFFICIENT_DATA, unranked; backtest OOS/WF shown beside, never summed in |
| Deteriorated strategies | `drift` performance events | + last 30 trades significantly worse than the earlier ones (bootstrap) |
| Redundant / conflicting strategies | `fingerprint` static and behavioural similarity | + **co-firing in the scanner's own analyses**: same bar, same or opposite direction |
| Combinations with independent value | Phase 10 combinations | + increment over either strategy alone, with a 95% interval |
| Stable configurations, parameter sensitivity | optimiser `optimisation_results` | Neighbour check: in-sample winner vs out-of-sample, and one-parameter neighbours → STABLE / FRAGILE / OVERFIT |
| Regime probability analysis | `regime_history` (regime intelligence) | Transition matrix and persistence; strategies' realised mean R weighted by the current regime probabilities, with coverage |
| Strategy correlation, portfolio correlation | existing P&L correlation, consensus vote matrix, `portfolio` | Unchanged; co-firing added |
| Failure intelligence | `failure_reports` | Most frequent loss causes per strategy |
| Confidence calibration | `signals.engine.Calibrator` | **Unchanged on purpose**: a profile never alters calibrated confidence |
| Strategy fingerprinting | `fingerprint` | Shown in the redundancy panel |
| **Adaptive weight proposals** | `model_registry` lifecycle | Per strategy × regime multipliers in [0.5, 1.5], only where there are ≥ 30 trades; signed |
| **Walk-forward comparison** | — | Adaptive vs original weights on 3 expanding, embargoed folds + a held-out period |
| **Shadow evaluation** | `model_predictions` | Every scan records the original and the profile's decision; outcomes counted only where known |

**Baseline**
* With no profile in PRODUCTION, the engine's output is identical to the original (tested, including a neutral profile).
* A profile only scales one strategy's setup strength inside `signals.engine`.
* A VALIDATED or APPROVED profile only **records** what it would have decided.
* Applying a profile needs an administrator. Retiring it returns to the original weights.

| Result | Value |
|---|---|
| End-to-end on the running install (real learner, analysis and api containers, real scan cycles) | **21 / 21** (section 3) |
| Backend suite, including the 215-strategy preservation test | **276 passed** (260 + 16 new) in one full run |
| Frontend `next build` | Passed, 29 pages (new: Adaptive Intelligence) |
| Noise check of the profile validation | **0 / 200** pure-noise datasets validated; a ±1 R regime edge validated **40 / 40** |

## 2. Defects found

**1. Consensus weights could no longer be approved (a regression from my Phase 11 work).**
* Phase 11 made every approval check the model's signed artefact.
* `consensus_weights` models stored their weights only in `metrics` and had no artefact. A validated set of weights
  was therefore refused approval ("signature no_artifact"), so the consensus could never leave prior mode.
* Confirmed by running the new test against the Phase 11 code.

**Fixed**
* The weights are now the model's artefact, signed like any model.
* The consensus engine reads them **only from the verified artefact**, so editing the displayed metrics in the
  database changes nothing.
* An unusable set falls back to prior mode, labelled as such.
* Verified in the installed learner and api containers: signed, then approved through the API.

**2. Co-firing counted directions instead of strategies (my own new code, caught by its test).**
* `Counter.update(dict)` adds the dictionary's *values*, so it accumulated direction strings.
* **Fixed:** it counts one per strategy that fired.

**Found, not changed (original behaviour, kept as the baseline):** the original engine's historical-performance
factor (`scanner.strategy_stats`) sums out-of-sample, walk-forward, paper and live results into one profit factor.
* Changing it would change the baseline signals, which Part 12 says to preserve.
* Adaptive profiles do not use it; they use one account mode's realised trades.
* Recorded for Phase 13 (signal fusion), where the engine's evidence inputs are in scope.

## 3. End-to-end verification

Run on the running install (schema 0008, secrets format 5): the patched images, the real learner, analysis and api
containers, and the connector on the simulated feed.

1. **Upgrade.**
   * No migration and no new secret.
   * Without evidence the engine keeps the original weights. The learner had already run the new weekly job on start-up and correctly **REJECTED** it: "INSUFFICIENT DATA: 0 DEMO trades".
2. **Evidence.**
   * 400 DEMO trades were seeded on real strategy codes: `combo_bos_fvg` earns in one regime and loses in the other, `combo_breakout_atr` the reverse, and two strategies have no edge.
   * The LIVE view stayed at 0 trades.
   * The per-regime analysis found each strategy best in its own regime.
   * Overall, all four rank INCONCLUSIVE. That is correct, and it is why per-regime weighting exists.
3. **Proposal.**
   * The triggered learner job produced a VALIDATED profile, signed with the installation's key: held-out improvement +0.103 R per trade (95% interval 0.055 to 0.153), 3 walk-forward folds.
   * The agent run was recorded.
   * The profile runs in shadow only.
4. **Shadow.**
   * Real scan cycles in the analysis container verified the learner's signature and recorded shadow decisions beside the unchanged original ones.
   * The scan cycle's agent run includes the adaptive step.
5. **Tampering.**
   * A newer profile proposed through the API was altered in the database, with its SHA-256 recomputed.
   * The API reported it "invalid" and refused approval (409).
   * The analysis container did not use it.
   * After it was rejected through the API, the earlier profile was the shadow again.
6. **Governance.**
   * The administrator approved and applied the profile.
   * Scan cycles then recorded its decisions as used.
   * "Back to original" (retire) restored the original weights.
   * The audit trail recorded APPROVED, PRODUCTION, RETIRED.
7. **Consensus weights:** learner-fitted, signed and approved through the API (defect 1).
8. **Adaptive Intelligence page:** profiles, ranking, slices, redundancy, regime outlook, parameters and failures, with no console errors.

**How the runs went**
* The first attempt assumed the seeded profile would be v1. The learner had already created v1 (rejected), so I stopped that run and changed the script to look profiles up by their evidence.
* The page check then failed twice on my checker's timing (it read the page before the navigation and the slice table had loaded). After the fix, the page check passed on the same install.

**Not covered end-to-end:**
* The simulated feed has flat prices, so no strategy fires. The scan decisions are therefore NO_TRADE in both the original and the profile's version (agreement 100%).
* Shadow decisions that differ, and their outcome accounting, are covered by the unit tests (decisions, agreement, avoided R, counterfactuals counted but not estimated).

## 4. How a profile is validated

* **Evidence:** closed trades of **one** account mode with an R multiple and a recorded strategy, oldest first. AI recommendations are never an input.
* **Proposal:**
  * multiplier = 1 + mean R shrunk towards 0 by 30 pseudo-trades, clipped to [0.5, 1.5];
  * a strategy × regime cell needs ≥ 30 trades, otherwise the strategy's all-regime cell is used; failing that, the original weight;
  * a profile needs ≥ 150 trades in total.
* **Test:** each unseen trade is weighted by the profile's mean multiplier for its strategies in its regime. The weighted mean R is compared with the plain (original) mean R. A VALIDATED profile requires all of:
  * a held-out last 30% (after a one-day embargo) with the improvement's 95% interval above zero;
  * 3 expanding walk-forward folds with mean improvement ≥ 0.02 R and at most one fold not positive;
  * at least one multiplier different from 1.
* **Noise check:**
  * 200 noise datasets: none passed;
  * a ±1 R regime edge on 400 trades: 40 / 40 passed;
  * the weaker edge I planted first (+0.6 / −0.4 R) was not detected in either of the 2 datasets I probed. The test is deliberately conservative.

## 5. Files

**Created**
* `backend/app/learning_engine/adaptive.py`
* `backend/tests/test_adaptive.py` (16)
* `frontend/app/(console)/adaptive/page.tsx` (navigation: "Adaptive Intelligence", item 13 of Part 17)
* this report

**Modified**
* `backend/app/signals/engine.py`: optional `MarketContext.adaptive`. When empty (the default), the result is the original.
* `backend/app/scanner/service.py`: shadow/production step in each analysis; records linked to the signal.
* `backend/app/learning_engine/scheduler.py`: weekly `adaptive_weights` job.
* `backend/app/learning_engine/consensus_weights.py`, `backend/app/intelligence/consensus.py`: defect 1.
* `backend/app/api/routes/learning.py`: `/api/intelligence/adaptive/{overview,slices,redundancy,regimes,parameters,fit}`. Approval, promotion and rollback reuse `/api/learning/models/...`.
* `backend/app/orchestration/agents.py`: the Adaptive Optimisation agent's implementation reference and learner-job mapping.
* `frontend/components/Shell.tsx`: navigation entry.

**Preserved:**
* all 215 strategies;
* the original signal weights, grades and calibration;
* the model lifecycle (unchanged);
* every original test.

## 6. Known limitations

* **No profile has been fitted on real trading.** The install has no real DEMO trades yet, and the sandbox feed is flat. Seeded trades prove the pipeline, not market skill.
* **Regime granularity:** profiles use the scanner's primary regime (the same value the engine receives). The richer regime-intelligence probabilities are used only in the outlook report.
* **Symbol, timeframe and session** are reported (which strategies work where) but not yet weighted. Each extra dimension divides the evidence; it can be added once real trade counts support it.
* **Shadow outcomes:** only trades the original engine actually took have outcomes. Trades only the profile would have taken are counted, never estimated.
* **Profiles come from DEMO trades by default** (the weekly job). A LIVE profile can be proposed on request (`mode=LIVE`); modes are never pooled.

## 7. Security implications

* Profiles and consensus weights are **signed** and verified on **every use** in the analysis container. A database writer cannot change the weights (tested end-to-end).
* Applying a profile requires administrator permission (`trade.live`). Proposing requires `strategy.manage`; viewers are refused (403).
* No AI output, and no AI recommendation, is an input to profiles. The learner stays read-scoped and cannot place orders.
* LIVE is still locked.

## 8. Rollback

* **Without code changes:** "Back to original" (retire the PRODUCTION profile) restores the original weights immediately; the previous profile can also be rolled back to.
* **Revert the Phase 12 commits:** no migration to undo. Profile rows remain in `model_registry`, unused.
* After a revert, consensus weights fitted by Phase 12 (signed artefact) remain readable by the Phase 11 code.

## 9. Next phase

**Phase 13 — probability and signal fusion (Part 13)**
* Combine the original strategy outputs, regime, multi-timeframe alignment, independent evidence families, calibrated probabilities, correlation-adjusted consensus, news, spread, anomalies and portfolio exposure into explainable BUY / SELL / WAIT / NO_TRADE decisions.
* LOW DATA CONFIDENCE / INSUFFICIENT DATA where the evidence is thin.
* The original engine stays the benchmark.
* This includes the source-mixing finding in section 2.
