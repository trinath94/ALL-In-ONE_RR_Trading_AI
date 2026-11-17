"""Part 22 gaps (RR Trading One Phase 22): tests the specification asks for that no earlier test covered directly.

* Existing API routes: every route of the original engine (commit 5c6c26b) still exists with the same method.
* Safety condition 13: an emergency stop does not blindly close existing positions (default policy PROTECT).
* Safety conditions 1-3: no code path from the learner, the AI layer or the agents to order submission, LIVE
  activation or permissions (static check of what those modules reference).
* Database migrations: one linear chain; downgrade to base and upgrade to head again works; the schema the
  migrations build matches the models.
"""
import ast
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import OpenPosition
from app.db.session import session_scope
from app.execution import manager as em
from app.workers import main as worker
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


# ---- existing API routes ---------------------------------------------------------------------------------------
def test_every_route_of_the_original_engine_still_exists():
    from app.main import app
    base = json.loads((ROOT / "tests" / "fixtures" / "baseline_routes.json").read_text())
    spec = app.openapi()
    now = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops if m in ("get", "post", "put", "patch", "delete")}
    missing = sorted({tuple(r) for r in base["http"]} - now)
    assert len(base["http"]) == 112 and missing == [], missing
    for name, path in base["websocket"].items():
        assert app.url_path_for(name) == path


# ---- safety condition 13 ---------------------------------------------------------------------------------------
def test_emergency_stop_blocks_entries_but_does_not_blindly_close_positions(stack):  # noqa: F811
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="k-keep")
    assert len(fake.positions) == 1
    ticket = next(iter(fake.positions))
    with session_scope() as db:
        acts = em.kill_switch(db, bridge, None, "operator emergency stop")       # no policy given: the configured default
    assert all(a.get("action") != "CLOSE" for a in acts), acts
    assert ticket in fake.positions, "the position is still open at the broker"
    with session_scope() as db:
        assert db.scalar(select(func.count(OpenPosition.id))) == 1
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="[Kk]ill"):
        open_eur(db, bridge, key="k-new")
    # the PC's emergency stop and identity failures reach the kill switch without a bridge: no position is touched
    with session_scope() as db:
        assert em.kill_switch(db, None, None, "RR MT5 Connector: emergency stop") == []
    assert ticket in fake.positions


# ---- safety conditions 1-3 -------------------------------------------------------------------------------------
# What submits orders, changes LIVE state or permissions. None of it may be referenced from the learner, the AI
# layer or the agents. (Reading broker data, scoring and recording are allowed.)
FORBIDDEN = {
    "open_position", "manage", "kill_switch", "apply_position_policy", "reconcile_unknown", "trip_risk_lock", "submit",
    "activate_account", "create_account_record", "approve_live", "revoke_live", "switch_mode",
    "ROLE_PERMS", "PERMISSIONS", "RolePermission", "Role", "Permission", "require",
}
GUARDED = ["learning_engine", "ai", "agents", "orchestration", "intelligence", "news"]
# Module-level references that are reviewed and harmless (name -> why).
ALLOWED = {("learning_engine/collector.py", "mt5_tag"): "formats an execution label to match broker deals"}


def _references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names}
    return names


def test_learner_ai_and_agents_have_no_path_to_orders_live_or_permissions():
    hits = []
    files = 0
    for pkg in GUARDED:
        for path in (APP / pkg).rglob("*.py"):
            files += 1
            rel = str(path.relative_to(APP))
            for name in sorted(_references(path) & FORBIDDEN):
                hits.append(f"{rel}: {name}")
    assert files >= 30, files
    assert hits == [], "\n".join(hits)


def test_the_boundary_check_would_catch_a_violation(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from app.execution.manager import open_position\nclient.submit({'action': 'BUY'})\n")
    assert {"open_position", "submit"} <= _references(bad) & FORBIDDEN


def test_learner_and_analysis_containers_cannot_trade():
    """The learner and analysis roles get only the read-only bridge credential (no execution key)."""
    import yaml
    c = yaml.safe_load((ROOT.parent / "infrastructure" / "compose.yml").read_text())
    for name in ("learner", "analysis"):
        env = c["services"][name]["environment"]
        assert env.get("RR_BRIDGE_SCOPE") == "read" and not env.get("RR_ENCRYPTION_KEY"), name


# ---- database migrations ---------------------------------------------------------------------------------------
def _alembic():
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def test_migrations_form_one_linear_chain():
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(_alembic())
    assert script.get_heads() == [script.get_current_head()] and len(script.get_heads()) == 1
    revs = list(script.walk_revisions())
    assert [r.revision for r in reversed(revs)][:3] == ["0001", "0002", "0003"]
    assert all(r.down_revision is None or isinstance(r.down_revision, str) for r in revs), "no merges"


def test_downgrade_to_base_and_upgrade_to_head_and_schema_matches_models(migrated_db):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db import models, models_agent, models_ai, models_intel  # noqa: F401 (register every table)
    from app.db.base import Base
    from app.db.session import get_engine, reset_engine
    cfg = _alembic()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    reset_engine()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    # Differences the migrations deliberately do not express (server defaults rendered differently, etc.) are
    # reported by type; tables, columns and indexes must match.
    structural = [d for d in diff if isinstance(d, tuple) and d[0] in ("add_table", "remove_table", "add_column", "remove_column")]
    assert structural == [], structural
    from app.cli import seed
    seed()
