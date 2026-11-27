"""Multi-symbol, multi-timeframe market scanner (spec Part 6.3 workflow)."""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.claude_agent import AgentError, ClaudeAgent
from app.analysis import regime as rg
from app.analysis import sessions as ss
from app.core.config import get_settings
from app.db.models import (AgentConversation, AgentDecision, AnalysisResult, ClosedPosition, ScannerSettings, Strategy,
                           StrategyConfiguration, StrategyPerformance, Symbol, TradeOrder, TradingSignal)
from app.db.session import session_scope
from app.execution.bridge import BridgeClient
from app.notifications.service import notify
from app.signals.engine import Calibrator, MarketContext, SignalConfig, build_signal
from app.strategies import registry
from app.strategies.base import CAP_PEERS, CAP_REAL_VOLUME, CAP_TICK_VOLUME, StrategyContext
from app.services.events import agent_activity, get_redis, publish
from app.scanner import scheduler as sched
from app.orchestration.agents import SPECIALIST_AGENT
from app.orchestration.orchestrator import Cycle, NullCycle

log = logging.getLogger("scanner")
UTC = timezone.utc
HTF_OF = {"M1": "M15", "M5": "H1", "M15": "H4", "M30": "H4", "H1": "D1", "H4": "D1", "D1": "W1", "W1": "MN1"}
# Timeframes the console may select. W1 is excluded: no original strategy declares it (Phase 1 audit), so with
# timeframe enforcement a W1 scan would run nothing.
SCANNABLE_TFS = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
EVAL_KEY = "rr:strat:eval:{day}"          # per-day hash "<code>|<outcome>" -> count (Phase 8)
EVAL_TTL = 9 * 86400


def scan_timeframes(db: Session) -> list[str]:
    """The console's choice (scanner_settings) or, if never set, RR_SCAN_TIMEFRAMES."""
    row = db.get(ScannerSettings, 1)
    tfs = list(row.timeframes) if row and row.timeframes else get_settings().timeframes
    return [t for t in tfs if t in SCANNABLE_TFS] or ["H1"]


def applicable(enabled: dict[str, dict], tf: str) -> tuple[dict[str, dict], list[str]]:
    """Split the enabled strategies into those declaring this timeframe and those that do not (gap 7.1). The filter
    sits outside the strategy classes, so their code and hashes are unchanged. Disabled by
    RR_SCAN_ENFORCE_TIMEFRAMES=false (original behaviour: every strategy on every timeframe)."""
    if not get_settings().scan_enforce_timeframes:
        return enabled, []
    metas = registry.all_strategies()
    keep = {c: p for c, p in enabled.items() if tf in metas[c].timeframes}
    return keep, sorted(set(enabled) - set(keep))


def peer_frames(db: Session, bridge: BridgeClient, sym: Symbol, tf: str, now: datetime) -> tuple[dict, list[str]]:
    """Bars of the symbol's configured peers (gap 7.2), only peers the same broker still offers. Returns
    ({peer: df}, [problems])."""
    from app.signals.engine import TF_MINUTES
    out, problems = {}, []
    for name in (sym.peers or [])[:3]:
        peer = db.scalar(select(Symbol).where(Symbol.broker_connection_id == sym.broker_connection_id,
                                              Symbol.broker_symbol == name))
        if peer is None or peer.missing_since is not None:
            problems.append(f"{name}: not offered by the broker")
            continue
        try:
            pdf = drop_forming_bar(bars_to_df(bridge.rates(name, tf, get_settings().scan_bars)), TF_MINUTES[tf], now)
        except Exception as exc:  # noqa: BLE001 - a peer failure only makes peer strategies unavailable
            problems.append(f"{name}: {str(exc)[:120]}")
            continue
        if len(pdf):
            out[name] = pdf[["open", "high", "low", "close", "volume"]]
        else:
            problems.append(f"{name}: no bars")
    return out, problems


def peer_correlation(df: pd.DataFrame, peers: dict, window: int = 200) -> dict:
    """Measured correlation of bar-to-bar log returns between the symbol and each peer, on the closed bars both have
    (last `window`). Reported with its sample; None below 30 common bars (Phase 10: suggestions get evidence)."""
    import numpy as np
    out = {}
    base = np.log(df["close"]).diff()
    for name, pdf in peers.items():
        joined = pd.concat([base, np.log(pdf["close"]).diff()], axis=1, join="inner").dropna().iloc[-window:]
        n = len(joined)
        if n < 30:
            out[name] = {"rho": None, "bars": n, "note": "fewer than 30 common bars"}
            continue
        rho = joined.iloc[:, 0].corr(joined.iloc[:, 1])
        out[name] = ({"rho": None, "bars": n, "note": "undefined: no price variation in the window"} if pd.isna(rho)
                     else {"rho": round(float(rho), 3), "bars": n})
    return out


def record_evaluations(diag: list[dict], not_applicable: list[str]) -> dict:
    """Count every strategy outcome of one analysis (signal / no_signal / unavailable / error / not_applicable) in
    Redis per day, for the strategy library. Returns the totals for this analysis."""
    totals = Counter(d["status"] for d in diag)
    totals["not_applicable"] += len(not_applicable)
    try:
        key = EVAL_KEY.format(day=datetime.now(UTC).strftime("%Y%m%d"))
        p = get_redis().pipeline()
        for d in diag:
            p.hincrby(key, f"{d['strategy']}|{d['status']}", 1)
        for code in not_applicable:
            p.hincrby(key, f"{code}|not_applicable", 1)
        p.expire(key, EVAL_TTL)
        p.execute()
    except Exception:  # noqa: BLE001 - statistics never break a scan
        pass
    return dict(totals)


def bars_to_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df["volume"] = df["real_volume"] if "real_volume" in df and df["real_volume"].sum() > 0 else df.get("tick_volume", 0)
    return df[["open", "high", "low", "close", "volume"] + [c for c in ("spread", "tick_volume", "real_volume") if c in df]]


def drop_forming_bar(df: pd.DataFrame, tf_minutes: int, now: datetime) -> pd.DataFrame:
    """MT5 returns the still-forming bar last; strategies must only see closed bars."""
    if len(df) and df.index[-1] + pd.Timedelta(minutes=tf_minutes) > pd.Timestamp(now):
        return df.iloc[:-1]
    return df


