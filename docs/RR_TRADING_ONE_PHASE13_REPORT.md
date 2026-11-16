# RR Trading One — Phase 13 Report

**Phase:** 13 — Probability and signal fusion (specification Part 13)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 13 asks to reuse `signals/engine.py`, `intelligence/probability.py` and `intelligence/consensus.py`, combine all
the evidence into explainable BUY / SELL / WAIT / NO_TRADE decisions, never treat WAIT or NO_TRADE as executable, never
call a raw score a probability, say LOW DATA CONFIDENCE / INSUFFICIENT DATA when evidence is thin, and keep the
original signal engine as the benchmark.

**Audit (before Phase 13)**
* The scanner's decision was the original engine's.
* Regime, probability, consensus and anomaly results were attached to each analysis but never combined into one decision with an explanation.
* Consensus could only downgrade, and only with `RR_CONSENSUS_MODE=enforce`.
* Execution was already safe: the worker executes only BUY / SELL, and the Risk Engine blocks critical anomalies and exposure breaches.
* Two console pages labelled the engine's confluence score "AI score". It is a rules-based score, not an AI output and not a probability.

**Phase 13 adds one module, `app/intelligence/fusion.py`, run in every analysis. No new tables and no migration:** the
fused decision is stored with the analysis.

| Part 13 input | Source (reused) | How it is used |
|---|---|---|
| Original strategy outputs | `signals.engine` | The benchmark decision, grade and confluence score |
| Market regime | regime intelligence | Supports / opposes / caution |
| Multi-timeframe alignment | engine HTF bias | Explains (already in the engine score) |
| Independent evidence families, correlation-adjusted consensus | `intelligence.consensus` | **Opposing consensus downgrades BUY/SELL to WAIT** |
| Historical performance | learner performance snapshots, **active account mode only** | Explains |
| Calibrated probabilities | probability model in PRODUCTION; engine score calibration | Shown only when calibrated; **a calibrated model expecting no positive R after costs downgrades to WAIT** |
| News risk | news service | **Restriction window blocks: NO_TRADE**; unknown feed is a caution, never assumed clear |
| Spread | engine | Explains (already in the engine score) |
| Market anomalies | `intelligence.anomaly` | **Unresolved critical anomaly blocks: NO_TRADE** |
| Portfolio exposure | open positions + `portfolio` correlations | Explains; the Risk Engine enforces limits at execution |

**Rules** (all tested)
1. The fused decision is **never more aggressive** than the original engine's: BUY/SELL may stay, or become WAIT or NO_TRADE. It is never upgraded and never reversed.
2. WAIT and NO_TRADE are never executable.
3. Only blocks and two specific disagreements change the decision; everything else explains it. Evidence the engine already scored (multi-timeframe, spread) is not counted twice.
4. **Evidence status:**
   * `CALIBRATED`: an out-of-sample probability exists, shown with its source and sample;
   * `LOW_DATA_CONFIDENCE`: some evidence, too little;
   * `INSUFFICIENT_DATA`: none, so no probability is shown.

   The confluence score is always labelled "not a probability". A shadow (unapproved) model is never used.
5. **Benchmark:**
   * `RR_FUSION_MODE=advisory` (the default, like `RR_CONSENSUS_MODE`) keeps the original decision and records the fused one beside it.
   * `enforce` uses the fused decision for signals.
   * A benchmark report compares the two: how often they differ, and the outcome of original-engine trades the fused decision would have held back.

| Result | Value |
|---|---|
| End-to-end on the running install (real scan cycles in the analysis container) | **13 / 13** (section 3) |
| Backend suite, including the 215-strategy preservation test | **283 passed** (276 + 7 new) in one full run |
| Frontend `next build` | Passed, 29 pages |

## 2. Findings and fixes

1. **"AI score" labels.**
   * The Command Centre and Market Scanner called the engine's rules-based confluence score an "AI score".
   * Renamed to "Confluence"; the score is shown as `/100`, and the fusion view states "not a probability".
2. **The Phase 12 source-mixing finding.**
   * The original engine's historical factor sums out-of-sample, walk-forward, paper and live results.
   * It is **left unchanged**, because Part 13 says to keep the original engine as the benchmark.
   * The fused decision's historical factor uses realised trades of the **active account mode only**.
3. **No end-to-end coverage of signals before Phase 13.**
   * The test MT5 double produced flat bars, so no strategy fired in any earlier end-to-end run.
   * I added an opt-in `FAKE_MT5_SERIES=walk` series to the test double (`mt5-connector/tests/fake_mt5.py`, test-only code): trending waves plus noise, a pure function of symbol and hour, so every request returns the same bar.
   * 73 strategies fire on it. The default stays flat, so all existing tests are unchanged.

## 3. End-to-end verification

