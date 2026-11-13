# RR Trading One — Phase 9 Report

**Phase:** 9 — multi-agent orchestration, with the AI provider gateway (specification Parts 9 and 14)
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

**Orchestration**
* The 21 agents of Part 9 now exist as a closed registry under one orchestrator. Each maps to the existing engine component that does the work: nothing was duplicated and no "bot" was added.
* Every agent run is recorded in a typed contract, which is the audit trail: agent ID, version, task, input references, output schema, output, status, start, completion, duration, provider/model, evidence, errors.
* Only *Trade Execution* and *Trade Management* touch the broker, only in the trading worker and monitor, and only through the Risk Engine and execution manager.

**AI provider gateway**
* Every AI call now goes through one gateway: Anthropic Claude plus any OpenAI-compatible API (Qwen, Ollama and other local servers).
* Calls are routed per task with fallback, timeouts, retries, rate-limit handling and cooldown.
* Output is untrusted: it is validated against a typed schema (extra fields refused).
* **The original Claude restrictions now apply identically to every provider:** no reversal, no upgrade of a non-trade, no sizing, no orders.
* Credential-like fields are removed before sending.
* Keys are encrypted with a new DPAPI-protected stack key and never returned.
* Usage, tokens, cost and model are recorded per call. Cost is recorded only when you set a price; it is never guessed.

| Result | Value |
|---|---|
| Phase 9 end-to-end as an **upgrade of the Phase 8 install** (real stack, connector, fake OpenAI-compatible provider) | **7 / 7** upgrade checks (first run) + **17 / 17** for the rest (final run; see section 3) |
| Backend suite, including the 215-strategy preservation test | **244 passed** (223 + 21 new) |
| Desktop core | **90 passed** (+1: secrets format 4 upgrade); clippy clean |
| Frontend `next build` (strict TS) | Passed, **27 pages** (new: Agent Control Room, AI Providers) |

## 2. What was built

### 2.1 Agents and the run contract

**Registry** (`backend/app/orchestration/agents.py`)
* Each of the 21 agents has an ID, version and kind: 19 deterministic, 1 hybrid (Signal Quality: deterministic score plus AI review of shortlisted candidates), 1 LLM (Strategy Discovery).
* Each also records its implementing component, output schema, which loop runs it, and its authority.
* Tests assert that the registry is closed, that only two agents are on the execution path, and that LLMs are used only for review and research.

**Orchestrator** (`backend/app/orchestration/orchestrator.py`, run log `agent_runs`, migration 0007)
* Each scan cycle gets an ID.
* Timings are measured where the work happens:
  * multi-timeframe data;
  * regime;
  * the 215-strategy evaluation;
  * news;
  * signal scoring;
  * consensus;
  * historical evidence;
  * ranking;
  * the AI review.
* One record is written per agent that ran, plus a master record. **Cycles with nothing due record nothing.**
* The consensus specialists (SMC/ICT, Price Action, Indicators) are evaluated together inside the consensus step, so their records say so ("timing shared") instead of inventing separate timings.

**Standalone runs**
* Broker discovery (monitor), trade management (monitor, only with open positions), every learner job (mapped to AI Learning, Post-Trade Learning, Portfolio Risk, Correlation, Adaptive Optimisation or Strategy Discovery), and on-request strategy discovery.
* Trade execution is recorded **only when an attempt changes something**. Re-checking pending candidates while auto-trading is off is not recorded as a run.

Recording is best effort and isolated: a failure to record never affects the work. Run records are pruned after
`RR_AGENT_RUN_RETENTION_DAYS` (30).

### 2.2 AI provider gateway (`backend/app/ai/`)