def enabled_strategies(db: Session, mode: str) -> dict[str, dict]:
    out = {}
    rows = {s.code: s for s in db.scalars(select(Strategy))}
    cfgs = {c.strategy_id: c for c in db.scalars(select(StrategyConfiguration).where(
        StrategyConfiguration.account_mode == mode, StrategyConfiguration.symbol_id.is_(None)))}
    for code, cls in registry.all_strategies().items():
        s = rows.get(code)
        if s is None or not s.enabled or s.stage != "PRODUCTION":
            continue
        if mode == "LIVE" and cls.risky:
            # Grid / martingale / DCA are blocked for LIVE. Without this, enabling one for DEMO (which sets the shared
            # enabled flag) also put it in the LIVE scan set whenever no LIVE configuration row existed.
            continue
        cfg = cfgs.get(s.id)
        if cfg is not None and not cfg.enabled:
            continue
        out[code] = (cfg.params if cfg else {}) or {}
    return out


def strategy_stats(db: Session) -> dict[str, dict]:
    """OOS / walk-forward / paper / live evidence per strategy (backtest IS results are excluded)."""
    out: dict[str, dict] = {}
    q = select(Strategy.code, StrategyPerformance).join(Strategy, Strategy.id == StrategyPerformance.strategy_id) \
        .where(StrategyPerformance.source.in_(["OOS", "WALKFORWARD", "PAPER", "LIVE"]))
    for code, p in db.execute(q):
        d = out.setdefault(code, {"trades": 0, "wins": 0, "gp": 0.0, "gl": 0.0})
        d["trades"] += p.trades
        d["wins"] += p.wins
        d["gp"] += p.gross_profit
        d["gl"] += p.gross_loss
    for d in out.values():
        d["win_rate"] = d["wins"] / d["trades"] if d["trades"] else None
        d["profit_factor"] = d["gp"] / d["gl"] if d["gl"] else None
    return out


def calibrator(db: Session) -> Calibrator:
    """Fit score -> win probability on realised outcomes of executed signals (demo/paper/live):
    ClosedPosition.execution_id -> TradeOrder.signal_id -> TradingSignal.score."""
    rows = db.execute(select(TradingSignal.score, ClosedPosition.profit)
                      .join(TradeOrder, TradeOrder.signal_id == TradingSignal.id)
                      .join(ClosedPosition, ClosedPosition.execution_id == TradeOrder.execution_id)).all()
    if not rows:
        return Calibrator()
    return Calibrator.fit([float(s) for s, _ in rows], [p > 0 for _, p in rows])


def analyse(db: Session, bridge: BridgeClient, sym: Symbol, tf: str, enabled: dict[str, dict], stats: dict,
            calib: Calibrator, cfg: SignalConfig, cycle: Cycle | None = None) -> dict | None:
    s = get_settings()
    cyc = cycle or NullCycle()
    ref = f"{sym.broker_symbol}:{tf}"
    now = datetime.now(UTC)
    from app.signals.engine import TF_MINUTES
    from app.services import sources
    t_bars = time.perf_counter()
    try:
        raw = bridge.rates(sym.broker_symbol, tf, s.scan_bars)
    except Exception as exc:
        sources.observe("mt5_market_data", False, f"{sym.broker_symbol} {tf}: {str(exc)[:300]}")
        raise
    sources.observe("mt5_market_data", True, latency_ms=(time.perf_counter() - t_bars) * 1000)
    df = drop_forming_bar(bars_to_df(raw), TF_MINUTES[tf], now)
    if len(df) < 120:
        return None
    enabled, not_applicable = applicable(enabled, tf)
    htf_tf = HTF_OF.get(tf)
    htf = {}
    if htf_tf:
        with cyc.step("multi_timeframe", ref):
            h = drop_forming_bar(bars_to_df(bridge.rates(sym.broker_symbol, htf_tf, 300)), TF_MINUTES[htf_tf], now)
            if len(h):
                htf[htf_tf] = h
    tick = bridge.tick(sym.broker_symbol)
    try:                                  # Parts 27-30 Phase 7 (Part 30.3): quality of what the broker returned
        from app.intelligence import data_quality
        dq = data_quality.assess(raw, TF_MINUTES[tf], now=now, tick=tick, source=f"mt5:{sym.broker_symbol}:{tf}",
                                 max_quote_age_s=s.max_quote_age_seconds)
    except Exception as exc:  # noqa: BLE001 - recording must not break the scan
        dq = {"error": str(exc)[:200]}
    caps = {CAP_TICK_VOLUME}
    if "real_volume" in df and df["real_volume"].sum() > 0:
        caps.add(CAP_REAL_VOLUME)
    peers, peer_problems = {}, []
    metas_all = registry.all_strategies()
    if sym.peers and any(CAP_PEERS in metas_all[c].requires for c in enabled):
        peers, peer_problems = peer_frames(db, bridge, sym, tf, now)
        if peers:
            caps.add(CAP_PEERS)
    ctx = StrategyContext(df, sym.broker_symbol, tf, htf=htf, peers=peers, point=float(sym.point), data_caps=caps)
    with cyc.step("market_regime", ref):
        reg = rg.classify(df)
    cyc.note("market_regime", {f"regime_{reg.primary}": 1})
    # Master upgrade (adaptive configuration): the best matching validated, active profile per strategy for this regime,
    # symbol and timeframe. Without one, the existing configuration applies unchanged.
    profiles_applied: dict = {}
    try:
        from app.services.trading import get_state as _gs
        from app.strategies import profiles as prof
        enabled, profiles_applied = prof.apply(db, enabled, _gs(db).active_mode, reg.primary, sym.broker_symbol, tf)
        if profiles_applied:
            cyc.note("market_regime", {"config_profiles": len(profiles_applied)})
    except Exception as exc:  # noqa: BLE001 - a profile problem never breaks the scan; the defaults apply
        log.warning("configuration profiles skipped for %s: %s", ref, exc)
        profiles_applied = {}
    with cyc.step("original_strategy_analysis", ref):
        sigs, diag = registry.evaluate_all(ctx, enabled)
    outcomes = record_evaluations(diag, not_applicable)
    cyc.note("original_strategy_analysis", outcomes)
    from app.news.service import status_for
    with cyc.step("news_events", ref):
        news, _ = status_for(db, sym, sym.broker_symbol)
    cyc.note("news_events", {f"news_{news}": 1})
    metas = registry.all_strategies()
    spread = (float(tick.get("ask", 0)) - float(tick.get("bid", 0))) if tick else 0.0
    patt = None
    try:                                  # Parts 27-30 Phase 5 (Part 28): advisory pattern detections
        from app.patterns import service as patterns
        with cyc.step("pattern_recognition", ref):
            with db.begin_nested():
                patt = patterns.run(db, sym, tf, df, regime=reg.primary, sessions=ss.current_sessions(pd.Timestamp(now)),
                                    spread_points=round(spread / float(sym.point), 2) if float(sym.point) else None,
                                    sigs=sigs, data_caps=sorted(caps))
        cyc.note("pattern_recognition", {"detections": patt["detections"], "new": patt["new"],
                                         "active": len(patt["active"])})
    except Exception as exc:  # noqa: BLE001 - pattern recognition must never break the scan
        log.warning("pattern recognition skipped for %s: %s", ref, exc)
    mctx = MarketContext(sym.broker_symbol, tf, float(tick.get("bid") or df.close.iloc[-1]), df.index[-1].to_pydatetime(),
                         ctx.atr_last(), spread, reg.primary, reg.tags, ctx.htf_bias(), ss.current_sessions(pd.Timestamp(now)),
                         news, {k: metas[k].regimes for k in enabled}, stats)
    b = mctx.htf_bias
    cyc.note("multi_timeframe", {"bias_bullish": int(b > 0), "bias_bearish": int(b < 0), "bias_neutral": int(b == 0)})
    with cyc.step("signal_quality", ref):
        res = build_signal(sigs, mctx, cfg, calib)
    if profiles_applied:                  # configuration version on each strategy entry of the signal
        res.strategies = [{**e, "config_profile": profiles_applied[e["strategy"]]}
                          if isinstance(e, dict) and e.get("strategy") in profiles_applied else e
                          for e in (res.strategies or [])]
    cyc.note("signal_quality", {f"decision_{res.decision}": 1})
    adaptive_recs: list = []
    try:
        with cyc.step("adaptive_optimisation", ref):
            res, adaptive_recs = _adaptive(db, sigs, mctx, cfg, calib, res)
    except Exception as exc:  # noqa: BLE001 - adaptive weighting must never break the scan; the baseline result stands
        log.warning("adaptive weighting skipped for %s: %s", ref, exc)
    intel, consensus_rec = _intelligence(db, df, sym, tf, sigs, res, news, tick, mctx, metas, enabled, cyc, ref)
    fused = None
    try:
        with cyc.step("signal_quality", ref):
            fused = _fuse(db, sym, res, intel, news, spread, mctx, cfg, adaptive_recs)
        cyc.note("signal_quality", {f"fused_{fused['decision']}": 1, f"evidence_{fused['evidence']}": 1})
    except Exception as exc:  # noqa: BLE001 - fusion must never break the scan; the original decision stands
        log.warning("fusion skipped for %s: %s", ref, exc)
        fused = {"error": str(exc)[:200]}
    opp = None
    if sigs:
        try:
            opp = _opportunity(db, sym, tf, df, reg, sigs, outcomes, news, mctx, res, intel, fused, spread, caps, htf,
                               peers, adaptive_recs, stats, cyc, ref, now, patt, dq)
        except Exception as exc:  # noqa: BLE001 - the trace must not break the scan; the engine's decision stands
            log.warning("opportunity record skipped for %s: %s", ref, exc)
    ar = AnalysisResult(symbol_id=sym.id, timeframe=tf, bar_time=df.index[-1].to_pydatetime(), regime=reg.primary,
                        structure={"regime": reg.dict(), "htf_bias": mctx.htf_bias, "sessions": mctx.sessions,
                                   "news": news, "atr": mctx.atr, "price": mctx.price,
                                   "spread_points": round(spread / float(sym.point), 2) if float(sym.point) else None,
                                   # Phase 8: what this symbol's feed actually provided and what every strategy did
                                   "data_caps": sorted(caps), "peers": sorted(peers), "peer_problems": peer_problems,
                                   "peer_correlation": peer_correlation(df, peers) if peers else {},
                                   "evaluation": outcomes, "data_quality": dq,
                                   "unavailable": sorted({d["strategy"] for d in diag if d["status"] == "unavailable"})},
                        strategy_outputs=[s.dict() for s in sigs] + [d for d in diag if d["status"] == "error"],
                        score={**res.dict(), "intelligence": intel, "fusion": fused})
    db.add(ar)
    db.flush()
    if consensus_rec is not None:
        consensus_rec.analysis_id = ar.id
    agent_activity("market_scanner", "evaluating", f"{sym.broker_symbol} {tf}: {len(sigs)} setups, "
                                                   f"{res.decision} {res.grade} ({res.score})")
    return {"analysis": ar, "result": res, "symbol": sym, "regime": reg, "price": mctx.price, "spread": spread,
            "intel": intel, "consensus_record": consensus_rec, "adaptive_records": adaptive_recs, "opportunity": opp}


