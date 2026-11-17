"""Parts 27-30 Phase 5 (Part 28): pattern catalogue, harmonic and conservative Elliott detectors, detection records
(lifecycle, deduplication, revision), strategy relations, pattern learning statistics, multi-timeframe alignment, the
pattern agent and the API. Synthetic price paths with known geometry; simulated broker for the scan."""
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Symbol
from app.db.models_ai import AgentRun
from app.db.models_opportunity import Opportunity, PatternDetection
from app.db.session import session_scope
from app.patterns import catalogue as cat
from app.patterns import elliott, harmonic
from app.patterns import service as patterns
from app.patterns.pivots import zigzag
from app.scanner.service import scan_once
from app.strategies import registry
from tests.test_api import login, make_user
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)
from tests.test_part27 import scan  # noqa: F401 (fixture)


def path(points, legs=20, seed=0, noise=0.0002):
    rng = np.random.default_rng(seed)
    xs = [points[0]]
    for a, b in zip(points, points[1:]):
        xs += list(np.linspace(a, b, legs + 1)[1:])
    c = np.array(xs) + rng.normal(0, noise, len(xs))
    idx = pd.date_range("2026-01-01", periods=len(c), freq="h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.0003, "low": np.minimum(o, c) - 0.0003, "close": c,
                         "volume": 100.0}, index=idx)


def mirror(points, around=1.05):
    return [2 * around - p for p in points]


X, A = 1.0, 1.1
B = A - 0.618 * (A - X)
C = B + 0.5 * (A - B)
D = A - 0.786 * (A - X)
GARTLEY = [1.05, X, A, B, C, D, D + 0.06]


def found(df, name):
    return [d for d in harmonic.detect(df, zigzag(df)) if d.pattern == name]


# ---- detectors ----------------------------------------------------------------------------------------------------------
def test_bullish_gartley_completes_confirms_and_reaches_its_target():
    (g,) = found(path(GARTLEY), "gartley")
    assert g.direction == "BUY" and g.state == "TARGET_REACHED"
    assert [h["state"] for h in g.history] == ["FORMING", "COMPLETED", "CONFIRMED", "TARGET_REACHED"]
    assert abs(g.ratios["ab_xa"] - 0.618) < 0.02 and abs(g.ratios["ad_xa"] - 0.786) < 0.02
    assert g.levels["invalidation"] < g.points[0]["price"] + 1e-9, "Gartley invalidation is beyond X"
    m = g.measure
    assert m["outcome"] == "TARGET_REACHED" and m["r"] > 0 and m["mfe_r"] > 0 and m["mae_r"] >= 0
    assert 0.5 <= g.quality <= 1.0


def test_bearish_mirror_is_a_sell_and_failure_is_invalidated():
    (g,) = found(path(mirror(GARTLEY)), "gartley")
    assert g.direction == "SELL" and g.state == "TARGET_REACHED"
    through = path([1.05, X, A, B, C, D - 0.05])                         # no D pivot: price runs through the zone
    (g1,) = found(through, "gartley")
    assert g1.state == "INVALIDATED" and g1.measure == {"outcome": "INVALIDATED", "completed": False}
    fail = path([1.05, X, A, B, C, D, D + 0.02, X - 0.03])               # D, a bounce past confirmation, then below X
    (g2,) = found(fail, "gartley")
    assert g2.state == "INVALIDATED" and g2.measure["outcome"] == "INVALIDATED" and g2.measure["r"] > 0
    assert [h["state"] for h in g2.history][-2:] == ["CONFIRMED", "INVALIDATED"], "a false breakout"


def test_butterfly_and_abcd():
    b = A - 0.786 * (A - X)
    c = b + 0.5 * (A - b)
    d = A - 1.27 * (A - X)
    assert found(path([1.05, X, A, b, c, d, d + 0.06]), "butterfly")
    ab = path([1.08, 1.10, 1.05, 1.08, 1.03, 1.07])                     # AB = 0.05, BC = 0.03, CD = 0.05
    (x,) = [d for d in found(ab, "abcd") if d.points[-1]["price"] < 1.04]
    assert x.direction == "BUY" and abs(x.ratios["cd_ab"] - 1.0) < 0.1


