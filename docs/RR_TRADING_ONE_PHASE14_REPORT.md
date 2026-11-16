# RR Trading One — Phase 14 Report

**Phase:** 14 — Multi-provider AI studio (specification Part 14)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Phase 9 built the AI gateway that Part 14 describes. Phase 14 began with an audit of that gateway against every Part 14
item and completed what was missing.

| Part 14 item | Before | Now |
|---|---|---|
| Anthropic Claude API | ✅ | Unchanged |
| **Qwen** | Only as a generic "OpenAI-compatible" provider; the user had to know the URL | **Presets**: Model Studio international and mainland China (DashScope compatible mode); keys are region-specific (stated) |
| **Ollama / local models** | Possible, but `localhost` inside the engine is the container | **Preset** with `http://host.docker.internal:11434/v1`, the address of the Windows host from Docker Desktop, and the reason |
| Compatible future providers | ✅ any `/chat/completions` API | Preset "Other OpenAI-compatible API" |
| Different models per task (6 tasks) | Routes for all 6, but **2 tasks were never called** | **All 6 wired**: learning explanations and research summaries added (section 2) |
| Provider configuration | ✅ | Prices and per-minute limit editable in the console (prices were API-only) |
| **Model selection** | Model typed by hand | **Discovered from the provider's own model list** (`/models`; Anthropic models API); offered in the route and test fields |
| API key storage, encrypted credentials | ✅ Fernet with a DPAPI-protected key; write-only | Unchanged; keys still decrypt after the upgrade (end-to-end) |
| Usage, token, cost tracking | ✅ ledger; cost only with a configured price, never guessed | + the **requested** model beside the served one |
| Timeouts, retry policies | ✅ per provider | Unchanged |
| **Rate-limit handling** | 429 / Retry-After, cooldown after repeated failures | + an optional **per-minute limit per provider**: above it the gateway moves to the next provider instead of calling; this is not counted as a provider failure |
| Fallback selection, provider health | ✅ | Unchanged |
| **Model version history** | ❌ only the served model string per call | **History per provider and requested model**: every version served (first and last seen, calls, tasks), changes flagged, plus the audit trail of route and provider changes |
| Keys in Windows-protected storage; no broker credentials to AI | ✅ (DPAPI; credential-like fields redacted) | Unchanged; verified again end-to-end |
| "A Claude subscription does not provide API credits" | ✅ stated | Also in the Anthropic preset |
| AI output validated, untrusted | ✅ strict schemas | Also for nested items (research summary items refuse unknown fields) |

| Result | Value |
|---|---|
| End-to-end as an **upgrade** of the running install (schema 0008 → 0009) | **14 / 14** (section 3) |
| Backend suite, including the 215-strategy preservation test | **289 passed** (283 + 6 new) in one full run |
| Frontend `next build` | Passed, 29 pages; the page is now **AI Studio** (Part 17 item 23) |
| CI (`e847afa`) | backend 43904095602, desktop (installer build + silent install/uninstall) 43904095430, frontend 43904095486. All passed. |

## 2. The two tasks wired in this phase