def _opportunity(db: Session, sym, tf, df, reg, sigs, outcomes, news, mctx, res, intel, fused, spread, caps, htf, peers,
                 adaptive_recs, stats, cyc, ref, now, patt=None, dq=None) -> dict:
    """Parts 27-30 Phase 4: the new specialist views and the decision trace for this analysis. Entry geometry errors
    make the setup NO_TRADE; with RR_OPPORTUNITY_GATE=enforce, entry WAIT or HIGH failure risk downgrade BUY/SELL to
    WAIT. Nothing here can upgrade a decision."""
    import json

    from app.intelligence import opportunity as opx
    from app.services import trading as tr
    s = get_settings()
    with cyc.step("fundamental_analysis", ref):
        fund = opx.fundamental_view(db, sym, sym.broker_symbol, now)
    cyc.note("fundamental_analysis", {"calendar_available": int(bool(fund.get("available")))})
    if news == "RESTRICTED" or fund.get("high_impact_released_last_hour"):
        if "NEWS_DRIVEN" not in reg.tags:
            reg.tags.append("NEWS_DRIVEN")
    original = res.decision
    with cyc.step("entry_validation", ref):
        entry = opx.entry_validation(res, mctx.price, spread, mctx.atr, max_chase_atr=s.entry_max_chase_atr,
                                     max_spread_sl=s.entry_max_spread_sl)
    cyc.note("entry_validation", {f"entry_{entry['outcome']}": 1})
    with cyc.step("exit_optimisation", ref):
        acct = tr.active_account(db)
        plan = opx.exit_plan(res, tr.account_risk_params(db, acct))
    with cyc.step("failure_prediction", ref):
        fail = opx.failure_prediction(res, stats, intel, news, reg.tags, mctx.htf_bias)
    cyc.note("failure_prediction", {f"failure_risk_{fail['level']}": 1})
    from app.intelligence import selection as sel_mod
    with cyc.step("strategy_ranking", ref):
        selection = sel_mod.assess(res, stats, registry.all_strategies(), reg.primary, reg.tags)
    cyc.note("strategy_ranking", {f"selection_{selection['decision']}": 1})
    try:
        health = sel_mod.health(db)
    except Exception as exc:  # noqa: BLE001
        health = {"error": str(exc)[:200], "models_healthy": True, "providers_healthy": True}
    gate = []
    if entry["outcome"] == "FAIL" and res.decision in ("BUY", "SELL", "WAIT"):
        res.decision = "NO_TRADE"
        res.warnings.append(f"entry validation failed ({entry['reason']}): NO_TRADE")
        gate.append("entry geometry: NO_TRADE")
    elif s.opportunity_gate == "enforce" and res.decision in ("BUY", "SELL"):
        if entry["outcome"] == "WAIT":
            res.decision = "WAIT"
            res.warnings.append(f"entry validation {entry['reason']} (enforce mode): downgraded to WAIT")
            gate.append("entry validation: WAIT")
        elif fail["level"] == "HIGH":
            res.decision = "WAIT"
            res.warnings.append("failure risk HIGH (enforce mode): downgraded to WAIT")
            gate.append("failure risk HIGH: WAIT")
        elif selection["decision"] == "REJECT":
            res.decision = "WAIT"
            res.warnings.append(f"strategy selection REJECT ({selection['reason']}, enforce mode): downgraded to WAIT")
            gate.append("selection REJECT: WAIT")
        elif not health.get("models_healthy", True):
            res.decision = "WAIT"
            res.warnings.append("a production model is unhealthy (enforce mode): downgraded to WAIT")
            gate.append("model health: WAIT")
    cons = intel.get("consensus") or {}
    prob = intel.get("probability") or {}
    lead = sorted(sigs, key=lambda x: -x.strength)[:8]
    from app.patterns import service as patterns
    try:
        align = patterns.alignment(db, sym.id, scan_timeframes(db))
    except Exception as exc:  # noqa: BLE001
        align = {"error": str(exc)[:200]}
    trace = [
        opx.stage("data", "symbol_scanner", "ok", {"closed_bars": len(df), "last_closed_bar": df.index[-1].isoformat(),
                                                   "data_caps": sorted(caps), "higher_timeframes": sorted(htf),
                                                   "peers": sorted(peers), "quality": dq}),
        opx.stage("market_regime", "market_regime", reg.primary, {"tags": list(reg.tags), "adx": reg.adx,
                                                                  "atr_percentile": reg.atr_pct}),
        opx.stage("strategies", "original_strategy_analysis", f"{len(sigs)} setups",
                  {"outcomes": outcomes, "top": [{"strategy": x.strategy, "direction": x.direction, "strength": x.strength}
                                                 for x in lead]}),
        opx.stage("multi_timeframe", "multi_timeframe", mctx.htf_bias, {}),
        opx.stage("timeframe_alignment", "multi_timeframe", align.get("aligned"), align),
        opx.stage("patterns", "pattern_recognition",
                  f"{len(patt['active'])} open" if patt else "unavailable",
                  {"open": (patt or {}).get("active", []),
                   "relation_to_engine": patterns.relation_to((patt or {}).get("active", []), res.direction),
                   "note": "advisory: detections never change the decision"}),
        opx.stage("news", "news_events", news, {}),
        opx.stage("fundamental", "fundamental_analysis", "available" if fund.get("available") else "unavailable", fund),
        opx.stage("signal_engine", "signal_quality", f"{original} {res.grade}",
                  {"score": res.score, "components": res.components, "opposing_score": res.opposing_score}),
        opx.stage("adaptive", "adaptive_optimisation",
                  "production profile used" if any(not r.shadow for r in adaptive_recs) else
                  ("shadow only" if adaptive_recs else "original weights"), {}),
        opx.stage("consensus", "strategy_correlation", cons.get("decision"),
                  {k: cons.get(k) for k in ("agreement", "conflicts", "weights_version")}),
        opx.stage("probability", "signal_quality", "available" if prob.get("available") else "unavailable",
                  {k: prob.get(k) for k in ("tp_before_sl_probability", "model", "status", "sample") if k in prob}),
        opx.stage("anomalies", "signal_quality", len((intel.get("anomalies") or {}).get("found") or []),
                  {"blocking": (intel.get("anomalies") or {}).get("blocking") or []}),
        opx.stage("fusion", "signal_quality", (fused or {}).get("decision"),
                  {k: (fused or {}).get(k) for k in ("evidence", "mode", "error") if k in (fused or {})}),
        opx.stage("entry_validation", "entry_validation", entry["outcome"], entry),
        opx.stage("exit_plan", "exit_optimisation", "available" if plan.get("available") else "unavailable", plan),
        opx.stage("failure_prediction", "failure_prediction", fail["level"], fail),
        opx.stage("strategy_selection", "strategy_ranking", selection["decision"], selection),
        opx.stage("health", "master_orchestrator",
                  "healthy" if health.get("models_healthy", True) and health.get("providers_healthy", True) else "degraded",
                  health),
        opx.stage("decision", "master_orchestrator", res.decision,
                  {"engine_decision": original, "gate_mode": s.opportunity_gate, "gate": gate,
                   "next": "shortlist and AI review, then the Risk Engine at execution" if res.decision in ("BUY", "SELL")
                   else None}),
    ]
    breakdown = {"engine": {"score": res.score, "grade": res.grade, "components": res.components,
                            "opposing_score": res.opposing_score, "calibrated_confidence": res.calibrated_confidence,
                            "confidence_sample": res.confidence_sample},
                 "consensus": {"decision": cons.get("decision"), "agreement": cons.get("agreement")},
                 "probability": prob.get("tp_before_sl_probability") if prob.get("available") else None,
                 "fusion": (fused or {}).get("decision"), "entry_validation": entry["outcome"],
                 "failure_risk": fail["level"], "fundamental_available": bool(fund.get("available")),
                 "timeframes_aligned": align.get("aligned"),
                 "selection": {"decision": selection["decision"], "evidence": selection.get("evidence")},
                 "patterns": patterns.relation_to((patt or {}).get("active", []), res.direction)}
    safe = json.loads(json.dumps({"trace": trace, "breakdown": breakdown, "entry": entry}, default=str))
    return safe


