# DEMO validation runbook (Parts 27–30, Phase 9)

This is the acceptance of RR Trading One on **your** PC with **your DEMO account**. It cannot be done for you from a
development machine: it needs your broker, your MT5 terminal and real market data. Allow at least **3 trading days**.
No step here enables LIVE trading.

## 1. Install

1. Download the latest installer: GitHub → Actions → the newest successful **desktop** run on this branch → artifact `rr-trading-one-windows-unsigned`. Check its SHA-256 against `SHA256SUMS.txt` in the same artifact.
2. Install it over the existing version. Your settings, backups and database are kept; CI verifies this on every build.
3. Open RR Trading One. In the window, run **Start All**. Every service should become healthy.

## 2. Connect the DEMO account

1. **MT5 Manager:** select your DEMO profile and start the MT5 connector. The window must show the account as **DEMO**.
2. **Trading Console → Broker Symbol Discovery:** enable 2–5 symbols you know (for example EURUSD, XAUUSD). New symbols start disabled.
3. **Optional, AI review:** configure a provider in **AI Studio** (an API key with API billing; a chat subscription does not include API credits). For consensus, put two different providers first and set the mode to *consensus*. Without a provider, candidates stay unreviewed and are never auto-executed, unless you choose **Without AI** in the Live Trading Control Center: then the engine's own BUY/SELL signals are auto-executed (DEMO here), the Risk Engine still decides, and the AI review item shows engine-only mode.

## 3. Let it run

* Leave the stack running during market hours. Auto trading stays **off** until you turn it on in the Trading Worker page, for DEMO only.
* Watch these pages:
  * **Opportunity Lifecycle:** opportunities appear as setups are found, each with its decision trace;
  * **Signal Centre:** each signal shows its trace;
  * **Pattern Intelligence:** harmonic and Elliott detections, if your symbols form any;
  * **System Monitor:** data quality per symbol, job backlog, error logs, model governance;
  * **Backup and Recovery** (desktop window): an automatic backup within a day, a restore test within a week.

## 4. Read the result

**Trading Console → DEMO Validation** computes the acceptance evidence from what your installation recorded:

| Item | Passes when |
|---|---|
| Mode | DEMO, and LIVE was never switched on |
| Broker | the DEMO account is verified, MT5 is connected, startup reconciliation passed |
| Scanning | symbols are enabled and were analysed in the window |
| Data quality | every symbol/timeframe scores 70 or more |
| Opportunities | at least one opportunity with a full decision trace |
| AI review | candidates were reviewed without errors (PENDING without a provider) |
| Execution | DEMO orders confirmed, never more than one entry per signal |
| Protection | every open position has a stop |
| DEMO record | at least 20 closed DEMO trades **and a non-negative net result** |
| Operations | no stuck jobs, no error logs or API server errors in the last 15 minutes |
| Backups | checked in the desktop window (the engine cannot see the PC's backups folder) |

* **PENDING** means not enough activity yet. It is never a pass.
* **ATTENTION** means look at the evidence shown.
* The page reads "accepted" only when nothing needs attention and nothing but the backups item is pending.

**Note on the DEMO record.** The LIVE readiness checklist (a separate page) counts closed DEMO trades only. This
validation also flags a net-negative DEMO result. Whether LIVE should *require* a non-negative result is your
decision.

## 5. Send back

* Export the DEMO Validation page (print to PDF) and the System Monitor page.
* Note anything that looked wrong, with the time.

These are the inputs for Phase 10 (LIVE readiness). LIVE stays locked until you decide otherwise.
