# Part 25 + 26 — Upgrade, verification & rollback (Ubuntu 24.04)

## What the upgrade changes
* **Database:** migration `0002_part25_26`, additive only.
  * 19 new tables.
  * Nullable columns on `closed_positions` (`mode`, `partial_exits`, `sync_status`) and on
    `strategy_experiments` (`code`, `category`, `version`, `source_code`, `source_hash`, `validation`,
    `duplicate_of`).
  * One new FK, `strategies.current_version_id`.
  * A reversible mapping of legacy experiment stage names.
  * No table is dropped, renamed or retyped.
* **New service:** `learner` (`python -m app.learning_engine.scheduler`). It runs the learning and
  intelligence jobs and never places orders.
* **New settings (defaults preserve existing behaviour):**
  * `RR_CONSENSUS_MODE=advisory`
  * `RR_MAX_QUOTE_AGE_SECONDS=120`
  * `RR_REGIME_INTEL_ENABLED=true`
  * `RR_LEARNER_INTERVAL_SECONDS=60`
* **New Python dependencies:** scikit-learn, scipy, lightgbm, xgboost.

Behaviour changes that only make trading **more restrictive** (safety, enforced by default):
* The Risk Engine refuses entries on a symbol with an unresolved **critical** market/feed anomaly
  (flash move, abnormal spread, stale price, unexpected jump). Existing positions and stops are untouched.
* Entries are refused when the live quote is older than `RR_MAX_QUOTE_AGE_SECONDS`.
* A stop-loss can no longer be moved further away from the entry (widening risk). Tightening is allowed,
  and every adjustment is logged.
* The Risk Engine's existing correlated-exposure check now receives a rolling, regime-dependent
  correlation matrix (`learner` job `portfolio`). Before, only currency-leg overlap was used.
* The spread is rounded to 0.1 point before the max-spread check. This **fixes** spurious rejections
  at exactly the limit, and is the only change that can let through an order the old code (incorrectly)
  rejected.

## Upgrade

```bash
cd /opt/rr-trading-engine
```

```bash
./scripts/backup.sh
```

```bash
git fetch origin && git checkout feature/part25-26-ai-intelligence
```

```bash
docker compose build
```

```bash
docker compose run --rm migrate
```

```bash
docker compose up -d api worker learner frontend nginx
```

```bash
./scripts/healthcheck.sh
```

`healthcheck.sh` checks the services it already knew about. Verify the learner separately (below).

## Verification

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"
```

Expect `0002`.

```bash
docker compose ps learner
```

```bash
docker compose logs --tail=50 learner
```

```bash
docker compose run --rm api python -m pytest -q
```

This needs the test database. In CI, run the suite against a disposable PostgreSQL instance.

In the UI, check **AI Learning & Evolution**:
* trades analysed (after the first `trade_sync` run);
* the job history;
* the experiment lifecycle (the four reference experiments appear after the learner's first start).

In **Predictive Intelligence**, the regime and consensus tabs fill after the next scan.

## Rollback

The code and the migration roll back independently. Learning data written by 0002 is discarded on a
schema rollback, so take a backup first if you want to keep it.

```bash
docker compose stop learner
```

```bash
./scripts/backup.sh
```

```bash
docker compose run --rm api alembic downgrade 0001
```

```bash
git checkout main
```

```bash
docker compose build && docker compose up -d api worker frontend nginx
```

The downgrade:
* removes only the objects 0002 added;
* maps experiment stage names back to the legacy names;
* leaves all original tables and their rows intact.

This was verified by an upgrade → downgrade → upgrade cycle on a populated database.

## Operating notes
* **Probability models** are trained weekly from broker history. A run ends `VALIDATED` only if, on an
  untouched final test set, both the Brier improvement over the base rate and AUC − 0.5 have positive
  95% bootstrap lower bounds, and ECE < 0.1. Otherwise it ends `REJECTED`.
  * In this session, models trained on simulated (random) data were correctly rejected.
  * A `VALIDATED` model runs in **shadow**. It influences decisions only after an administrator approves
    it and then promotes it to `PRODUCTION`. Rollback is one click.
* **Consensus** runs in `advisory` mode until you choose `RR_CONSENSUS_MODE=enforce`. Enforce mode can
  only downgrade a BUY/SELL to WAIT. Specialist weights stay in "unvalidated prior" mode (independent
  evidence families, no invented weights) until ≥ 200 outcome-linked decisions produce weights with
  significant out-of-sample value and an administrator approves them.
* **Experimental strategies** execute only inside the sandbox subprocess. "PRODUCTION" on an experiment
  records the approval for a developer to port it into a code-reviewed registry module. The production
  registry never loads experimental code, and `tests/test_preservation.py` enforces this.
* **Claude** research and code generation need `RR_ANTHROPIC_API_KEY`. Without it those jobs report
  `SKIPPED` and nothing is simulated.