def _fuse(db: Session, sym, res, intel: dict, news: str, spread: float, mctx: MarketContext, cfg: SignalConfig,
          adaptive_recs: list) -> dict:
    """Phase 13: one explainable decision from all the evidence (app.intelligence.fusion). Advisory by default: the
    original engine's decision is kept and the fused one recorded beside it. With RR_FUSION_MODE=enforce the fused
    decision is used; it can only keep or make more cautious the original one."""
    from app.db.models_intel import ModelRegistry
    from app.intelligence import fusion
    from app.services import trading as tr
    s = get_settings()
    acct = tr.active_account(db)
    used = next((r for r in adaptive_recs if not r.shadow), None)
    weighting = (f"adaptive profile v{db.get(ModelRegistry, used.model_id).version}" if used is not None
                 else "original weights")
    out = fusion.fuse(res, intel, news=news, spread=spread, atr=mctx.atr, htf_bias=mctx.htf_bias,
                      max_spread_atr=cfg.max_spread_atr,
                      history=fusion.historical(db, acct.mode if acct else "DEMO", sorted({x["strategy"] for x in res.strategies})),
                      exposure_info=fusion.exposure(db, sym.broker_symbol, res.direction), weighting=weighting,
                      mode=s.fusion_mode).dict()
    if s.fusion_mode == "enforce" and out["decision"] != res.decision:
        res.warnings.append(f"fusion {out['decision']} (enforce mode): original {res.decision} not used")
        res.decision = out["decision"]
        if out["decision"] == "NO_TRADE":
            res.direction = None
    return out