| Concern | Behaviour |
|---|---|
| Providers | `anthropic`: the original Claude call (JSON-schema output, prompt caching). `openai_compatible`: `/chat/completions` with JSON mode, the schema stated in the prompt, and the output validated afterwards. |
| Routing | Per task: candidate review, strategy discovery, diagnostics (used today); trade research, learning explanations, research summaries (routable, for later phases). Ordered chain; the next provider is the fallback. |
| Original behaviour | With no route, candidate review and discovery use `RR_ANTHROPIC_API_KEY` and `RR_CLAUDE_MODEL`, as before. With neither, candidates stay UNREVIEWED and are never auto-executed. |
| Validation | A strict typed schema (extra fields refused). Invalid output is never repaired or partly used; the next provider is tried. |
| Boundaries | `enforce_boundaries` (unchanged) is applied to the validated review **whatever the provider**. Discovery output only creates RESEARCH proposals in the isolated sandbox. |
| Redaction | Keys that look like credentials (password, secret, token, API key, credential, auth, login, cookie) are removed at any depth before sending. Each provider receives only its own key. |
| Resilience | Timeout per provider; retry on connection errors and 429 with backoff (Retry-After honoured up to 30 s); longer rate limits, or 3 failures in 5 minutes, put the provider in cooldown and it is skipped. |
| Ledger (`ai_usage`) | Task, provider, **model as reported by the provider** (the model version history), status, tokens, cost (only with a configured price), latency, attempts, fallback, error. |
| Health | Derived from the ledger: healthy, degraded, failing, cooling down, or unknown (never called). |
| Keys | Encrypted with `RR_AI_KEYS_KEY`; write-only through the API; never in responses, logs or audit details. |

The analysis service builds its reviewer from the gateway **each cycle**, so a provider configured in the console takes
effect without a restart. Strategy discovery (`/api/agent/discover`) now goes through the gateway too.

### 2.3 Key storage (desktop and Compose)

* Stack secrets **format 4** adds `ai_keys_key`, a Fernet key protected by DPAPI on Windows. Older files are upgraded in place, adding only the missing key and keeping every existing value (tested in Rust, and verified on the real upgrade).
* Compose gives `RR_AI_KEYS_KEY` **only to `api` and `analysis`**. The analysis service stays read-scoped: it still has no trading encryption key.
* CI asserts both rules, alongside the existing check that the supervisor key goes only to `api`.

### 2.4 Gap 7.13 fixed

The AI review now receives the spread measured at analysis time instead of a constant `0.0`.

### 2.5 Console

**Agent Control Room** (new)
* All 21 agents: kind, where each runs, read-only or execution, runs in the last 24 h, last run.
* Per-agent detail: implementation, authority, recent runs with output and errors.
* Orchestrated cycles, with each agent's units, timing, output and evidence.

**AI Providers** (new)
* Providers: add, enable or disable, encrypted key, prices, connectivity test.
* The model per task, with a fallback chain editor.
* Health, and the usage ledger.
* The page states that a Claude subscription does not include API credits.

**Header:** the AI status now follows the gateway. It previously showed "not configured" unless
`RR_ANTHROPIC_API_KEY` was set.

## 3. End-to-end verification (upgrade of the Phase 8 install)

**Upgrade (first run, 7 / 7)**
* Starting point: schema 0006, format-3 secrets, the Phase 8 data.
* Start All succeeded; the schema migrated to 0007 **with a fresh pre-migration backup**.
* **Secrets were upgraded in place to format 4, keeping the database password** (the engine could still reach its data) and adding the AI keys key.
* The administrator, symbols and Phase 8 analyses were kept.
* The AI keys key was present only in the api and analysis containers; analysis was still read-scoped.

**Gateway inside the real containers, against a fake OpenAI-compatible provider on the engine's outbound network**
* The provider was stored with its key encrypted (never returned or audited).
* A connectivity test ran from the api container, and candidate review was routed to it.
* **The analysis container's own reviewer**, the code path the scan uses:
  * a confirmation passed;
  * **a reversal by the provider became NO_TRADE**;
  * the provider received its own key and **none of the password, API-key or login fields** in the payload.
* Ledger: exactly this run's 3 calls, with tokens, the model as reported (`fake-llm-1-20261113`) and no cost (no price set). Health: healthy.

**Orchestration**
* The connector ran (Start All).
* New bars were simulated (as in Phase 8), and a scan cycle was recorded. It contained the 7 analysis agents with measured timings, and every record had the full contract.
* The control-room API listed 21 agents; the analysis agents had run, the execution agents had not.

**Console:** both pages rendered live data, the key was never in the page, and there were no console errors.

**How the runs were done**
* The first run passed the 7 upgrade checks and the provider set-up checks.
* Its reviewer, redaction and ledger checks failed only because of bugs in my test script (the import path and shell quoting).
* Its timing check failed because sub-millisecond steps were rounded to 0. The recorder now keeps 3 decimals.
* I fixed these and resumed the same upgraded install. The final resumed run passed 17 / 17. The ledger check now counts only that run's calls.

## 4. Findings

