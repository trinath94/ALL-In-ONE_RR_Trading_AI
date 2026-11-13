# RR Trading One — Phase 6 Report

**Phase:** 6 — dynamic broker symbol discovery
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

The engine now keeps a live catalogue of everything the connected broker offers.

| Feature | Behaviour |
|---|---|
| Discovery | Automatic, once the account passes its startup checks; refreshed every 6 hours |
| New symbols | **Not scanned until the user enables them** (Phase 1 gap 7.5 closed for the local stack) |
| Symbols the broker stops offering | Kept and flagged rather than deleted |
| Scan scope | All eligible, by asset class, explicit symbols, exclusions, priorities. Each choice changes exactly what the analysis service scans (verified). |
| Console | A new **Broker Symbols** page |

| Result | Value |
|---|---|
| Phase 6 end-to-end from a **fresh install** (real stack, real headless connector with FakeMT5) | **20 / 20 passed** |
| Upgrade path (Phase 5 data → migration 0005) | Passed in the first run, with a pre-migration backup |
| Backend suite, including the 215-strategy preservation test | **203 passed** (194 + 9 new) |
| MT5 connector / Windows connector / desktop core | **17 / 36 / 88 passed** |
| Frontend `next build` (strict TS) | Passed, **25 pages** (new `/symbols`) |
| Broker Symbols page, logged in through the gateway with live data | Rendered; no console errors |
| CI for the Phase 6 commit (`4b99230`) | backend 43457008514, windows-connector 43457008538, frontend 43457008688 (first run): all passed |

## 2. What was built

### 2.1 Catalogue behaviour (`backend/app/services/trading.py`, `models.py`, migration `0005`)

**New symbols**
* They take their scan flag from `RR_NEW_SYMBOLS_SCAN_ENABLED`.
* The default is `true`, the **original** behaviour, so every original test passes unchanged. The local stack sets `false`.
* The flag is applied **only on insert**: rediscovery never overrides a user's choice (tested).

**Tracking**
* Every discovery sets `last_seen_at`.
* Symbols the broker no longer returns get `missing_since` and become not tradable. They are **never deleted**, since history references them.
* When the broker offers them again, the flag clears automatically.
* Discovery returns `{discovered, new, missing}`.

**Exclusion:** `symbols.excluded` keeps a symbol from ever being scan-enabled until the exclusion is removed.

**Migration `0005`:** additive. It adds `excluded` (not null, default false), `last_seen_at` and `missing_since`; the
downgrade drops exactly these.

### 2.2 Automatic discovery (monitor role)

* After the active account's startup checks pass, the monitor refreshes that broker connection's catalogue.
* It refreshes again every `RR_SYMBOL_REFRESH_HOURS` (default 6), marked by a Redis key with a TTL.
* Connector errors are logged and never break the monitor cycle.

### 2.3 API (`backend/app/api/routes/intel.py`, additive)

**`GET /api/symbols` adds catalogue fields**
* trade mode (FULL / CLOSE_ONLY / …), tick size and value, point, maximum volume, stops level, currencies;
* current spread (points, floating flag), last seen / no longer offered, excluded, and **eligible**;
* `sessions`: the literal text "not provided by the MT5 terminal API". The MetaTrader5 Python package exposes no session schedule, so nothing is guessed.

**`POST /api/symbols/scan-scope`** `{enable, scope: all_eligible|all|categories|symbols, …}`
* Enabling only touches **eligible** symbols: fully tradable, still offered, not excluded. It reports the skipped ones by reason.
* `all` can only disable.
* Requires `strategy.manage`, the same as the original per-symbol patch. Audited.

**`PATCH /api/symbols/{id}`** gains `excluded`. Excluding turns scanning off, and enabling an excluded symbol is refused with 409.

**`POST /api/symbols/discover`**
* **Fixed:** a connector error now returns **502** "MT5 connector unavailable: …" instead of an internal 500 (pre-existing issue noted in Phase 4).
* It returns `new` and `missing` counts.

### 2.4 Console page (`frontend/app/(console)/symbols/page.tsx`, new; navigation entry "Broker Symbols")

It is built with the existing UI components, inside the existing console:
* counts: discovered, scanning, eligible, excluded, no longer offered;
* scan-scope actions: all eligible, stop all, by asset class, filtered set;
* per-symbol scan toggle, priority and exclude, shown only to users with `strategy.manage`;
* spread, lot min/step/max, tick size/value, contract, last seen or "not offered since";
* "Discover now" for administrators;
* the page states that new symbols are not scanned until enabled and that sessions are not provided.

### 2.5 CI

New `.github/workflows/frontend.yml`. It runs `npm ci`, then a strict `next build`, then a guard that fails if Google
Fonts references reappear or the bundled fonts go missing. This closes the Phase 5 gap "frontend has no CI".