def _adaptive(db: Session, sigs, mctx: MarketContext, cfg: SignalConfig, calib: Calibrator, baseline):
    """Phase 12. A PRODUCTION adaptive profile (administrator-approved, signature verified) re-weights strategies and
    its result is used; a VALIDATED/APPROVED one runs in shadow beside the original result. Either way both decisions
    are recorded. With no usable profile the original engine's result is returned unchanged."""
    from dataclasses import replace

    from app.learning_engine import adaptive
    prof = adaptive.active_profiles(db)
    used, recs = baseline, []
    for kind in ("production", "shadow"):
        if prof[kind] is None:
            continue
        reg, mults = prof[kind]
        applied = adaptive.resolve(mults, mctx.regime)
        alt = build_signal(sigs, replace(mctx, adaptive=applied), cfg, calib)
        live = kind == "production"
        recs.append(adaptive.record_shadow(db, reg, baseline, alt, applied, live))
        if live:
            used = alt
    return used, recs


def _intelligence(db: Session, df, sym, tf, sigs, res, news, tick, mctx, metas, enabled, cyc=None, ref=None):
    """Part 26 enrichment. Advisory by default; with RR_CONSENSUS_MODE=enforce a BUY/SELL that the
    consensus does not support is downgraded to WAIT (never upgraded)."""
    from app.intelligence import anomaly as anomaly_intel
    from app.intelligence import consensus, probability
    from app.intelligence import regime as regime_intel
    from app.db.models_intel import RegimeHistory
    s = get_settings()
    out: dict = {}
    rec = None
    try:
        found = anomaly_intel.detect(df, tick, tf, float(sym.point), news_status=news,
                                     stale_seconds=s.max_quote_age_seconds)
        out["anomalies"] = {"found": found, **anomaly_intel.record(db, sym.broker_symbol, found)}
    except Exception as exc:  # noqa: BLE001 - intelligence must never break the scan
        out["anomalies"] = {"error": str(exc)[:200]}
    ri = None
    if s.regime_intel_enabled:
        try:
            r = regime_intel.analyse(df, news, {k: metas[k].regimes for k in enabled})
            ri = r.dict()
            ri_small = {k: ri[k] for k in ("regime", "probabilities", "confidence", "stability", "transition_warning",
                                           "model_version")}
            out["regime"] = {**ri_small, "suitable_count": len(r.suitable), "unsuitable_count": len(r.unsuitable)}
            exists = db.scalar(select(RegimeHistory.id).where(RegimeHistory.symbol == sym.broker_symbol,
                                                              RegimeHistory.timeframe == tf,
                                                              RegimeHistory.bar_time == df.index[-1].to_pydatetime(),
                                                              RegimeHistory.model_version == r.model_version))
            if not exists:
                db.add(RegimeHistory(symbol=sym.broker_symbol, timeframe=tf, bar_time=df.index[-1].to_pydatetime(),
                                     regime=r.regime, probabilities=r.probabilities, confidence=r.confidence,
                                     stability=r.stability, transition_warning=r.transition_warning,
                                     suitable=r.suitable, unsuitable=r.unsuitable, details=r.details,
                                     model_version=r.model_version))
        except Exception as exc:  # noqa: BLE001
            out["regime"] = {"error": str(exc)[:200]}
    if res.decision in ("BUY", "SELL", "WAIT") and res.direction:
        lead = next((x for x in sigs if x.direction == res.direction), None)
        prob = None
        if lead is not None:
            try:
                sp_pts = (mctx.spread / float(sym.point)) if float(sym.point) else 0.0
                prob = probability.estimate(db, df, lead.dict(), sum(1 for x in sigs if x.direction == res.direction),
                                            sp_pts, float(sym.point), sym.broker_symbol, tf)
            except Exception as exc:  # noqa: BLE001
                prob = {"available": False, "status": f"error: {str(exc)[:120]}"}
        out["probability"] = prob
        gates = []
        blocking = (out.get("anomalies") or {}).get("blocking") or []
        if blocking:
            gates.append(f"critical anomaly: {blocking}")
        if news == "RESTRICTED":
            gates.append("news restriction window")
        cats = {code: metas[code].category for code in enabled}
        cyc = cyc or NullCycle()
        with cyc.step("strategy_correlation", ref):
            cons = consensus.run(db, sigs, cats, ri, prob, gates, res.direction,
                                 record={"symbol": sym.broker_symbol, "timeframe": tf})
        cyc.note("strategy_correlation", {f"consensus_{cons['decision']}": 1})
        for v in cons["specialists"]:
            agent = SPECIALIST_AGENT.get(v["specialist"])
            if agent:
                cyc.add(agent, ref=f"{ref}:{v['specialist']}", out={f"vote_{v['decision']}": 1},
                        evidence={"timing": "evaluated together inside the consensus step (see strategy_correlation)"})
        rec = cons.pop("record", None)
        out["consensus"] = {k: cons[k] for k in ("decision", "agreement", "conflicts", "weights_version", "explanation")}
        out["consensus"]["specialists"] = [{"specialist": v["specialist"], "decision": v["decision"],
                                            "confidence": v["confidence"]} for v in cons["specialists"]]
        if s.consensus_mode == "enforce" and res.decision in ("BUY", "SELL") and cons["decision"] != res.decision:
            res.decision = "WAIT"
            res.warnings.append(f"consensus {cons['decision']} (enforce mode): downgraded to WAIT")
    return out, rec


