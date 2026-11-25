# RR Trading One — Claude Code + Anthropic API integration: report

**Date:** 2026-11-25 · **Branch:** `claude/focused-cori-b8em6u` · **Base:** `e658964`

The integration extends the existing engine. It adds no second engine, AI gateway, chat, task system or scheduler:
* Claude Code is a new task kind in the existing `agent_tasks`, run by one new worker role.
* The Anthropic API analyses are new tasks of the existing AI gateway.
* Both are reached from the existing Engine Assistant through a rule-based orchestrator.

The 215 strategies, the MT5 connector, the Risk Engine, execution and the LIVE lock are unchanged.

**Change of decision.** Earlier the same day the operator agent was limited to safe actions, without code editing.
This prompt asks for code editing through the Claude Agent SDK, with an isolated workspace, tests, patch review and
approval gates. That is what is implemented; the safe operator agent remains as the runtime path.

## 1. Phases

| Phase | What was done |
|---|---|
| 1 Audit | The existing Anthropic adapter already uses the official `anthropic` SDK: structured JSON output, adaptive thinking, prompt caching, refusal fallbacks, model from configuration. It was extended, not replaced. Integration points: `ai/gateway.py` (tasks, ledger, budget), `services/assistant.py` (chat), `agent_tasks` (persistence), `api/routes/system.py`, `components/Assistant.tsx`, compose roles, desktop supervisor services |
| 2 Agent SDK | `claude-agent-sdk` 0.2.189 (bundles the Claude Code CLI 2.1.332). Lock change is additive only: no existing pin moved, so numpy, pandas and pydantic are unchanged. `pytest` is in the engine image, because the engine tests every patch. `git` and the simulated-connector sources are in the image for the workspace |
| 3 Claude Code in the backend | `app/ai/claude_code.py` (workspace, tool policy, runner, review, gates, status) and the `claude-code` worker role (`app/workers/claude_code.py`), which is also a compose service and a desktop-supervised service |
| 4 Anthropic API in the provider service | Five gateway tasks (market summary, trade review, performance analysis, strategy research, error explanation), each routable to its own provider and model. Prompt-cache read and write tokens are recorded in the ledger. Budget alert at a configurable threshold, raised as an incident |
| 5 Chat | `services/orchestrator.py` routes every request to Claude Code, the Anthropic API or the engine runtime. Prefixes force a route: `/code`, `/ai`, `/engine`. The chat shows Claude Code tasks live |
| 6 Tasks | Persistent tasks (migration 0022: kind, progress events, session id, branch, cancel request, usage). Progress is stored as it happens and polled by the chat. **Cancel** interrupts the SDK session; **Resume** continues the same session and worktree; a restart marks running tasks *interrupted* (resumable) |
| 7 Secure repair and patches | Isolated git worktree per task. Every tool call is checked by `ToolPolicy`, through a PreToolUse hook and the permission callback. Repository settings files are ignored. The engine runs the tests itself against a dedicated test database. The patch is committed on the task branch with its approval gates. An administrator merges only after the tests pass and each gate is acknowledged. No deployment path |
| 8 Research, performance, learning | The analyses read the engine's records: closed trades, per-symbol results, signals, incidents. The existing learner jobs (weekly strategy research through the gateway, adaptive profiles, post-trade reviews) are unchanged and remain the learning path |
| 9 Cost controls and routing | Deterministic routing, with no AI needed to decide. Per-task models. Prompt caching: system-prompt cache on the API, and the Claude Code harness cache (tracked). Daily/monthly budgets, token limit and alert. Per-task Claude Code budget and turn limit. Opt-in result cache |
| 10 Handover | The existing handover workflow remains. Claude Code is idle unless switched on and has no role in trading |
| 11 DEMO | The workspace test run includes the engine's DEMO execution end-to-end tests against the simulated broker. Verified here. Real broker DEMO validation is your step on the PC (runbook `docs/rr-trading-one/DEMO_VALIDATION.md`) |
| 12 Claude unavailable | Tested: a missing CLI fails the task cleanly and the engine state is unchanged. A missing provider or exhausted budget returns the engine's facts. The trading roles never call Claude Code, and the unavailable-AI policy is unchanged |
| 13 Report | This document, the administrator manual §5.1, the user manual and the release notes |

## 2. Security constraints (prompt §12) and how each is enforced

