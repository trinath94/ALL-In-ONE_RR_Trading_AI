"""Strategy DNA: static fingerprints (AST-derived) + behavioural similarity (signal overlap).

Report-only: original strategies are never merged, renamed, deleted or replaced.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from itertools import combinations

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_intel import StrategyFingerprint
from app.strategies import registry
from app.strategies.base import StrategyContext

CTX_INDICATORS = {"ema", "sma", "atr", "rsi", "macd", "adx", "bb", "swings", "known_swings", "events", "fvgs",
                  "order_blocks", "htf_bias"}


def static_fingerprint(cls) -> dict:
    try:
        src = inspect.getsource(cls)
        tree = ast.parse(src.lstrip() if not src.startswith("class") else src)
    except (OSError, TypeError, SyntaxError):
        tree, src = None, ""
    calls, attrs, helpers = set(), set(), set()
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute):
                    if isinstance(f.value, ast.Name) and f.value.id == "ctx" and f.attr in CTX_INDICATORS:
                        calls.add(f.attr)
                    elif isinstance(f.value, ast.Name) and f.value.id in ("ind", "st", "ss"):
                        helpers.add(f"{f.value.id}.{f.attr}")
                elif isinstance(f, ast.Name):
                    helpers.add(f.id)
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                attrs.add(node.slice.value)
    m = cls.meta()
    feat = {"category": m.category, "component": m.component, "ctx_indicators": sorted(calls),
            "helpers": sorted(h for h in helpers if not h[0].isupper())[:40], "params": sorted(m.params),
            "timeframes": sorted(m.timeframes), "regimes": sorted(m.regimes), "requires": sorted(m.requires),
            "risky": m.risky}
    sig = hashlib.sha256(json.dumps({k: feat[k] for k in ("ctx_indicators", "helpers", "params")}, sort_keys=True).encode()).hexdigest()
    return {"features": feat, "signature": sig, "code_hash": m.code_hash}


def refresh(db: Session) -> dict:
    n = 0
    for code, cls in registry.all_strategies().items():
        fp = static_fingerprint(cls)
        if db.scalar(select(StrategyFingerprint).where(StrategyFingerprint.strategy_code == code,
                                                       StrategyFingerprint.code_hash == fp["code_hash"])):
            continue
        db.add(StrategyFingerprint(strategy_code=code, code_hash=fp["code_hash"], static=fp["features"], signature=fp["signature"]))
        n += 1
    db.flush()
    return {"new_fingerprints": n}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def static_similarity(fps: dict[str, dict]) -> list[dict]:
    out = []
    for a, b in combinations(sorted(fps), 2):
        fa, fb = fps[a]["features"], fps[b]["features"]
        if fps[a]["signature"] == fps[b]["signature"] and fa["category"] == fb["category"]:
            out.append({"a": a, "b": b, "kind": "near_duplicate_logic", "similarity": 1.0})
            continue
        s = 0.5 * _jaccard(set(fa["ctx_indicators"]) | set(fa["helpers"]), set(fb["ctx_indicators"]) | set(fb["helpers"])) + \
            0.3 * _jaccard(set(fa["params"]), set(fb["params"])) + 0.2 * (fa["component"] == fb["component"])
        if s >= 0.85:
            out.append({"a": a, "b": b, "kind": "similar_construction", "similarity": round(s, 3)})
    return out


def behavioural_similarity(df: pd.DataFrame, codes: list[str] | None = None, window: int = 300, step: int = 3,
                           min_signals: int = 5) -> dict:
    """Fire-bar Jaccard & direction agreement on a common dataset — reveals strategies that add no new
    information (highly overlapping signals) even when their code looks different."""
    reg = registry.all_strategies()
    codes = codes or list(reg)
    fires: dict[str, dict[int, str]] = {c: {} for c in codes}
    inst = {c: reg[c]() for c in codes}
    for i in range(window, len(df), step):
        ctx = StrategyContext(df.iloc[i - window:i + 1])
        for c, s in inst.items():
            try:
                sig = s.evaluate(ctx)
            except Exception:  # noqa: BLE001
                sig = None
            if sig:
                fires[c][i] = sig.direction
    active = {c: f for c, f in fires.items() if len(f) >= min_signals}
    pairs = []
    for a, b in combinations(sorted(active), 2):
        A, B = set(active[a]), set(active[b])
        j = _jaccard(A, B)
        if j < 0.5:
            continue
        common = A & B
        agree = np.mean([active[a][i] == active[b][i] for i in common]) if common else 0.0
        pairs.append({"a": a, "b": b, "fire_jaccard": round(j, 3), "direction_agreement": round(float(agree), 3),
                      "kind": "highly_correlated_signals" if agree > 0.8 else "overlapping_opposed"})
    return {"bars": len(df), "strategies_with_signals": len(active), "pairs": sorted(pairs, key=lambda p: -p["fire_jaccard"]),
            "note": "Report only. No strategy is merged, renamed or removed."}


def duplicate_report(db: Session, df: pd.DataFrame | None = None) -> dict:
    fps = {fp.strategy_code: {"features": fp.static, "signature": fp.signature}
           for fp in db.scalars(select(StrategyFingerprint))}
    rep = {"static": static_similarity(fps), "fingerprints": len(fps)}
    if df is not None:
        rep["behavioural"] = behavioural_similarity(df)
    return rep


def proposal_overlap(proposal_text: str, db: Session, top: int = 5) -> list[dict]:
    """Rough check of a research proposal against existing strategy names/descriptions (token Jaccard)."""
    toks = lambda s: {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in s).split() if len(w) > 3}  # noqa: E731
    p = toks(proposal_text)
    scored = []
    for m in registry.metas():
        s = _jaccard(p, toks(f"{m.name} {m.description} {' '.join(m.params)}"))
        if s > 0.15:
            scored.append({"strategy": m.code, "name": m.name, "overlap": round(s, 3)})
    return sorted(scored, key=lambda x: -x["overlap"])[:top]