def test_detection_is_causal_forming_until_d_is_confirmed():
    df = path(GARTLEY)
    full = found(df, "gartley")[0]
    d_conf = next(p for p in full.points if p["label"] == "D")["confirmed_idx"]
    early = found(df.iloc[:d_conf], "gartley")                          # the bars before D could be known
    assert early and all(d.state in ("FORMING", "INVALIDATED") for d in early)
    assert all("D" not in [p["label"] for p in d.points] for d in early)


def test_elliott_counts_only_rule_satisfying_impulses():
    good = path([1.02, 1.0, 1.05, 1.025, 1.11, 1.09, 1.14, 1.10], seed=1)
    (e,) = elliott.detect(good, zigzag(good))
    assert e.state == "COMPLETED" and e.direction == "SELL", "a completed bullish impulse implies a correction"
    assert all(v for v in e.ratios["rules"].values() if v is not None)
    assert "subjective" in e.rules
    overlap = path([1.02, 1.0, 1.05, 1.025, 1.11, 1.04, 1.14, 1.10], seed=1)   # wave 4 below the end of wave 1
    assert not [d for d in elliott.detect(overlap, zigzag(overlap)) if d.state == "COMPLETED"]
    short3 = path([1.02, 1.0, 1.06, 1.03, 1.07, 1.065, 1.14, 1.10], seed=1)    # wave 3 the shortest
    assert not [d for d in elliott.detect(short3, zigzag(short3)) if d.state == "COMPLETED"]


# ---- catalogue ----------------------------------------------------------------------------------------------------------
def test_catalogue_derives_from_the_215_strategies_without_changing_them():
    c = cat.catalogue()
    originals = [x for x in c if x["source"] == "original_strategy"]
    assert len(originals) == 215 and {x["code"] for x in originals} == set(registry.all_strategies())
    assert len(registry.all_strategies()) == 215, "detectors are not strategies"
    det = [x for x in c if x["source"] == "pattern_detector"]
    assert {x["family"] for x in det} == {"harmonic", "elliott"} and not {x["code"] for x in det} & set(registry.all_strategies())
    fam = {x["code"]: x["family"] for x in originals}
    assert fam["pa_head_shoulders"] == "classical" and fam["pa_engulfing"] == "candlestick"
    assert fam["vol_wyckoff_spring"] == "wyckoff" and fam["smc_fvg"] == "gaps_imbalances" and fam["vol_vsa"] == "vsa"
    assert not next(x for x in originals if x["code"] == "ind_rsi_obos")["is_pattern"]


# ---- records ------------------------------------------------------------------------------------------------------------
def a_symbol():
    with session_scope() as db:
        return db.scalar(select(Symbol).order_by(Symbol.id).limit(1)).id


class Sig:
    def __init__(self, code, d):
        self.strategy, self.direction = code, d


def run_on(df, sigs=()):
    with session_scope() as db:
        sym = db.get(Symbol, a_symbol())
        return patterns.run(db, sym, "H1", df, regime="SIDEWAYS", sessions=["LONDON"], spread_points=12.0,
                            sigs=list(sigs), data_caps=["tick_volume"])


def rows():
    with session_scope() as db:
        return db.scalars(select(PatternDetection).order_by(PatternDetection.id)).all()


def test_records_are_deduplicated_move_forward_and_record_relations(scan):
    df = path(GARTLEY)
    d_conf = next(p for p in found(df, "gartley")[0].points if p["label"] == "D")["confirmed_idx"]
    first = run_on(df.iloc[:d_conf], [Sig("pa_double_bottom", "BUY"), Sig("trend_macd", "SELL")])
    g = [r for r in rows() if r.pattern == "gartley"]
    assert len(g) == 1 and g[0].state == "FORMING" and first["new"] >= 1
    assert g[0].relations["supporting"] == ["pa_double_bottom"] and g[0].relations["contradicting"] == ["trend_macd"]
    n = len(rows())
    again = run_on(df.iloc[:d_conf])
    assert len(rows()) == n and again["new"] == 0, "same bars: no duplicate"
    run_on(df)
    g = [r for r in rows() if r.pattern == "gartley"]
    assert len(g) == 1 and g[0].state == "TARGET_REACHED", "the same record moved forward"
    assert [h["state"] for h in g[0].history] == ["FORMING", "COMPLETED", "CONFIRMED", "TARGET_REACHED"]
    assert g[0].context["regime"] == "SIDEWAYS" and g[0].context["spread_points"] == 12.0
    run_on(df.iloc[:d_conf])                                            # an older view can never reopen it
    assert [r for r in rows() if r.pattern == "gartley"][0].state == "TARGET_REACHED"