def persist_signal(db: Session, item: dict) -> TradingSignal | None:
    r = item["result"]
    if r.decision not in ("BUY", "SELL", "WAIT"):
        return None
    sig = TradingSignal(symbol_id=item["symbol"].id, analysis_id=item["analysis"].id, decision=r.decision, grade=r.grade,
                        timeframe=r.timeframe, entry=r.entry, stop_loss=r.stop_loss, tp1=r.tp1, tp2=r.tp2, tp3=r.tp3,
                        risk_reward=r.risk_reward, score=r.score, calibrated_confidence=r.calibrated_confidence,
                        confidence_sample=r.confidence_sample, strategies=r.strategies, regime=r.regime,
                        explanation=r.explanation, expires_at=r.expires_at)
    db.add(sig)
    db.flush()
    if item.get("consensus_record") is not None:
        item["consensus_record"].signal_id = sig.id
    for rec in item.get("adaptive_records") or []:
        rec.signal_id = sig.id
    publish("signal", {"id": sig.id, "symbol": item["symbol"].broker_symbol, "decision": sig.decision, "grade": sig.grade,
                       "score": sig.score, "timeframe": sig.timeframe})
    if r.grade == "A+":
        notify(db, "SIGNAL_A_PLUS", f"A+ {r.decision} {item['symbol'].broker_symbol} {r.timeframe}", r.explanation)
    elif r.decision in ("BUY", "SELL"):
        notify(db, f"SIGNAL_{r.decision}", f"{r.decision} {item['symbol'].broker_symbol} {r.timeframe} ({r.grade})",
               r.explanation)
    return sig


def claude_review(db: Session, agent: ClaudeAgent, sig: TradingSignal, item: dict, extra: dict, cycle=None) -> None:
    r = item["result"]
    if not agent.throttle_ok(item["symbol"].broker_symbol, r.timeframe):
        return
    candidate = {**r.dict(), "symbol": item["symbol"].broker_symbol, "broker_symbol": item["symbol"].broker_symbol,
                 "current_price": item["price"], "spread": item["spread"], "regime_detail": item["regime"].dict(),
                 "structure": item["analysis"].structure, **extra}
    conv = AgentConversation(purpose="SIGNAL_REVIEW", model=agent.model, request={"signal_id": sig.id})
    db.add(conv)
    db.flush()
    try:
        review, meta = agent.review(candidate)
    except AgentError as exc:
        conv.error = str(exc)
        if cycle is not None:
            cycle.add("signal_quality", ref=f"signal:{sig.id}", error=f"AI review: {str(exc)[:200]}")
        notify(db, "CLAUDE_ERROR", "Claude review failed", str(exc))
        agent_activity("claude_agent", "error", str(exc))
        # Without a review the candidate is not auto-executed (conservative), unless the administrator's policy for an
        # unavailable AI is engine_only (autonomous agent upgrade).
        from app.services.trading import unavailable_review
        sig.ai_review = unavailable_review(db, str(exc))
        _review_to_opportunity(db, sig, sig.ai_review, None)
        return
    conv.response, conv.input_tokens, conv.output_tokens = review.model_dump(), meta["input_tokens"], meta["output_tokens"]
    conv.latency_ms, conv.model = meta["latency_ms"], meta["model"]
    if cycle is not None:
        cycle.add("signal_quality", ref=f"signal:{sig.id}", ms=meta["latency_ms"], out={f"ai_review_{review.decision}": 1},
                  provider=meta.get("provider", "anthropic"), model=meta["model"],
                  evidence={"ai_output": "schema-validated and boundary-checked (confirm / downgrade / reject only)"})
        if meta.get("mode") == "consensus":
            agree = len({v["decision"] for v in meta["consensus"]["votes"]}) == 1
            cycle.add("ai_consensus", ref=f"signal:{sig.id}", ms=meta["latency_ms"],
                      out={f"consensus_{review.decision}": 1, "agreed": int(agree)},
                      provider=meta["provider"], model=meta["model"],
                      evidence={"votes": meta["consensus"]["votes"], "rule": meta["consensus"]["rule"]})
    prob = extra.get("probability") or {}
    sig.ai_review = {**review.model_dump(), "model_versions": {
        "claude": meta["model"], "consensus": (extra.get("consensus") or {}).get("weights_version"),
        "probability": prob.get("model") if prob.get("available") else None,
        "regime": (extra.get("regime_intelligence") or {}).get("model_version")}}
    db.add(AgentDecision(conversation_id=conv.id, signal_id=sig.id, symbol=item["symbol"].broker_symbol,
                         decision=review.decision, payload=review.model_dump()))
    if review.decision == "NO_TRADE":
        sig.decision, sig.status = "NO_TRADE", "REJECTED"
    elif review.decision == "WAIT_FOR_CONFIRMATION" and sig.decision in ("BUY", "SELL"):
        sig.decision = "WAIT"
    _review_to_opportunity(db, sig, review.model_dump(), meta)


def _review_to_opportunity(db: Session, sig: TradingSignal, review: dict, meta: dict | None) -> None:
    from app.db.models_opportunity import Opportunity
    from app.intelligence import opportunity as opx
    try:
        with db.begin_nested():
            opp = db.scalar(select(Opportunity).where(Opportunity.signal_id == sig.id).order_by(Opportunity.id.desc())
                            .limit(1))
            if opp is not None:
                opx.after_review(db, opp, review, meta)
    except Exception as exc:  # noqa: BLE001 - the trace must not break the review
        log.warning("opportunity review stage skipped for signal %s: %s", sig.id, exc)


def supersede_same_bar(db: Session, item: dict, trigger: str) -> str | None:
    """An event-driven re-analysis of a bar that already has a signal: still-NEW signals of that bar are expired
    (superseded) so there is never more than one NEW signal per symbol, timeframe and bar. If one of them was already
    executed, no new signal is created (returned reason)."""
    ar = item["analysis"]
    rows = db.scalars(select(TradingSignal).join(AnalysisResult, AnalysisResult.id == TradingSignal.analysis_id)
                      .where(TradingSignal.symbol_id == ar.symbol_id, TradingSignal.timeframe == ar.timeframe,
                             AnalysisResult.bar_time == ar.bar_time, AnalysisResult.id != ar.id)
                      .with_for_update(of=TradingSignal)).all()
    if any(x.status == "EXECUTED" for x in rows):
        return "a signal from this bar was already executed; the re-analysis creates no second signal"
    for x in rows:
        if x.status == "NEW":
            x.status = "EXPIRED"
            x.explanation = f"{x.explanation} [Superseded by re-analysis ({trigger})]"
    return None


def record_opportunity(db: Session, item: dict, sig: TradingSignal | None, trigger: str, blocked: str | None) -> None:
    """Persist the opportunity built by analyse() (only analyses in which a strategy produced a setup)."""
    info = item.get("opportunity")
    if not info:
        return
    from app.intelligence import opportunity as opx
    try:
        with db.begin_nested():
            ar = item["analysis"]
            opp = opx.create(db, symbol_id=ar.symbol_id, timeframe=ar.timeframe, bar_time=ar.bar_time,
                             analysis_id=ar.id, res=item["result"], trigger=trigger, trace=info["trace"],
                             breakdown=info["breakdown"], entry=info["entry"])
            opp.signal_id = sig.id if sig is not None else None
            if blocked and opp.state not in opx.TERMINAL:
                opx.transition(db, opp, "REJECTED", blocked)
    except Exception as exc:  # noqa: BLE001 - the trace must not break the scan
        log.warning("opportunity not recorded: %s", exc)