| Constraint | Enforcement |
|---|---|
| No automatic LIVE, risk increase, stop-loss removal, permission bypass, credential change | Claude Code can only change files in its worktree. Nothing it writes reaches the engine without review. Patches touching LIVE, risk, order execution, credentials or security raise gates that an administrator must acknowledge one by one |
| No unrestricted production shell | Shell allow-list: pytest, ruff, mypy, read-only git, ls/cat/grep/find/head/tail/wc/diff. No pipes, redirection, substitution, chaining, `python -c`, `sudo`, `rm` or network tools. Paths outside the worktree are refused |
| No deletion of production data | The worktree holds no data. Deleted files raise the *destructive* gate. Tests never run against the trading database: production URLs are not passed to them, and the engine refuses when the test database is the trading one |
| No unreviewed migrations | Migration files raise the *database migration* gate. Merging never runs a migration |
| No untested deployment | There is no deployment path. A merge needs a passing engine test run and lands on the workspace's base branch only; deployment is the release pipeline |
| API key never in the frontend | The key is read on the backend from the AI Studio store or the environment, passed only to the Claude Code process, and never returned (tested) |
| No obsolete hard-coded model | The model comes from configuration: the AI Studio per-task route, the Claude Code model setting, or the engine's Claude model setting |
| No unrestricted filesystem | Claude Code's working directory is the task worktree. Reads and writes are confined to it; secrets are refused inside it too; `.git` cannot be written |
| No fabricated backtests, no profitability guarantees | Both system prompts forbid them. Analyses get only engine facts. The engine's own test run decides, not Claude's claim |
| No changes while trading | Development happens on a copy, in its own service, never in the running engine's files |

## 3. Files

**New:**
* `backend/app/ai/claude_code.py`, `backend/app/workers/claude_code.py`, `backend/app/services/orchestrator.py`
* `backend/migrations/versions/0022_claude_code.py`
* `backend/tests/test_claude_code.py` (65 tests)
* `frontend/components/ClaudeCode.tsx`
* this report

**Extended:**
* `ai/gateway.py`: tasks, cache tokens, budget alert
* `ai/providers.py`, `agents/claude_agent.py`: cache tokens
* `services/assistant.py`: routing, task output
* `services/incidents.py`: AI budget category
* `api/routes/system.py`: task approve, cancel and resume for Claude Code; status and config
* `db/models_ai.py`, `db/models_opportunity.py`, `core/config.py`
* `components/Assistant.tsx`, AI Studio page

**Infrastructure:**
* `requirements.txt` and `.lock`
* both backend Dockerfiles: git, workspace directory, connector sources
* `infrastructure/compose.yml`: the `claude-code` service and workspace volume
* `install.dockerignore`, `desktop/scripts/stage_bundle.py`
* desktop core: `Service::ClaudeCode`, started and stopped with the learner
* CI: compose guards include `claude-code`; image check for the SDK, its CLI, git, pytest and sources
* test matrix: safety condition 22

## 4. API

**New endpoints:**
* `GET /api/claude-code/status`
* `PUT /api/claude-code/config` (administrator)
* `POST /api/agent/tasks/{id}/resume` (administrator)

**Extended endpoints:**
* `POST /api/assistant/ask`: routing; a Claude Code request needs an administrator; questions up to 4,000 characters.
* `GET /api/agent/tasks`: `?kind=` filter; tasks now carry progress, branch, session and usage.
* `POST /api/agent/tasks/{id}/approve`: for Claude Code, a merge with `acknowledged_gates`.
* `POST /api/agent/tasks/{id}/cancel`: for Claude Code, interrupt, or reject a patch.

No route was removed.

## 5. Database

Migration 0022 is additive: new columns on `agent_tasks`, `ai_control` and `ai_usage`, and one index.

The round trip was verified on a scratch database: 0021 with an existing task → 0022 (the task is backfilled as kind
`operator`) → 0021 (row kept) → 0022.

**Expected version:** `0022`.

## 6. Testing

| Suite | Result |
|---|---|
| Engine (full) | **573 passed**, 0 failed (before: 508; +65 new) |
| `test_claude_code.py` | 65 passed. Covers: tool policy (13 cases), shell allow-list (26), gates, the isolated run → engine test run → review → merge, gated and failing-test merges refused, cancel and resume of the same session, Claude unavailable, restart recovery, the test database guard, the workspace layout, routing (13), API analysis via the route, the budget alert, API permissions and audit, the real SDK client construction |
| Related existing suites (agent, assistant, gateway, AI studio, AI control, incidents, AI mode) | 87 passed |
| Desktop core | 135 passed, Clippy clean (3 test fixtures updated for the new service, unchanged in intent) |
| Console | builds |
| CI compose guards | all pass locally, including the new `claude-code` checks |
| CI on `2693a63` | all green: backend (suite, image build with the SDK/CLI/git/pytest/sources check, compose guards, test matrix with condition 22), desktop (Windows install/upgrade/uninstall, installer published), frontend, security |