## 3. End-to-end verification (final run from a fresh install)

**Fresh install**
* Start All, schema at `0005` from scratch.
* Phase 5 setup: administrator, profile, link, connector, DEMO account.

**Automatic discovery**
* The monitor discovered the catalogue automatically after the startup checks.
* **Both new symbols were scan-disabled.**
* Specs were recorded: tick size and value, contract, volume limits, last seen.
* **The analysis service scanned nothing for 70 s**, because no symbol was enabled.

**Scope controls, as the administrator through the gateway**
* The API reports sessions honestly and shows eligibility.
* **Enabling the "forex" class enabled only EURUSD.r, and the analysis service then scanned only EURUSD.r.**
* With XAUUSD.r excluded, "enable all eligible" skipped it; enabling it directly returned 409.
* Scope changes were audited.

**Broker stops offering a symbol**
* With the connector stopped, discovery returned a clean **502**.
* After restarting the connector against a broker without XAUUSD.r, discovery reported `missing: 1`. The symbol was kept, flagged, and made not tradable, and "enable all eligible" never enabled it.
* When the broker offered it again, the flag cleared and it was tradable again.

**Console:** logged in as the administrator, the Broker Symbols page rendered the live catalogue, with the sessions
note and scope controls, and no console errors.

## 4. Files

**Created**
* `backend/migrations/versions/0005_symbol_catalogue.py`
* `backend/tests/test_symbol_catalogue.py`
* `frontend/app/(console)/symbols/page.tsx`
* `.github/workflows/frontend.yml`
* this report

**Modified**
* `backend/app/core/config.py`: `new_symbols_scan_enabled`, `symbol_refresh_hours`
* `backend/app/db/models.py`: three nullable/defaulted columns on `Symbol`
* `backend/app/services/trading.py`: `discover_symbols`, new/missing tracking. The original behaviour is preserved by default.
* `backend/app/api/routes/intel.py`: extra fields, scan scope, exclude, 502
* `backend/app/workers/roles.py`: monitor catalogue refresh
* `frontend/components/Shell.tsx`: navigation entry
* `infrastructure/compose.yml`: `RR_NEW_SYMBOLS_SCAN_ENABLED=false`
* `windows-connector/tests/fake_connector_entry.py`: test-only `FAKE_MT5_DROP`

**Preserved:** all 215 strategies (preservation test passes) and every original test, unmodified.

## 5. Findings

| # | Finding | Outcome |
|---|---|---|
| 1 | The spec's "new symbols scan-disabled" conflicted with original tests that rely on new symbols being scanned | Made configurable: the original default is kept, and the local stack is disabled. Both behaviours are tested. |
| 2 | Pre-existing: discovery returned a generic 500 when the connector failed | Now a clear 502 (tested in unit and end-to-end) |
| 3 | A check in my e2e script had broken SQL quoting (first run, 18/19) | Script fixed; the final run from a fresh install passed 20/20 |
| 4 | Header connector dots looked grey in a screenshot | Not a defect: they render grey until the first status poll returns; `/api/system/status` reported the connector `UP`. |
| 5 | The frontend patch image is layered on the previous patch image | Each build runs from its own fresh directory, so no stale file can be served (sandbox-only; the committed Dockerfile is unchanged). |

## 6. Not verified here / known limitations

* **Trading sessions** are not available from the MetaTrader5 Python API and are shown as "not provided". Deriving them from observed tick activity would be an estimate; if added later, it must be labelled as such.
* **Asset classes** come from the existing heuristic (symbol path, currencies). Brokers with unusual symbol paths may land in "other". Users can still scope by explicit symbols.
* **Real broker catalogues** (hundreds of symbols) have not been exercised: FakeMT5 offers two. Discovery is one bulk call plus per-symbol upserts; performance at scale belongs to Phase 8 (scanner scheduling).
* **No real Windows or MetaTrader 5** (unchanged).

## 7. Security implications

* The scan-scope and exclude changes require `strategy.manage`, the same as the original per-symbol control. They are audited and refused for read-only users and traders (tested).
* Nothing here can trade. Enabling a symbol only allows the scanner to analyse it; execution still needs the trading worker, auto-trading, a Claude-confirmed signal and the Risk Engine.

## 8. Rollback

* Revert the Phase 6 commits. `alembic downgrade 0004` drops only the three new columns.
* **Without reverting:** set `RR_NEW_SYMBOLS_SCAN_ENABLED=true` to restore the original scan-by-default behaviour.

## 9. Next phase

**Phase 7 — original strategy library integration**
* a Strategy Library view of all 215 baseline strategies with their real registry metadata (data requirements, timeframes, regimes, risk class, availability on the connected broker);
* explicit "unavailable" and "risky" labelling;
* no dummy statistics.