def prune_opportunities(days: int) -> int:
    from sqlalchemy import delete

    from app.db.models_opportunity import Opportunity
    from app.intelligence.opportunity import TERMINAL
    try:
        with session_scope() as db:
            res = db.execute(delete(Opportunity).where(Opportunity.state.in_(TERMINAL),
                                                       Opportunity.updated_at < datetime.now(UTC) - timedelta(days=days)))
            from app.db.models_opportunity import PatternDetection
            from app.patterns.lifecycle import CLOSED
            res2 = db.execute(delete(PatternDetection).where(PatternDetection.state.in_(CLOSED),
                                                             PatternDetection.updated_at < datetime.now(UTC) - timedelta(days=days)))
            return (res.rowcount or 0) + (res2.rowcount or 0)
    except Exception:  # noqa: BLE001
        return 0


def closed_bar_epoch(bridge: BridgeClient, symbol: str, tf: str, now: datetime) -> int | None:
    """Time of the latest closed bar, by the same rule as analyse() (a 3-bar request). None if unknown."""
    from app.signals.engine import TF_MINUTES
    try:
        df = drop_forming_bar(bars_to_df(bridge.rates(symbol, tf, 3)), TF_MINUTES[tf], now)
    except Exception:  # noqa: BLE001 - unknown: the job is treated as due and analyse() reports the problem
        return None
    return int(df.index[-1].timestamp()) if len(df) else None


def scan_once(bridge: BridgeClient, agent: ClaudeAgent | None, auto_execute,
              should_stop=lambda: False) -> dict:
    s = get_settings()
    rds = get_redis()
    with session_scope() as db:
        from app.services.trading import get_state
        mode = get_state(db).active_mode
        ai_mode = get_state(db).ai_mode or "AI"
        enabled = enabled_strategies(db, mode)
        t_stats = time.perf_counter()
        stats = strategy_stats(db)
        stats_ms = (time.perf_counter() - t_stats) * 1000
        calib = calibrator(db)
        symbols = db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True))
                             .order_by(Symbol.priority, Symbol.canonical)).all()
        targets = [(x.id, x.broker_symbol, x.priority) for x in symbols]
        timeframes = scan_timeframes(db)
    report = sched.CycleReport(started_at=sched.now_iso(), timeframes=timeframes, symbols=len(targets))
    cycle = Cycle("scan_cycle")
    agent_activity("market_scanner", "processing", f"Scanning {len(targets)} symbols x {timeframes}")

    # 1. Which jobs are due: a closed bar newer than the last one analysed (or every job, if once-per-bar is off),
    # or an event asked for a re-analysis (Parts 27-30 Phase 4: news release, anomaly, price at a waiting entry).
    now = datetime.now(UTC)
    from app.scanner import triggers
    try:
        with session_scope() as db:
            triggers.check(db, bridge, rds, timeframes, now, s.reanalysis_throttle_seconds)
    except Exception as exc:  # noqa: BLE001 - event detection must not stop the scan
        log.warning("event trigger check failed: %s", exc)
    triggered: dict[tuple[int, str], str] = {}
    jobs: list[sched.Job] = []
    for sid, name, prio in targets:
        for tf in timeframes:
            report.jobs_total += 1
            if should_stop():
                break
            bar = closed_bar_epoch(bridge, name, tf, now) if s.scan_once_per_bar else None
            last = rds.get(sched.last_key(sid, tf)) if s.scan_once_per_bar else None
            trig = triggers.pending(rds, sid, tf)
            if s.scan_once_per_bar and not sched.is_due(int(last) if last else None, bar) and not trig:
                report.not_due += 1
                continue
            if trig:
                triggered[(sid, tf)] = trig
            jobs.append(sched.Job(priority=prio, bar_epoch=bar or 0, tf=tf, symbol_id=sid, symbol=name))
    report.due = len(jobs)

    def work(job: sched.Job):
        lock = sched.inflight_key(job.symbol_id, job.tf)
        if not rds.set(lock, "1", nx=True, ex=max(60, s.scan_job_timeout_seconds * 4)):
            return {"outcome": "skipped_inflight", "symbol": job.symbol, "tf": job.tf}
        try:
            with session_scope() as db:
                sym = db.get(Symbol, job.symbol_id)
                try:
                    item = analyse(db, bridge, sym, job.tf, enabled, stats, calib, SignalConfig(), cycle)
                except Exception as exc:  # noqa: BLE001 - one symbol failing must not stop the scan
                    log.exception("scan %s %s failed", sym.broker_symbol, job.tf)
                    return {"symbol": sym.broker_symbol, "tf": job.tf, "error": str(exc)}
                trig = triggered.get((job.symbol_id, job.tf))
                if not item:
                    if job.bar_epoch:
                        rds.set(sched.last_key(job.symbol_id, job.tf), job.bar_epoch)
                    if trig:
                        triggers.clear(rds, job.symbol_id, job.tf)
                    return {"outcome": "insufficient_data", "symbol": sym.broker_symbol, "tf": job.tf}
                blocked = supersede_same_bar(db, item, trig) if trig else None
                sig = persist_signal(db, item) if blocked is None else None
                record_opportunity(db, item, sig, trig or "bar_close", blocked)
                analysed_bar = int(item["analysis"].bar_time.timestamp())
                out = {"signal_id": sig.id if sig else None, "score": item["result"].score,
                       "decision": item["result"].decision, "symbol": sym.broker_symbol, "tf": job.tf}
            # marker after the commit: a crash before it leaves the bar due, so it is retried, never lost
            rds.set(sched.last_key(job.symbol_id, job.tf), max(analysed_bar, job.bar_epoch))
            if trig:
                triggers.clear(rds, job.symbol_id, job.tf)
            return out
        finally:
            rds.delete(lock)

    # 2. Run them in order, within the cycle budget; the rest stay due for the next cycle.
    results = sched.run_jobs(sched.order(jobs), work, max_workers=s.scan_max_parallel,
                             budget_s=float(s.scan_cycle_budget_seconds), job_timeout_s=float(s.scan_job_timeout_seconds),
                             should_stop=should_stop, report=report)
    results = [x for x in results if x.get("outcome") is None]
    if report.due:
        cycle.add("symbol_scanner", ms=report.duration_s * 1000, started=cycle.started_at,
                  out={k: getattr(report, k) for k in ("jobs_total", "due", "not_due", "analysed", "deferred",
                                                        "timed_out", "errors", "insufficient_data")})
        cycle.add("historical_performance", ref="strategy_stats", ms=stats_ms, out={"strategies_with_evidence": len(stats)},
                  evidence={"sources": "OOS, walk-forward, paper and live results only (in-sample backtests excluded)"})
    t_rank = time.perf_counter()
    eligible = [r for r in results if r.get("signal_id") and r["decision"] in ("BUY", "SELL")]
    ranked = sorted(eligible, key=lambda r: r["score"], reverse=True)[: s.claude_max_candidates_per_scan]
    if eligible:
        cycle.add("strategy_ranking", ref=[r["signal_id"] for r in ranked], ms=(time.perf_counter() - t_rank) * 1000,
                  out={"candidates": len(eligible), "shortlisted": len(ranked)},
                  evidence={"rule": f"top {s.claude_max_candidates_per_scan} BUY/SELL by engine score"})
    for r in ranked:
        with session_scope() as db:
            sig = db.get(TradingSignal, r["signal_id"])
            from app.services.trading import wants_ai_review
            want = (False, "engine-only mode") if ai_mode == "ENGINE_ONLY" else wants_ai_review(db, sig)
            if ai_mode == "ENGINE_ONLY":
                # The operator runs the engine without AI: no provider call, no cost; the Risk Engine decides.
                from app.services.trading import engine_only_review
                sig.ai_review = engine_only_review()
                _review_to_opportunity(db, sig, sig.ai_review, {"mode": "ENGINE_ONLY"})
            elif not want[0]:
                # Engine-first (owner's decision 2026-11-26): the engine decides; no AI call for this candidate.
                from app.services.trading import engine_decided_review
                review = engine_decided_review(want[1])
                # Market playbook: the saved plan for this symbol and regime (AI only when no valid or proven plan
                # exists, capped per day). It can only restrict: stand aside, skip a direction, reduce size.
                try:
                    from app.intelligence import playbook as pbk
                    with db.begin_nested():
                        psym = db.get(Symbol, sig.symbol_id)
                        pb, how = pbk.get_or_create(db, psym, pbk.plan_regime(db, psym, sig.regime))
                except Exception as exc:  # noqa: BLE001 - a playbook problem never stops the engine
                    pb, how = None, f"none:error {type(exc).__name__}"
                if pb is not None:
                    v = pbk.apply(pb, sig.decision)
                    if v["allow"]:
                        review["playbook"] = {**v, "how": how}
                        review["size_multiplier"] = v["size_multiplier"]
                    else:
                        review = {"decision": "NO_TRADE", "mode": "PLAYBOOK", "note": v["reason"],
                                  "playbook": {**v, "how": how}}
                else:
                    review["playbook"] = {"how": how}
                sig.ai_review = review
                _review_to_opportunity(db, sig, sig.ai_review, {"mode": "ENGINE"})
                cycle.add("signal_quality", ref=f"signal:{sig.id}", out={"engine_decided": 1},
                          evidence={"ai_call": "playbook" if how == "ai" else f"none ({want[1]}; playbook {how})"})
            elif agent is not None:
                ar = db.get(AnalysisResult, sig.analysis_id)
                sym = db.get(Symbol, sig.symbol_id)
                sp_pts = (ar.structure or {}).get("spread_points")
                item = {"result": _ResultView(sig, ar), "symbol": sym, "analysis": ar,
                        "price": float(sig.entry or 0),
                        # gap 7.13: the spread measured at analysis time (was a constant 0.0)
                        "spread": float(sp_pts) * float(sym.point) if sp_pts is not None else None,
                        "regime": _RegimeView(ar)}
                with cycle.step("historical_performance", f"signal:{sig.id}"):
                    extra = _review_extra(db, sig, sym, ar, mode)
                claude_review(db, agent, sig, item, extra, cycle)
            else:
                from app.services.trading import unavailable_review
                sig.ai_review = unavailable_review(db, "no AI provider is configured for candidate review")
                _review_to_opportunity(db, sig, sig.ai_review, None)
        auto_execute(r["signal_id"])
    sched.publish_status(rds, report)
    cycle.finish({"analysed": report.analysed, "due": report.due, "candidates": len(ranked),
                  "timeframes": timeframes, "symbols": report.symbols})
    if rds.set("rr:agents:prune", "1", nx=True, ex=3600):
        from app.orchestration.orchestrator import prune
        prune(s.agent_run_retention_days)
        prune_opportunities(s.opportunity_retention_days)
    agent_activity("market_scanner", "waiting", f"Scan complete: {report.analysed} analyses ({report.not_due} not due, "
                                                f"{report.deferred} deferred), {len(ranked)} candidates")
    return {"analyses": report.analysed, "candidates": len(ranked), "errors": report.errors + report.timed_out,
            "not_due": report.not_due, "deferred": report.deferred, "timed_out": report.timed_out}


