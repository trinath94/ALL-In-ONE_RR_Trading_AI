"""Adaptive strategy configuration: versioned parameter profiles for existing strategies.

A profile supplies parameter values for one strategy, optionally only in one market regime, on some symbols and
timeframes. The strategy's source code, registry entry and default parameters are never changed: without an active
profile the scanner uses the existing configuration exactly as before.

Lifecycle::

    PROPOSED -> VALIDATING -> VALIDATED | REJECTED
    VALIDATED -> DEMO_ACTIVE                (an authorised user, or automatically when the policy allows it)
    DEMO_ACTIVE -> LIVE_ACTIVE              (an authorised user only, with DEMO forward evidence)
    any -> RETIRED                          (rollback re-activates the parent version if it was validated)

Anti-overfitting gates (all must pass to become VALIDATED; the numbers are recorded with the evidence):

* out-of-sample sample size (``MIN_OOS_TRADES``, in the profile's regime when it has one);
* out-of-sample expectancy positive and not below the strategy's defaults on the same data;
* out-of-sample profit factor at least ``MIN_PF``;
* drawdown not worse than the defaults' by more than ``MAX_DD_RATIO``;
* stability: in-sample and out-of-sample expectancy agree in sign, out-of-sample keeps ``MIN_OOS_IS_RATIO``;
* walk-forward with the profile's fixed values: combined unseen windows positive;
* cost sensitivity: still positive with doubled spread;
* the bias report (look-ahead / repainting and leakage) passes.

AI proposals are untrusted: only parameters the strategy already has, of the same type, within a bounded range of
the default, are accepted, and a proposal is only ever PROPOSED: it must pass the same validation as any other.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models_opportunity import AutomationPolicy, StrategyConfigProfile
from app.strategies import registry

log = logging.getLogger("profiles")
UTC = timezone.utc

STATUSES = ("PROPOSED", "VALIDATING", "VALIDATED", "REJECTED", "DEMO_ACTIVE", "LIVE_ACTIVE", "RETIRED")
ACTIVE = ("DEMO_ACTIVE", "LIVE_ACTIVE")
REGIMES = ("STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "CONSOLIDATION", "SIDEWAYS")
MIN_OOS_TRADES = 30
MIN_PF = 1.1
MAX_DD_RATIO = 1.2
MIN_OOS_IS_RATIO = 0.5
AI_RANGE = (0.5, 2.0)              # an AI-proposed number must stay within 0.5x..2x of the default
MIN_FORWARD_TRADES_LIVE = 20


class ProfileError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def policy(db: Session) -> AutomationPolicy:
    p = db.get(AutomationPolicy, 1)
    if p is None:
        p = AutomationPolicy(id=1, auto_promote_profiles_demo=False, auto_promote_experiments_demo=False, auto_repair=True)
        db.add(p)
        db.flush()
    return p


# ---- parameters -----------------------------------------------------------------------------------------------------
def check_params(code: str, params: dict, *, bounded: bool = False) -> dict:
    """Only parameters the strategy declares, with the default's type. ``bounded`` (AI proposals): numbers within
    ``AI_RANGE`` times the default, and never a sign change."""
    try:
        defaults = dict(registry.get(code).params)
    except KeyError:
        raise ProfileError(f"unknown strategy {code}") from None
    if not isinstance(params, dict) or not params:
        raise ProfileError("a profile needs at least one parameter")
    out = {}
    for k, v in params.items():
        if k not in defaults:
            raise ProfileError(f"{code} has no parameter {k!r}")
        d = defaults[k]
        if isinstance(d, bool):
            if not isinstance(v, bool):
                raise ProfileError(f"{k} must be true or false")
        elif isinstance(d, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ProfileError(f"{k} must be a number")
            v = int(round(v)) if isinstance(d, int) else float(v)
            if bounded and d != 0:
                lo, hi = sorted((d * AI_RANGE[0], d * AI_RANGE[1]))
                if not lo <= v <= hi:
                    raise ProfileError(f"{k}={v} is outside the allowed range {lo:g}..{hi:g} (default {d})")
            if v <= 0 < d:
                raise ProfileError(f"{k} must stay positive")
        elif type(v) is not type(d):
            raise ProfileError(f"{k} must be a {type(d).__name__}")
        out[k] = v
    return out


def _note(p: StrategyConfigProfile, to: str, user_id: int | None, note: str) -> None:
    p.history = [*(p.history or []), {"at": now().isoformat(), "from": p.status, "to": to, "by": user_id,
                                     "note": note[:500]}]
    p.status = to


def create(db: Session, code: str, params: dict, *, regime: str | None = None, symbols: list | None = None,
           timeframes: list | None = None, source: str = "USER", reason: str = "", ai_provider: str | None = None,
           user_id: int | None = None, parent_id: int | None = None) -> StrategyConfigProfile:
    from app.db.models import Strategy
    if db.scalar(select(Strategy.id).where(Strategy.code == code)) is None:
        raise ProfileError(f"unknown strategy {code}")
    if regime is not None and regime not in REGIMES:
        raise ProfileError(f"regime must be one of {', '.join(REGIMES)}")
    params = check_params(code, params, bounded=(source == "AI"))
    if parent_id is None:
        last = db.scalar(select(StrategyConfigProfile).where(StrategyConfigProfile.strategy_code == code,
                                                             StrategyConfigProfile.regime.is_(None) if regime is None
                                                             else StrategyConfigProfile.regime == regime)
                         .order_by(StrategyConfigProfile.version.desc()).limit(1))
        parent_id = last.id if last else None
    version = (db.scalar(select(func.max(StrategyConfigProfile.version))
                         .where(StrategyConfigProfile.strategy_code == code)) or 0) + 1
    p = StrategyConfigProfile(strategy_code=code, version=version, parent_id=parent_id, params=params, regime=regime,
                              symbols=sorted(set(symbols)) if symbols else None,
                              timeframes=sorted(set(timeframes)) if timeframes else None, status="PROPOSED",
                              source=source, ai_provider=ai_provider, reason=reason[:2000], validation={},
                              history=[{"at": now().isoformat(), "from": None, "to": "PROPOSED", "by": user_id,
                                        "note": f"created ({source})"}], created_by=user_id)
    db.add(p)
    db.flush()
    return p


def label(p: StrategyConfigProfile) -> str:
    return f"{p.strategy_code} v{p.version}"


# ---- validation -----------------------------------------------------------------------------------------------------
def _metrics(trades: list[dict], initial: float = 10_000.0) -> dict:
    from app.backtesting import engine as bt
    curve, bal = [], initial
    for t in trades:
        bal += t["pnl"]
        curve.append((t["exit_time"], bal))
    m = bt.metrics([bt.Trade(**t) for t in trades], curve, initial)
    return {k: m.get(k) for k in ("total_trades", "win_rate", "profit_factor", "expected_value", "max_drawdown_pct",
                                  "net_profit")}


def _side(df: pd.DataFrame, code: str, params: dict, spec: dict, regime: str | None) -> dict:
    from app.backtesting import engine as bt
    from app.strategies.experimental.variants import _cfg
    split = bt.split_oos(df, _cfg(code, params, spec))
    pick = lambda ts: [t for t in ts if regime is None or t.get("regime") == regime]  # noqa: E731
    return {"in_sample": _metrics(pick(split["in_sample"]["trades"])),
            "out_of_sample": _metrics(pick(split["out_of_sample"]["trades"]))}


def validate(db: Session | None, p: StrategyConfigProfile, df: pd.DataFrame, spec: dict) -> dict:
    """Run every gate on ``df`` and record the evidence. VALIDATED only if all pass; otherwise REJECTED."""
    from app.backtesting import engine as bt
    from app.strategies.experimental.variants import _cfg, bias_report
    code = p.strategy_code
    defaults = dict(registry.get(code).params)
    merged = {**defaults, **(p.params or {})}
    if p.status == "PROPOSED":
        _note(p, "VALIDATING", None, "validation started")
    base, prof = _side(df, code, defaults, spec, p.regime), _side(df, code, merged, spec, p.regime)
    oos, ins, b_oos = prof["out_of_sample"], prof["in_sample"], base["out_of_sample"]
    ev, ev_is, b_ev = oos.get("expected_value") or 0.0, ins.get("expected_value") or 0.0, b_oos.get("expected_value") or 0.0
    dd, b_dd = oos.get("max_drawdown_pct") or 0.0, b_oos.get("max_drawdown_pct") or 0.0
    wf = bt.walk_forward(df, _cfg(code, merged, spec), code, {k: [v] for k, v in merged.items()
                                                                if isinstance(v, (int, float)) and not isinstance(v, bool)}
                         or {next(iter(merged)): [merged[next(iter(merged))]]}, n_splits=3, min_trades=1)
    wf_ev = (wf.get("combined_oos") or {}).get("expected_value") or 0.0
    costly = {**spec, "spread_points": float(spec.get("spread_points") or 10.0) * 2}
    cost_oos = _side(df, code, merged, costly, p.regime)["out_of_sample"]
    bias = bias_report(db, df, code, merged, spec)
    gates = {
        "sample_size": {"passed": (oos.get("total_trades") or 0) >= MIN_OOS_TRADES,
                        "detail": f"{oos.get('total_trades') or 0} out-of-sample trades"
                                  f"{' in ' + p.regime if p.regime else ''} (need {MIN_OOS_TRADES})"},
        "expectancy": {"passed": ev > 0 and ev >= b_ev,
                       "detail": f"out-of-sample expectancy {ev:.4g} vs defaults {b_ev:.4g}"},
        "profit_factor": {"passed": (oos.get("profit_factor") or 0) >= MIN_PF,
                          "detail": f"out-of-sample profit factor {oos.get('profit_factor')} (need {MIN_PF})"},
        "drawdown": {"passed": dd <= max(b_dd * MAX_DD_RATIO, b_dd + 0.5),
                     "detail": f"max drawdown {dd:.2f}% vs defaults {b_dd:.2f}%"},
        "stability": {"passed": ev_is > 0 and ev > 0 and ev >= MIN_OOS_IS_RATIO * ev_is,
                      "detail": f"in-sample {ev_is:.4g} -> out-of-sample {ev:.4g} (keep at least {MIN_OOS_IS_RATIO:.0%})"},
        "walk_forward": {"passed": wf_ev > 0 and len(wf.get("windows") or []) >= 2,
                         "detail": f"{len(wf.get('windows') or [])} walk-forward windows, combined unseen expectancy "
                                   f"{wf_ev:.4g}"},
        "cost_sensitivity": {"passed": (cost_oos.get("expected_value") or 0) > 0,
                             "detail": f"with doubled spread: expectancy {cost_oos.get('expected_value')}"},
        "bias": {"passed": bool(bias.get("passed")),
                 "detail": "; ".join(v["detail"] for k, v in bias.items() if isinstance(v, dict) and "detail" in v)[:400]},
    }
    passed = all(g["passed"] for g in gates.values())
    p.validation = {"at": now().isoformat(), "bars": len(df), "symbol": spec.get("symbol"),
                    "timeframe": spec.get("timeframe", "H1"), "regime": p.regime, "passed": passed, "gates": gates,
                    "profile": prof, "defaults": base, "walk_forward": wf.get("combined_oos"),
                    "multiple_testing": bias.get("multiple_testing")}
    _note(p, "VALIDATED" if passed else "REJECTED", None,
          "every gate passed" if passed else "failed: " + ", ".join(k for k, g in gates.items() if not g["passed"]))
    return p.validation


# ---- activation, rollback -------------------------------------------------------------------------------------------
def _same_scope(a: StrategyConfigProfile, b: StrategyConfigProfile) -> bool:
    return (a.strategy_code, a.regime, a.symbols or None, a.timeframes or None) == \
           (b.strategy_code, b.regime, b.symbols or None, b.timeframes or None)


def forward_stats(db: Session, p: StrategyConfigProfile, mode: str = "DEMO") -> dict:
    """Closed trades of signals that used this profile (recorded on the signal's strategy entries)."""
    from app.db.models import ClosedPosition, TradeOrder, TradingSignal
    tag = f"{p.id}:v{p.version}"
    rows = db.execute(select(ClosedPosition.profit, ClosedPosition.commission, ClosedPosition.swap, TradingSignal.strategies)
                      .join(TradeOrder, (TradeOrder.execution_id == ClosedPosition.execution_id)
                            & (TradeOrder.signal_id.isnot(None)))
                      .join(TradingSignal, TradingSignal.id == TradeOrder.signal_id)
                      .where(ClosedPosition.mode == mode)).all()
    nets = [float(a) + float(b or 0) + float(c or 0) for a, b, c, st in rows
            if any((x or {}).get("config_profile") == tag for x in (st or []))]
    return {"trades": len(nets), "net": round(sum(nets), 2),
            "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 3) if nets else None}


def transition(db: Session, p: StrategyConfigProfile, to: str, user_id: int | None, note: str = "") -> StrategyConfigProfile:
    if to not in STATUSES:
        raise ProfileError(f"unknown status {to}")
    cur = p.status
    if to == "DEMO_ACTIVE":
        if cur != "VALIDATED":
            raise ProfileError(f"only a VALIDATED profile can be activated on DEMO (this one is {cur})")
    elif to == "LIVE_ACTIVE":
        if user_id is None:
            raise ProfileError("LIVE activation needs an authorised user; it is never automatic")
        if cur != "DEMO_ACTIVE":
            raise ProfileError("a profile reaches LIVE only after running on DEMO")
        fw = forward_stats(db, p, "DEMO")
        if fw["trades"] < MIN_FORWARD_TRADES_LIVE or fw["net"] <= 0:
            raise ProfileError(f"DEMO forward evidence: {fw['trades']} closed trades, net {fw['net']} "
                               f"(need {MIN_FORWARD_TRADES_LIVE} and a positive result)")
        p.validation = {**(p.validation or {}), "forward_demo": fw}
    elif to == "RETIRED":
        if cur == "RETIRED":
            return p
    else:
        raise ProfileError(f"{to} is set by validation, not by hand")
    if to in ACTIVE:
        for other in db.scalars(select(StrategyConfigProfile).where(StrategyConfigProfile.strategy_code == p.strategy_code,
                                                                    StrategyConfigProfile.status == to,
                                                                    StrategyConfigProfile.id != p.id)):
            if _same_scope(other, p):
                other.retired_at = now()
                _note(other, "RETIRED", user_id, f"replaced by v{p.version}")
        p.activated_at = now()
    if to == "RETIRED":
        p.retired_at = now()
    _note(p, to, user_id, note)
    db.flush()
    return p


def rollback(db: Session, p: StrategyConfigProfile, user_id: int | None) -> StrategyConfigProfile | None:
    """Retire an active profile; if its parent version was validated, it becomes active again at the same level.
    Otherwise the strategy's existing configuration applies. Returns the re-activated parent, if any."""
    if p.status not in ACTIVE:
        raise ProfileError("only an active profile can be rolled back")
    level = p.status
    transition(db, p, "RETIRED", user_id, "rolled back")
    parent = db.get(StrategyConfigProfile, p.parent_id) if p.parent_id else None
    if parent is not None and (parent.validation or {}).get("passed") and parent.status in ("RETIRED", "VALIDATED"):
        parent.retired_at = None
        _note(parent, level, user_id, f"re-activated by rollback of v{p.version}")
        parent.activated_at = now()
        db.flush()
        return parent
    return None


# ---- applying profiles in the scan ------------------------------------------------------------------------------------
def apply(db: Session, enabled: dict[str, dict], mode: str, regime: str | None, symbol: str, timeframe: str
          ) -> tuple[dict[str, dict], dict[str, str]]:
    """The scanner's parameters with the best matching active profile per strategy. DEMO uses DEMO_ACTIVE and
    LIVE_ACTIVE profiles (a DEMO_ACTIVE one under test wins a tie); LIVE uses LIVE_ACTIVE only. Specificity: regime,
    then symbol, then timeframe. Returns (params by strategy, {code: "id:vN"} applied)."""
    statuses = ("DEMO_ACTIVE", "LIVE_ACTIVE") if mode == "DEMO" else ("LIVE_ACTIVE",)
    rows = db.scalars(select(StrategyConfigProfile).where(StrategyConfigProfile.status.in_(statuses),
                                                          StrategyConfigProfile.strategy_code.in_(list(enabled)))).all()
    best: dict[str, tuple] = {}
    for p in rows:
        if p.regime is not None and p.regime != regime:
            continue
        if p.symbols and symbol not in p.symbols:
            continue
        if p.timeframes and timeframe not in p.timeframes:
            continue
        rank = (4 * (p.regime is not None) + 2 * bool(p.symbols) + bool(p.timeframes),
                int(mode == "DEMO" and p.status == "DEMO_ACTIVE"), p.version)
        if p.strategy_code not in best or rank > best[p.strategy_code][0]:
            best[p.strategy_code] = (rank, p)
    out = dict(enabled)
    applied = {}
    for code, (_, p) in best.items():
        out[code] = {**(enabled.get(code) or {}), **(p.params or {})}
        applied[code] = f"{p.id}:v{p.version}"
    return out, applied


# ---- AI proposals -----------------------------------------------------------------------------------------------------
PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["params", "regime", "reason"],
    "properties": {
        "params": {"type": "object", "additionalProperties": {"type": ["number", "integer", "boolean"]}},
        "regime": {"type": ["string", "null"], "enum": [*REGIMES, None]},
        "reason": {"type": "string", "maxLength": 1500},
    },
}
PROPOSAL_SYSTEM = (
    "You review one existing trading strategy's parameters. Propose at most one parameter profile that could suit a "
    "specific market regime better, using only the parameters listed, within 0.5x to 2x of each default. Base it on "
    "the evidence given; if the evidence does not support a change, return the defaults unchanged with a reason. "
    "Your proposal is untrusted: it will be backtested out of sample, walk-forward and cost-stressed before any use. "
    "Return only the JSON object.")


def propose_ai(db: Session, code: str, user_id: int | None, provider_factory=None,
               context: dict | None = None) -> StrategyConfigProfile:
    """An AI configuration proposal for one strategy, from a summarised research package (never raw data). ``context``:
    the engine's own finding that triggered the research (stored with the proposal as its evidence). The proposal is
    only stored: the engine validates it (out-of-sample, walk-forward, costs, overfitting gates) before any use."""
    from pydantic import BaseModel, Field
    from app.ai import gateway
    from app.db.models import Strategy, StrategyPerformance

    class Proposal(BaseModel):
        params: dict[str, float | int | bool]
        regime: str | None = None
        reason: str = Field(max_length=1500)
    if not gateway.configured("config_proposal"):
        raise ProfileError("no AI provider is configured (AI Studio)")
    sid = db.scalar(select(Strategy.id).where(Strategy.code == code))
    if sid is None:
        raise ProfileError(f"unknown strategy {code}")
    perf = [{"regime": r.regime, "source": r.source, "trades": r.trades, "wins": r.wins,
             "profit_factor": r.profit_factor} for r in db.scalars(
        select(StrategyPerformance).where(StrategyPerformance.strategy_id == sid,
                                          StrategyPerformance.source.in_(["OOS", "WALKFORWARD", "PAPER", "LIVE"])))][:40]
    cls = registry.get(code)
    user = {"strategy": code, "defaults": dict(cls.params), "suited_regimes": list(cls.regimes or []),
            "evidence_by_regime": perf, "regimes": list(REGIMES)}
    if context:
        user["observed_weakness"] = context
        user["objective"] = ("Propose parameter values (and optionally a regime filter) that address the observed "
                             "weakness; the engine will backtest, walk-forward and cost-test them before any use.")
    out, meta = gateway.run("config_proposal", PROPOSAL_SYSTEM, user, PROPOSAL_SCHEMA, Proposal, max_tokens=2000,
                            provider_factory=provider_factory)
    if out.regime is not None and out.regime not in REGIMES:
        raise ProfileError(f"AI proposed an unknown regime {out.regime!r}")
    prof = create(db, code, dict(out.params), regime=out.regime, source="AI", reason=out.reason,
                  ai_provider=f"{meta['provider']}/{meta['model']}"[:160], user_id=user_id)
    prof.evidence = {"trigger": "engine" if context else "user", "research_package": user,
                     "tokens": {"input": meta.get("input_tokens"), "output": meta.get("output_tokens")}}
    return prof


# ---- automatic promotion (policy) -------------------------------------------------------------------------------------
def auto_promote(db: Session) -> dict:
    """Only what the administrator's policy allows, and never LIVE: validated profiles to DEMO_ACTIVE; experiments
    that passed validation to the DEMO forward (shadow) test."""
    pol = policy(db)
    out = {"profiles": 0, "experiments": 0}
    if pol.auto_promote_profiles_demo:
        for p in db.scalars(select(StrategyConfigProfile).where(StrategyConfigProfile.status == "VALIDATED")):
            transition(db, p, "DEMO_ACTIVE", None, "automatic: administrator policy (DEMO only)")
            out["profiles"] += 1
    if pol.auto_promote_experiments_demo:
        from app.db.models import StrategyExperiment
        from app.strategies.experimental import lifecycle
        for e in db.scalars(select(StrategyExperiment).where(StrategyExperiment.stage == "VALIDATION")):
            try:
                lifecycle.transition(db, e, "DEMO_TESTING", None, "automatic: administrator policy (DEMO shadow test)")
                out["experiments"] += 1
            except (ValueError, PermissionError) as exc:
                log.info("experiment %s not promoted: %s", e.id, exc)
    return out