Run on the running install (schema 0008, secrets format 5, patched images). The connector serves the moving test
series, and the scanner covers M15, M30, H1 and H4.

1. **Upgrade.** No migration and no new secret; fusion mode advisory by default.
2. **Real scan cycles.** 24 analyses produced 9 signals. The original engine decided WAIT 9 times and NO_TRADE 15 times; no BUY/SELL was graded A this run. On every one of the 24 analyses:
   * a fused decision was recorded;
   * the stored decision was the original engine's (advisory);
   * the fused decision was never more aggressive and never reversed, and was executable only for BUY/SELL;
   * a probability was shown only with a calibrated source and its sample, and the score was labelled not a probability;
   * all factors were listed, and the explanation starts with the decision.
3. **Evidence.**
   * 9 analyses were CALIBRATED. They used the probability model in production, which is the test model trained on *planted synthetic data* during the Phase 11 end-to-end run. This proves the wiring, not market skill.
   * 15 were INSUFFICIENT_DATA, with no probability shown.
4. **API.**
   * Signals expose their fused decision and the original engine's; none of the WAIT signals is executable.
   * The benchmark report counted the analyses.
   * The scan cycle's agent run records the fusion step.
5. **Pages.**
   * Predictive Intelligence opens on the new Fusion tab.
   * Live Signals shows the fusion detail: every factor with its effect, evidence and source.
   * Neither page labels the confluence score an "AI score". No console errors.
6. **Restore.** Scanning back to H1 and the flat series; `stop_all`.

**How the runs went**
* The page check failed once on my checker: the Command Centre's opportunities table renders no header when there are no BUY/SELL candidates.
* That check also uncovered the Market Scanner label (finding 1).
* After the fix and a frontend rebuild, the page check passed on the same install.

**Not covered end-to-end:**
* No BUY/SELL candidate appeared in this run, so the downgrade paths (consensus against, calibrated probability against, blocks) and `enforce` mode were not exercised on the install.
* They are covered by unit tests, including an exhaustive check over every combination of engine decision, consensus, probability state, news status and anomaly block.

## 4. Files

**Created**
* `backend/app/intelligence/fusion.py`
* `backend/tests/test_fusion.py` (7)
* `frontend/components/Fusion.tsx`
* this report

**Modified**
* `backend/app/scanner/service.py`: fusion step in every analysis, stored as `analysis_results.score.fusion`; enforce mode.
* `backend/app/core/config.py`: `fusion_mode` (`RR_FUSION_MODE`, default advisory).
* `backend/app/api/routes/learning.py`: the signal detail includes fusion; `/api/intelligence/fusion/{latest,benchmark}`.
* `backend/app/orchestration/agents.py`: the Signal Quality agent's implementation reference.
* `frontend/app/(console)/signals/page.tsx`: fusion detail; "Confluence" column.
* `frontend/app/(console)/predictive/page.tsx`: Fusion tab (default).
* `frontend/app/(console)/page.tsx`, `frontend/app/(console)/scanner/page.tsx`: "AI score" changed to "Confluence".
* `mt5-connector/tests/fake_mt5.py`: opt-in moving series (test double only).

**Preserved:**
* all 215 strategies;
* the original engine's decisions, weights, grades and calibration;
* consensus and its mode;
* the Risk Engine;
* every original test.

## 5. Known limitations

* **Advisory by default.** The fused decision only drives signals with `RR_FUSION_MODE=enforce`, an environment setting with no console switch. Before enforcing, compare it with the original engine in the benchmark report on real DEMO trading.
* **The benchmark can only score what was traded.** An original-engine trade the fused decision would have held back has an outcome. A trade neither took has none, and is never estimated.
* **Historical performance needs the learner's daily snapshot.** Until then it reads as unavailable.
* **The probability model currently in production on this install is a test model** (Phase 11 end-to-end). Retire it (Learning Centre) before relying on probabilities; retraining on real broker history replaces it.

## 6. Security implications

* Fusion is deterministic; no AI output is an input. The AI review keeps its own boundaries (Phase 9).
* Execution safety is unchanged and does not depend on fusion:
  * the worker executes only BUY / SELL;
  * the Risk Engine still checks anomalies, news and exposure.
* No new permissions, endpoints that change state, secrets or tables. LIVE stays locked.

## 7. Rollback

* Revert the Phase 13 commits; there is no migration. Stored `score.fusion` entries are ignored by older code.
* **Without reverting:** leave `RR_FUSION_MODE` unset (advisory) and the original engine decides.

## 8. Next phase

**Phase 14 — multi-provider AI studio (Part 14)**
* Build on the Phase 9 gateway:
  * Qwen and Ollama as named providers, with the model per task;
  * cost and token tracking, rate-limit handling, fallback and health;
  * model version history.
* Keys stay in Windows-protected storage.
* AI output stays untrusted and schema-validated.