class _ResultView:
    """Adapts a stored signal to the shape claude_review expects."""

    def __init__(self, sig: TradingSignal, ar: AnalysisResult):
        self._d = dict(ar.score)
        self.timeframe = sig.timeframe

    def dict(self):
        return self._d


class _RegimeView:
    def __init__(self, ar: AnalysisResult):
        self._d = ar.structure.get("regime", {})

    def dict(self):
        return self._d


def _review_extra(db: Session, sig: TradingSignal, sym: Symbol, ar: AnalysisResult, mode: str) -> dict:
    """Historical evidence (structured SQL retrieval) + Part 26 intelligence for the Claude reviewer."""
    from app.learning_engine.memory import retrieve_evidence
    intel = (ar.score or {}).get("intelligence", {})
    sessions = (ar.structure or {}).get("sessions") or []
    sess = "+".join(x for x in sessions if x in ("ASIA", "LONDON", "NEW_YORK", "OVERLAP")) or "OFF_SESSION"
    try:
        ev = retrieve_evidence(db, mode, sym.broker_symbol, sig.timeframe, sorted({x["strategy"] for x in sig.strategies}),
                               ar.regime, sess)
    except Exception as exc:  # noqa: BLE001
        ev = {"error": str(exc)[:200]}
    return {"historical_evidence": ev, "consensus": intel.get("consensus"), "probability": intel.get("probability"),
            "regime_intelligence": intel.get("regime"), "anomalies": (intel.get("anomalies") or {}).get("found")}


def expire_signals(db: Session) -> int:
    n = 0
    for sig in db.scalars(select(TradingSignal).where(TradingSignal.status == "NEW",
                                                      TradingSignal.expires_at < datetime.now(UTC))):
        sig.status = "EXPIRED"
        n += 1
    # Parts 27-30 Phase 4: opportunities follow their signals (executed, rejected by the Risk Engine, expired).
    try:
        from app.intelligence import opportunity as opx
        with db.begin_nested():
            db.flush()
            opx.sync(db)
    except Exception as exc:  # noqa: BLE001 - the lifecycle record must not break expiry
        log.warning("opportunity sync failed: %s", exc)
    return n
_ = timedelta
