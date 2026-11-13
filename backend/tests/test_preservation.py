"""Guards the original 215-strategy library: identifiers, categories, parameters and source must stay
byte-identical to the baseline frozen before the Part 25/26 upgrade."""
import json
import os

from app.strategies import registry

BASE = json.load(open(os.path.join(os.path.dirname(__file__), "baseline", "strategy_inventory.json")))


def test_all_original_strategy_ids_present():
    assert set(BASE["strategies"]) <= set(registry.all_strategies())
    assert BASE["count"] == 215


def test_original_strategy_logic_and_config_unchanged():
    now = {m.code: m for m in registry.metas()}
    changed = []
    for code, b in BASE["strategies"].items():
        m = now[code]
        cur = {"name": m.name, "category": m.category, "component": m.component, "params": m.params,
               "timeframes": m.timeframes, "regimes": m.regimes, "requires": m.requires, "risky": m.risky,
               "code_hash": m.code_hash}
        if json.loads(json.dumps(cur, default=str)) != b:
            changed.append(code)
    assert changed == [], f"original strategies modified: {changed}"


def test_experimental_strategies_never_enter_production_registry():
    assert not any(code.startswith("exp_") for code in registry.all_strategies())