def test_an_open_formation_that_disappears_is_closed_not_left_open(scan):
    df = path(GARTLEY)
    d_conf = next(p for p in found(df, "gartley")[0].points if p["label"] == "D")["confirmed_idx"]
    run_on(df.iloc[:d_conf])
    flat = df.iloc[:d_conf].copy()
    flat[["open", "high", "low", "close"]] = 1.05                        # the same bars, revised to no structure
    run_on(flat)
    g = [r for r in rows() if r.pattern == "gartley"][0]
    assert g.state == "INVALIDATED" and "revised" in g.history[-1]["reason"]


def test_learning_statistics_count_outcomes_with_sample_sizes(scan):
    sid = a_symbol()
    now = datetime.now(UTC)
    with session_scope() as db:
        for i, (state, hist) in enumerate([("TARGET_REACHED", ["COMPLETED", "TARGET_REACHED"])] * 3 +
                                          [("INVALIDATED", ["COMPLETED", "CONFIRMED", "INVALIDATED"])]):
            db.add(PatternDetection(key=f"k{i}", symbol_id=sid, timeframe="H1", family="harmonic", pattern="bat",
                                    direction="BUY", state=state, quality=0.8, points=[], ratios={}, levels={},
                                    history=[{"state": h} for h in hist],
                                    measure={"r": 0.01, "mfe_r": 1.0 + i, "mae_r": 0.5, "outcome": state},
                                    relations={}, context={"regime": "SIDEWAYS"}, rules="", detector_version="harmonic-1.0",
                                    known_at=now, found_live=True, created_at=now, updated_at=now))
    with session_scope() as db:
        (s,) = patterns.stats(db)
    assert (s["pattern"], s["target_reached"], s["invalidated"], s["false_breakouts"]) == ("bat", 3, 1, 1)
    assert s["success_rate"] == 0.75 and s["avg_mfe_r"] == 2.5 and s["sufficient"] is False


# ---- the scan, alignment, the agent and the API -------------------------------------------------------------------------
def test_scan_records_the_pattern_agent_and_trace_stages(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        runs = db.scalars(select(AgentRun).where(AgentRun.agent_id == "pattern_recognition")).all()
        assert runs and runs[0].output.get("detections", 0) >= 0 and runs[0].input_refs["units"] == 6
        o = db.scalars(select(Opportunity)).first()
        assert o is not None
        stages = {t["stage"]: t for t in o.trace}
        assert {"patterns", "timeframe_alignment"} <= set(stages)
        al = stages["timeframe_alignment"]["evidence"]
        assert set(al["timeframes"]) >= {"M15", "H1", "H4"} and "basis" in al
        keys = db.scalars(select(PatternDetection.key)).all()
        assert len(keys) == len(set(keys))


def test_pattern_api(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    run_on(path(GARTLEY))
    from app.main import app
    make_user("viewer28", "readonly")
    with TestClient(app) as c:
        assert c.get("/api/patterns/catalogue").status_code == 401
        login(c, "viewer28")
        assert len(c.get("/api/patterns/catalogue").json()) == 215 + len(cat.DETECTORS)
        lst = c.get("/api/patterns/detections", params={"pattern": "gartley"}).json()
        assert lst and lst[0]["state"] == "TARGET_REACHED"
        one = c.get(f"/api/patterns/detections/{lst[0]['id']}").json()
        assert one["history"] and one["points"] and "rules" in one
        assert c.get("/api/patterns/detections/999999").status_code == 404
        assert c.get("/api/patterns/detections", params={"state": "BOGUS"}).status_code == 422
        st = c.get("/api/patterns/stats").json()
        assert st["min_sample"] == 30 and "not trades" in st["basis"]
        sym = c.get("/api/opportunities").json()[0]["symbol"]
        al = c.get("/api/patterns/alignment", params={"symbol": sym}).json()
        assert al["symbol"] == sym and "aligned" in al
        assert c.get("/api/patterns/alignment", params={"symbol": "NOPE"}).status_code == 404