**Learning explanations** (`learning_explanation`), from the "Explain in plain language (AI)" button:
* **Where:** on a model or weight profile (Learning Centre), and on a fused decision (Live Signals).
* **Input:** only the facts the engine recorded: metrics, validation, signature, dataset, or the fusion factors.
* **Instructions:** never invent results, state when evidence is thin, never call a score a probability, never recommend approving, trading or sizing.
* **Output:** shown labelled "AI-generated explanation of recorded results; not evidence". It is used as evidence nowhere and changes nothing (tested: the model's status is unchanged).

**Research summaries** (`research_summary`), from the "Summarise" button in the Learning Centre:
* **Input:** the period's research records, i.e. trade research, retraining recommendations, strategy proposals and their stage, models and profiles, and fusion against the original engine.
* **Output:** a digest that cites the record ids it used. It is stored as an OPEN recommendation for reading and applies nothing.

Each task has its own route, so a different provider and model can be chosen per task (verified: `fake-llm` for
explanations, `fake-llm-mini` for summaries).

## 3. End-to-end verification

Run as an upgrade of the running install, with the real api container and an OpenAI-compatible test provider on the
engine's egress network. The Phase 9 provider and its stored key were reused.

1. **Upgrade.**
   * Migration 0009 was applied after a fresh pre-migration backup.
   * The new columns are present, and existing ledger rows were kept (their requested model is "not recorded").
2. **Presets and discovery.**
   * The five presets have the stated URLs and no hard-coded model lists.
   * Model discovery returned the provider's own list, sent with the Phase 9 key: it still decrypts after the upgrade.
3. **Tasks.**
   * Explanations of the production model and of a Phase 13 fused decision; a research summary stored as OPEN, citing records.
   * The ledger shows each task on its own model.
   * Agent runs were recorded.
   * Nothing credential-like reached the provider.
4. **Model version history:** the provider started serving a new version of the same model. The history shows `fake-llm` served as two versions in order, flagged as changed, with the route changes listed.
5. **Per-minute limit:**
   * at 1 request per minute, the second call in the minute went to the next provider in the route;
   * the ledger records "local limit of 1 requests per minute reached";
   * the first provider was not put into cooldown.
6. **Pages, no console errors.**
   * AI Studio: preset choice, discovery ("2 models offered"), limit column, version history.
   * Learning Centre: an explanation (AI-labelled) and a research summary.

**How the runs went:**
* My check of the pre-migration backup compared the number of backup files, which stays constant because old backups are rotated out. The start report shows the backup was taken before migration 0009 was applied.
* I corrected the check ("the newest backup was written by this start") and reran it: passed.

**Not covered end-to-end:**
* Real Qwen, Anthropic and Ollama endpoints (no keys, and no Ollama, in this sandbox). Their adapters are the Phase 9 ones.
* Model discovery is unit-tested for both adapter kinds.
* `host.docker.internal` is provided by Docker Desktop on Windows; it was not exercised here.

## 4. Findings

1. **Two Part 14 tasks were routable but never used.** Learning explanations and research summaries showed "not used by the engine". Fixed (section 2).
2. **The agent registry understated AI use (my Phase 10 work).**
   * The Post-Trade Learning agent has made AI trade-research calls since Phase 10 but was registered as deterministic.
   * It is now HYBRID, as is the AI Learning agent (explanations).
   * The Phase 9 registry test now names all four AI-using agents and asserts that **none is on the execution path**.
3. **Prices could only be set through the API.** Without a price, cost shows "no price configured" (by design), and the console offered no way to enter one. Now editable.

## 5. Files

**Created**
* `backend/migrations/versions/0009_ai_studio.py` (additive): `ai_usage.requested_model`, `ai_providers.max_requests_per_minute`
* `backend/app/ai/studio.py`
* `backend/tests/test_ai_studio.py` (6)
* `frontend/components/AiText.tsx`
* this report

**Modified**
* `backend/app/ai/providers.py`: presets, `list_models`.
* `backend/app/ai/gateway.py`: requested model in the ledger, per-minute limit, all tasks wired.
* `backend/app/db/models_ai.py`
* `backend/app/api/routes/ai.py`: presets, model discovery, version history, limit field.
* `backend/app/api/routes/learning.py`: `/api/learning/explain`, `/api/learning/research-summary`; the signal detail includes the analysis id.
* `backend/app/orchestration/agents.py`: agent kinds and references.
* `backend/tests/test_orchestration.py`, `backend/tests/test_ai_gateway.py`: two Phase 9 assertions updated to the new facts (section 4).
* `frontend/app/(console)/ai-providers/page.tsx` (AI Studio), `frontend/components/{Shell,Intel,Fusion}.tsx`, `frontend/app/(console)/{learning,signals}/page.tsx`.

**Preserved:**
* all 215 strategies;
* the gateway's schema validation, boundaries, redaction, retries, fallback and cooldown (unchanged);
* every original test.

## 6. Known limitations

* **Costs need prices.** Provider prices change and differ by region, so none is built in. Enter them per provider; otherwise cost is "no price configured".
* **Local models** (Ollama) are free but slower, and follow the JSON schema less reliably. Invalid answers are rejected, never used, and the route falls back.
* **The per-minute limit is per installation** (Redis). If Redis is unreachable the call is allowed, and the provider's own 429 handling still applies.
* **Version history starts with Phase 14.** Earlier ledger rows have no requested model.

## 7. Security implications

* No new secret. Keys stay encrypted with the DPAPI-protected key, write-only, and are never logged or audited.
* The new tasks are read-only. They need `strategy.manage`, are audited, and send only recorded results; credential-like fields are removed before sending.
* No AI-using agent is on the execution path (asserted). LIVE stays locked.

## 8. Rollback

* Revert the Phase 14 commits; `alembic downgrade 0008` drops the two columns.
* Data in the two new columns is lost on downgrade; the rest of the ledger is kept.
* **Without reverting:** remove the routes for the two new tasks, and the buttons report "not configured".

## 9. Next phase

**Phase 15 — trading worker and execution (Part 15)**
* Audit the existing worker, bridge, execution manager, relay and Risk Engine against the Part 15 list:
  * idempotency, journal, HMAC, nonces, retcodes, reconciliation, adoption;
  * stop protection, partial closes, break-even, trailing;
  * daily loss and drawdown limits, emergency stop.
* Verify the initial state (worker stopped, auto trading off, DEMO, LIVE locked) and DEMO execution end-to-end on the install. Never LIVE.