| # | Finding | Outcome |
|---|---|---|
| 1 | The AI review received `spread: 0.0` (gap 7.13). | Fixed: the measured spread is passed. |
| 2 | The header showed AI "not configured" whenever `RR_ANTHROPIC_API_KEY` was unset, even with a configured provider. | Follows the gateway (tested). |
| 3 | **Seen thanks to the new outcome recording:** on the simulated feed's flat bars, `quant_cointegration` raises `LinAlgError('Singular matrix')` when a peer is configured. The engine already isolates it as an error for that strategy and analysis. | Not changed: the 215 strategies must stay byte-identical. Watch for it on real data; if it occurs there, handle it outside the strategy class. |
| 4 | Sub-millisecond agent steps were recorded as 0 ms. | 3-decimal precision; the console shows "<1 ms". |
| 5 | The learner's weekly research job (`strategy_discovery/agent.research`) still calls Claude directly with `RR_ANTHROPIC_API_KEY`. | Left as is (the learner holds no AI keys key); noted for Phase 11. |

## 5. Files

**Created**
* `backend/app/ai/{providers,gateway}.py`
* `backend/app/orchestration/{agents,orchestrator}.py`
* `backend/app/api/routes/ai.py`
* `backend/app/db/models_ai.py`
* `backend/migrations/versions/0007_agents_ai_gateway.py`
* `backend/tests/test_ai_gateway.py` (15), `backend/tests/test_orchestration.py` (6)
* `frontend/app/(console)/{agents,ai-providers}/page.tsx`
* this report

**Modified**
* `backend/app/scanner/service.py`: cycle steps, AI review recording, real spread, reviewer message
* `backend/app/workers/roles.py`: monitor and execution records, per-cycle reviewer
* `backend/app/learning_engine/scheduler.py`: job records
* `backend/app/agents/claude_agent.py`: `AgentError.kind`; discovery prompt as a constant. Behaviour unchanged.
* `backend/app/api/routes/system.py`: discovery through the gateway; AI status
* `backend/app/core/{config,security}.py`
* `backend/app/db/models.py`
* `backend/app/main.py`
* `backend/tests/conftest.py`: the new tables are added to the per-test truncate list
* `desktop/core/src/{secrets,supervisor}.rs`
* `infrastructure/compose.yml`
* `.github/workflows/backend.yml`
* `frontend/components/Shell.tsx`

**Preserved:** all 215 strategies and every original test. The Claude system prompt, review schema and
`enforce_boundaries` are unchanged.

## 6. Known limitations

* **No real provider has been called.** There are no API keys in this environment. The Anthropic path is the original, unchanged call (covered by its existing stub tests); the OpenAI-compatible path is tested against a fake. Qwen's and Ollama's real JSON-mode behaviour still needs a first live check; use **Test** on the AI Providers page. Ollama in Docker Desktop is reached at `http://host.docker.internal:11434/v1`.
* **Nothing measures the cost of a call yet.** Prices are whatever you enter.
* **No real AI-reviewed candidate came out of a live scan.** The simulated feed produced no BUY/SELL candidates, so the scan never called the reviewer by itself. That code path was exercised by calling the analysis container's own reviewer directly. The scan → review wiring is the original and is unit-tested.
* **The learner's weekly research job** still uses the environment key directly (finding 5).

## 7. Security implications

* **AI can do less than before, not more.** Every provider is bound by the same schema and boundaries, and output with extra fields (for example a lot size) is refused.
* **Keys:** encrypted at rest with a DPAPI-protected key, held only by two containers, write-only through the API (`system.manage`), and absent from audit details.
* **Credentials:** credential-like fields are removed before any AI call.
* **Agent records** add visibility only; they cannot change what an agent does.
* **Unchanged:** LIVE stays locked, AI cannot enable LIVE, change permissions, run commands or submit orders, and the trading worker stays stopped by default.

## 8. Rollback

* Revert the Phase 9 commits. `alembic downgrade 0006` drops the four new tables.
* Secrets format 4 is backward compatible: the older code ignores nothing it needs, though the file stays at version 4.
* **Without reverting:** remove the AI routes, and candidate review falls back to the original `RR_ANTHROPIC_API_KEY` behaviour.

## 9. Next phase

**Phase 10 — historical data and trade intelligence**
* Historical bar storage and broker trade history from the connector.
* Per-strategy and per-symbol trade intelligence built only from recorded trades.
* Measured correlation for peer suggestions (Phase 8 limitation).
* Wiring the routable "trade research" task to the gateway.