**Real runs** (beyond the scripted SDK messages in the tests):
1. **Real Claude Code sessions.** Run through the Claude Agent SDK and its bundled CLI, with a real model, on a
   scratch workspace. Two sessions, about 0.09 USD in total.
   * *Inspection:* the session started, the policy-checked `ls` ran, and cost and cache tokens were recorded; with no
     change the task closed as DONE.
   * *Repair:* asked to read `.env` and fix a bug. Claude declined the secret (it never appeared in the report or
     progress). The policy blocked a piped command and `python -c`. The edit was made, the engine's own test run
     passed, and the patch waited for review with no gate.
   * The engine state was unchanged after both runs.
2. **A workspace built from this repository** (`backend/` plus the simulated connector), with the dedicated test
   database created automatically. The engine's DEMO execution end-to-end tests and the 215-strategy preservation test
   passed inside the workspace (15 passed).

**Not verified here:**
* The engine image could not be built in this sandbox, because its network does not reach the Debian package mirrors.
  CI builds it and checks git, the SDK and its CLI, pytest and the connector sources inside it.
* No Docker stack run of the `claude-code` service.
* No real broker DEMO session.

### 6.1 Full engine suite

573 passed, 0 failed in 21 min 38 s (local run at `2693a63`). No existing test was changed except the desktop
fixtures named above.

## 7. Installation and configuration

1. **Upgrade.** Install the new installer. Migration 0022 runs automatically; the `claude-code` service starts with the
   learner and stays idle.
2. **Anthropic key.** AI Studio → add the Anthropic provider with your API key. A Claude.ai subscription is not API
   access.
3. **Analyses.** Route the five analysis tasks in AI Studio, each with its own model if you want, or use Single Active
   AI. Set the budget and the alert threshold under Budget.
4. **Switch on Claude Code.** AI Studio → Claude Code → *Switch Claude Code on*. Optionally set:
   * the model (empty = the engine's Claude model);
   * the per-task budget (default 2 USD) and maximum turns (default 40);
   * the test command.
5. **Use it.** Engine Assistant → `/code <what to change>`. Watch the progress, review the patch and gates, then
   approve and merge or reject.
6. **Deploy merged changes.** Through the release pipeline: commit to the repository, CI, new installer. Nothing is
   deployed from the workspace.
7. **Turn it off.** Switch it off in AI Studio, or stop the *Claude Code workspace* service in the desktop app.
   Trading is unaffected.

## 8. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Claude Code through the Agent SDK | Done: official Python Claude Agent SDK, verified with real sessions |
| 2 | Anthropic API through the official SDK | Done: existing `anthropic` SDK adapter, extended |
| 3 | Both reachable from the Engine Assistant | Done |
| 4 | Natural-language requests from the chat | Done (`/code …`, analysis questions, runtime questions) |
| 5 | Claude Code inspects the repository | Done: its own copy, in an isolated worktree |
| 6 | Claude Code repairs and tests authorised changes | Done: the engine tests every patch; an administrator merges |
| 7 | API analyses of trading summaries and strategy results | Done (5 analysis tasks) |
| 8 | Strategy engine intact | Preservation tests pass |
| 9 | MT5 connector intact | No connector code changed; its sources are only copied for the workspace tests |
| 10 | Symbol discovery | Existing (unchanged) |
| 11 | Internal opportunity analysis | Existing (unchanged) |
| 12 | Learning from historical and DEMO results | Existing learner (unchanged) |
| 13 | Candidates validated before production | Existing gates (unchanged) |
| 14 | Routine trading independent of Claude | Yes: no trading role calls Claude Code; With/Without AI and the unavailable-AI policy are unchanged |
| 15 | Continues if Claude is unavailable | Tested |
| 16 | Usage and costs tracked | Gateway ledger (now with cache tokens) and per-task Claude Code usage and cost |
| 17 | Chat shows progress and final reports | Done |
| 18 | Relevant tests pass | §6 |
| 19 | DEMO workflow verified | Simulated-broker DEMO end-to-end tests pass, also inside the workspace. **Real broker DEMO acceptance on your PC is still open** |
| 20 | LIVE needs explicit authorisation | Unchanged: Claude Code cannot change it, and changes touching it raise a gate |

## 9. Open items

* The prompt remainders that were cut off earlier (Master prompt after 12.2; Autonomous prompt §13) have not been
  received.
* Whether LIVE requires a non-negative DEMO result: your decision.
* Real broker DEMO acceptance on your PC.
* The unsigned installer.
* Claude Code merges stay inside the workspace. Bringing a merged change into this repository is a manual step today:
  export the patch from the task and commit it.
