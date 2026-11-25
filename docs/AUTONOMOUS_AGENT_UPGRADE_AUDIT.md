# RR Trading One — Autonomous Claude Engine Upgrade: audit and decisions

**Date:** 2026-11-25 · **Baseline:** `9c88ae1` (engine 486 tests passing, connectors 17/44, desktop 135)
**Scope received:** sections 1–13 of the prompt; section 13 ends after "Claude API usage and cost summary" and the rest
was not received.

## Decision (owner, 2026-11-25)

Sections 3–4 ask the in-app agent to change the engine's code, run migrations and tests on the installed PC. The owner
chose a **safe operator agent** instead: the Engine Assistant audits the engine with its own checks and carries out a
fixed catalogue of safe, verified operational actions that the user approves in chat; code-level problems become
written **repair proposals** that are implemented and tested in the development pipeline and shipped in the next
installer. The agent never edits code, applies migrations, enables LIVE, changes risk limits or places orders. This
keeps the earlier rule: AI-generated code is never executed directly in production.

## Classification (A works · B incomplete · C disconnected · D defective · E missing)

| § | Requirement | Class | Evidence / plan |
|---|---|---|---|
| 2 | Audit, preserve, reuse | A | this document; nothing rebuilt |
| 3 | Command-driven agent in the existing Engine Assistant | B | the assistant answers questions; **add** command workflows (audit, repair, readiness, accounts, symbols, missing capabilities, DEMO test, LIVE preparation, handover), an execution plan with progress, approval in chat, verification and a completion report |
| 4 | Full engine inspection and repair | B | incidents, monitoring, readiness, DEMO validation, data quality, baseline integrity exist; **add** one audit that runs all of them with evidence and impact; repairs through the existing safe catalogue; code-level findings as repair proposals |
| 5 | Lifecycle readiness analysis | B | the parts exist (readiness checklist, DEMO validation, signal diagnosis); **add** a lifecycle readiness report in the audit |
| 6 | Broker symbol discovery | A | `discover_symbols` reads every symbol from MT5 (category, contract size, tick size/value, volume min/max/step, stop and freeze levels, trade and filling modes, margin currency, raw spec), refreshes automatically, never hardcodes symbols; only `tradable` and enabled symbols are scanned; per-account symbol lists exist. Add a per-account symbol report to the assistant |
| 7 | Internal market analysis | A | 215 strategies, regime, MTF, patterns, SMC/ICT, consensus, fusion, selection, risk-adjusted gates run without AI |
| 8 | Independent runtime without Claude | B | the engine scans, manages and learns without AI; **gap**: in With AI mode, an unavailable or exhausted AI provider stops new executions (candidates unreviewed). **Add** an administrator policy for an unavailable AI: pause (default, current) or continue engine-only; and a runtime status that shows the engine running independently |
| 9 | Claude API cost control | B | usage, tokens and cost ledger, retries with backoff, rate-limit cool-down, provider health exist; **add** a configurable budget (daily and monthly), enforcement before each call (the engine continues when exhausted), request de-duplication and a result cache per task, and a usage summary |
| 10 | Self-monitoring and self-healing | A | incidents, detectors, safe repairs, verification, notifications (previous upgrade); order retries never resubmit (journal reconciliation). Add escalation of unresolved incidents to the agent (diagnosis on request) — exists as "Ask AI to diagnose" |
| 11 | Learning and improvement | A | trade features (MFE/MAE, costs, regime), failure analysis, reviews, loss significance, profiles, experiments with OOS/walk-forward/DEMO gates; LIVE never automatic |
| 12 | Safety and authorisation | A | Risk Engine, per-account limits, LIVE locks (five consents), identity checks block entries; unchanged |
| 13 | Handover | E | **add** the handover report command |
