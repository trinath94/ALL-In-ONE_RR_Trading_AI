# Phase report — RR Trading Engine (session of 2026-11-11)

## Phases completed (code + automated verification)

| Phase | Status |
|---|---|
| 1 Inspection & architecture | Done. The repository was empty, so there were no original strategies to preserve. See `ARCHITECTURE.md`. |
| 2 Backend & database | Done: FastAPI, PostgreSQL (Alembic, 39 tables incl. config), Redis, auth, RBAC, WebSocket, structured JSON logs. |
| 3 Windows MT5 connector | Code done and tested against an MT5 API emulation. **Not yet run on Windows with a real terminal.** |
| 4 Market scanner | Done: dynamic symbol discovery, multi-TF, regime, spread/volatility/session/news, parallel. |
| 5 Strategy engine | Done: 215 strategies with detection logic; 15 honestly unavailable on MT5 data. |
| 6 Claude agent | Done in code (structured output, adaptive thinking, refusal fallbacks, boundaries, throttling). **Not called live: no API key in this environment.** |
| 7 Risk engine | Done. |
| 8 Frontend | Done: 18 modules plus login, verified rendering with live API data (desktop + mobile). |
| 9 Backtesting & optimisation | Done: backtest, OOS, walk-forward, Monte Carlo, grid search, spread sensitivity. |
| 10 Automated trading | Done: auto-execution pipeline, DEMO/LIVE separation, controls, reconciliation. |
| 11 Production deployment | Docker stack built and run in this sandbox (TLS via Nginx, healthy). **Not deployed to your Ubuntu host or connected to your Windows server.** |

## Tests executed

```
backend:       100 passed  (pytest; real PostgreSQL 16 + Redis)
mt5-connector:  10 passed
frontend:      next build OK (TypeScript strict), 20 routes
docker:        images built; compose stack healthy; backup -> restore round-trip verified;
               nginx re-resolves recreated containers; wss through nginx verified;
               OOS backtest via HTTPS API completed and recorded strategy_performance
```

Highlights of what the tests prove:
* **No duplicate execution.** Duplicate signal → 1 broker order. Lost HTTP response → the order is
  reconciled from the journal and not resent. A crash after `order_send` → reconciled from tagged MT5
  deals. The journal survives a process restart.
* **Restart safety.** Positions are adopted from the broker; an unprotected position gets an ATR stop;
  nothing is re-opened; entries stay blocked until checks pass.
* **Risk.** Sizing is exact for EURUSD, XAUUSD and USDJPY (each with its own tick value). Loss and
  drawdown limits lock trading. Correlated exposure is detected via currency legs. Unknown news blocks
  by default. Volume is never rounded up past the risk budget.
* **Backtest realism.** Next-bar fills with spread, SL-first on ambiguous bars, gap fills at the open,
  closed-bar-only HTF data, and causality checks for 16 indicators.
* **Security.** CSRF enforced, RBAC enforced, lockout after 5 failures, HMAC signatures and nonce replay
  protection, TLS pinning, connector IP allow-list.

## Defects found by verification and fixed in this session
1. The backtester ignored exits on the final bar (trades were marked `END` instead of SL/TP).
2. Login with an unknown username returned HTTP 500 (malformed timing-equaliser hash). This also leaked
   username existence.
3. Nginx kept stale upstream IPs after an API container was recreated (502 after restore/upgrade).
4. The `strategies.availability` column was too short for real availability messages.
5. Gold was classified as forex when the broker put it under a "Forex" path.
6. The Windows installer used PowerShell-7-only syntax (Windows Server ships 5.1) and passed the service
   password on the command line.

## Acceptance criteria status (honest)

Legend: ✅ verified here · 🟡 implemented, verified only against emulation / without external dependency · ⛔ requires your environment

| # | Criterion | Status |
|---|---|---|
| 1 | Ubuntu 24.04 deployment | 🟡 Compose stack verified in a Linux container host; `install.sh` not run on a fresh 24.04 machine |
| 2–6 | Web app, login, permissions, PostgreSQL, Redis | ✅ |
| 7 | Claude agent connects | ⛔ needs `RR_ANTHROPIC_API_KEY`; request shape and boundaries tested with a stubbed client |
| 8 | Broker symbols discovered dynamically | 🟡 verified against the connector + MT5 emulation |
| 9–12, 14–15 | MTF analysis, registry, detection logic, combinations, explanations, NO TRADE | ✅ |
| 13 | Claude evaluates shortlisted opportunities | 🟡 pipeline tested; live call pending API key |
| 16 | Strategy performance recorded | ✅ (OOS / walk-forward from backtests; live outcomes from closed trades) |
| 17–22 | Risk validation, sizing, SL/TP, drawdown, duplicates, kill switch | ✅ |
| 23 | MT5 Windows connector connects | ⛔ code + PowerShell ready; needs the Windows server and terminal |
| 24–25 | Demo / Live account verified | ⛔ needs real broker accounts |
| 26–29 | Auto ON/OFF, safe mode switching, execution sync, restart reconciliation | ✅ (sync and reconciliation against emulation) |
| 30–38 | Dashboard modules and mobile | ✅ rendered with real API data (simulated broker feed) |
| 39–41 | Unit, integration and critical execution tests | ✅ 110 passing |
| 42 | Backup & restore tested | ✅ |
| 43 | No duplicate execution after restart | ✅ |
| 44 | No critical security issues | 🟡 no known critical issues in our code; no third-party penetration test. `npm audit` reports a PostCSS advisory bundled inside Next 15 (build-time CSS processing of our own stylesheets only; the fix requires a Next 16 major upgrade) |
| 45 | Documentation | ✅ |

## Outstanding issues and next steps
1. **Install the Windows connector** on the MT5 server (`mt5-connector/windows/install.ps1`) and point
   `RR_BRIDGE_URL` at it. Validate that the service account's session can drive the terminal (see the
   note in `INSTALL.md`).
2. **Add the Claude API key** and observe a few scan cycles in DEMO (token usage appears on the AI
   Intelligence page).
3. **Connect an economic-calendar provider.** Until then news risk is UNKNOWN and DEMO/LIVE entries are
   blocked unless the risk policy is changed explicitly.
4. **Validate on DEMO first.** Run walk-forward tests per strategy and symbol on broker history, keep
   only strategies with stable OOS results, and let calibration accumulate at least 30 outcomes before
   confidence is displayed.
5. Correlation inputs: the Risk Engine supports an explicit correlation matrix (`corr`). A periodic job
   to compute it from broker returns is not yet scheduled; the currency-leg overlap rule is used meanwhile.
6. The frontend uses Google Fonts at runtime; self-host the fonts if the server has no internet egress.
7. Plan the Next.js 16 upgrade to clear the bundled PostCSS advisory.

No trading performance is claimed or implied: all results shown in this session come from simulated
prices and exist only to exercise the software.
