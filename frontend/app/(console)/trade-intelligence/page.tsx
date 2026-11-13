"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const DIMS = ["strategy", "symbol", "timeframe", "session", "regime", "exit_type", "attribution", "duration", "combination",
  "volatility", "grade", "spread_bucket", "entry_method", "model_version"];
const VERDICT: Record<string, any> = { POSITIVE_EDGE: "neon", NEGATIVE_EDGE: "bear", INCONCLUSIVE: "warn", INSUFFICIENT_DATA: "dim", NO_DATA: "dim" };
const num = (v: any, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const avg = (a: any, d = 2) => (a?.value == null ? <span className="text-dim">no data</span> : <span>{num(a.value, d)} <span className="text-dim">(n={a.sample})</span></span>);
const dur = (s: number | null) => s == null ? "—" : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`;

/** What actually happened in closed trades, per account mode, from the broker's deal history and the engine's own
 *  records. DEMO and LIVE are never combined; backtest, out-of-sample, walk-forward and paper results are separate. */
export default function TradeIntelligence() {
  const me = useMe();
  const manage = can(me, "strategy.manage");
  const [mode, setMode] = useState("DEMO"); const [dim, setDim] = useState("strategy");
  const { data: pf, error } = useApi<any>(`/api/learning/performance?mode=${mode}`, 60000);
  const { data: src } = useApi<any>("/api/learning/sources", 60000);
  const { data: trades } = useApi<any[]>(`/api/learning/trades?mode=${mode}&limit=300`, 60000);
  const { data: recs, reload: reloadRecs } = useApi<any[]>("/api/learning/recommendations", 60000);
  const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const o = pf?.overall;
  const research = (recs || []).filter((r) => r.source === "trade_research");
  async function runResearch() {
    setErr(null); setBusy(true);
    try { await post(`/api/learning/trade-research?mode=${mode}`); reloadRecs(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const groups = pf ? Object.entries(pf.dimensions[dim] || {}).map(([k, m]: any) => ({ key: k, ...m })) : [];
  return (
    <div>
      <PageTitle sub="Closed trades only, from the broker's deal history and the engine's records. Every figure states its sample; anything not recorded is shown as missing, never estimated. Below 30 trades no verdict is given."
        right={<div className="flex gap-1">{["DEMO", "LIVE"].map((m) => <Btn key={m} small tone={mode === m ? (m === "LIVE" ? "bear" : "neon") : "dim"} onClick={() => setMode(m)}>{m}</Btn>)}</div>}>
        Trade Intelligence · {mode}</PageTitle>
      <ErrorLine error={error || err} />
      {o && o.sample_size === 0 && <Panel><div className="text-[12px] text-dim">No closed {mode} trades yet. Figures appear once the learner has synchronised the broker's deal history.</div></Panel>}
      {o && o.sample_size > 0 && <>
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
          <Stat label="Trades" value={o.sample_size} sub={<Badge tone={VERDICT[o.verdict]}>{o.verdict.replace(/_/g, " ").toLowerCase()}</Badge>} />
          <Stat label="Win / loss / BE" value={`${o.outcomes.WIN || 0} / ${o.outcomes.LOSS || 0} / ${o.outcomes.BE || 0}`} sub={`win rate ${num(o.win_rate * 100, 1)}%`} />
          <Stat label="Profit factor" value={num(o.profit_factor)} sub={o.profit_factor_ci95?.[0] != null ? `95% CI ${num(o.profit_factor_ci95[0])}–${num(o.profit_factor_ci95[1])}` : "CI needs ≥ 5 trades"} />
          <Stat label="Expectancy" value={num(o.expectancy)} sub={`avg R ${num(o.average_r)} (n=${o.r_sample})`} tone={o.expectancy > 0 ? "up" : o.expectancy < 0 ? "down" : "plain"} />
          <Stat label="Max drawdown" value={num(o.max_drawdown)} sub="closed-trade equity" tone="down" />
          <Stat label="Costs" value={num(o.total_commission + o.total_swap)} sub={`commission ${num(o.total_commission)} · swap ${num(o.total_swap)}`} />
        </div>
        <div className="mb-3 grid gap-3 xl:grid-cols-3">
          <Panel title="Exits (broker deal reason)">
            {Object.entries(o.exit_types).map(([k, v]: any) => <div key={k} className="flex justify-between text-[11px]"><span>{k.replace(/_/g, " ").toLowerCase()}</span><b>{v}</b></div>)}
            <div className="mt-1 text-[10px] text-dim">{o.trades_with_partial_exits} with partial exits (known for {o.partial_exits_known})</div>
          </Panel>
          <Panel title="Quality">
            <div className="grid grid-cols-[150px_1fr] gap-1 text-[11px]">
              <div className="text-dim">Entry quality</div><div>{avg(o.avg_entry_quality)}</div>
              <div className="text-dim">Exit efficiency</div><div>{avg(o.avg_exit_efficiency)}</div>
              <div className="text-dim">Avg MAE / MFE (R)</div><div>{num(o.avg_mae_r)} / {num(o.avg_mfe_r)}</div>
              <div className="text-dim">Holding (median)</div><div>{dur(o.holding_seconds.median)}</div>
              <div className="text-dim">Slippage (points)</div><div>{avg(o.avg_slippage_points)}</div>
              <div className="text-dim">Spread at entry</div><div>{avg(o.avg_spread_points)}</div>
            </div>
            <div className="mt-1 text-[10px] text-dim">Entry quality: 1 = never went against the entry, 0 = went 1 R or more against it. Exit efficiency: share of the best available move kept (1 = best price, 0 = gave it back, −1 = lost at least as much).</div>
          </Panel>
          <Panel title="Attribution and data coverage">
            {Object.entries(o.attribution).map(([k, v]: any) => <div key={k} className="flex justify-between text-[11px]"><span>{k.replace(/_/g, " ").toLowerCase()}</span><b>{v}</b></div>)}
            <div className="mt-2 text-[10px] uppercase text-dim">Recorded for</div>
            {Object.entries(o.coverage).map(([k, v]: any) => <div key={k} className="flex justify-between text-[11px]"><span>{k.replace(/_/g, " ")}</span><span>{v} / {o.sample_size}</span></div>)}
            <div className="mt-1 text-[10px] text-dim">"Unlinked": no engine order is linked to the position; it is never assigned to a strategy.</div>
          </Panel>
        </div>
        <Panel title="By dimension" right={<select className="rounded border border-line bg-void px-2 py-1 text-[11px]" value={dim} onChange={(e) => setDim(e.target.value)}>{DIMS.map((d) => <option key={d}>{d}</option>)}</select>}>
          <Table rows={groups} rowKey={(g) => g.key} empty="No trades carry this dimension."
            cols={[{ k: "key", h: dim }, { k: "sample_size", h: "Trades" },
              { k: "verdict", h: "Verdict", r: (g) => <Badge tone={VERDICT[g.verdict]}>{g.verdict.replace(/_/g, " ").toLowerCase()}</Badge> },
              { k: "win_rate", h: "Win rate", r: (g) => `${num(g.win_rate * 100, 1)}%` }, { k: "profit_factor", h: "PF", r: (g) => num(g.profit_factor) },
              { k: "expectancy", h: "Expectancy", r: (g) => num(g.expectancy) }, { k: "average_r", h: "Avg R", r: (g) => num(g.average_r) },
              { k: "max_drawdown", h: "Max DD", r: (g) => num(g.max_drawdown) },
              { k: "eq", h: "Entry / exit quality", sort: false, r: (g) => <span>{num(g.avg_entry_quality?.value)} / {num(g.avg_exit_efficiency?.value)}</span> }]} />
        </Panel>
      </>}
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title="Evidence sources (kept apart)">
          {src && <>
            {Object.entries(src.closed_trades).map(([m, v]: any) => <div key={m} className="flex justify-between text-[11px]"><span>Closed trades · {m}</span><span>{v.sample_size} trades{v.sample_size ? ` · PF ${num(v.profit_factor)} · net ${num(v.net_profit)}` : ""}</span></div>)}
            {Object.entries(src.strategy_performance).map(([s, v]: any) => <div key={s} className="flex justify-between text-[11px]"><span>Strategy performance · {s}</span><span>{v.trades} trades · PF {num(v.profit_factor)}</span></div>)}
            {!Object.keys(src.strategy_performance).length && <div className="text-[11px] text-dim">No backtest, out-of-sample, walk-forward or paper records yet.</div>}
            <div className="mt-1 text-[10px] text-dim">{src.note}</div></>}
        </Panel>
        <Panel title="AI trade research" right={manage && <Btn small tone="violet" busy={busy} disabled={!o?.sample_size} onClick={runResearch}>Research {mode} trades</Btn>}>
          <div className="mb-1 text-[10px] text-dim">Sends only this mode's aggregated statistics (no tickets or accounts) through the AI gateway. The result is a recommendation to review; it changes nothing.</div>
          {research.length === 0 ? <div className="text-[11px] text-dim">No research yet.</div> : research.slice(0, 3).map((r) => (
            <div key={r.id} className="border-b border-line py-1 text-[11px]">
              <div className="text-dim">{fmt.time(r.at)} · {r.subject} · {r.evidence.provider}/{r.evidence.model} · {r.status}</div>
              {(r.evidence.findings || []).map((f: string) => <div key={f}>• {f}</div>)}
              {(r.evidence.data_gaps || []).length > 0 && <div className="text-warn">Data gaps: {r.evidence.data_gaps.join("; ")}</div>}
            </div>))}
        </Panel>
      </div>
      <Panel title={`Closed ${mode} trades`} className="mt-3">
        <Table rows={trades} rowKey={(t) => t.ticket} empty="No closed trades."
          cols={[{ k: "closed_at", h: "Closed", r: (t) => fmt.time(t.closed_at) }, { k: "symbol", h: "Symbol" }, { k: "direction", h: "Side" },
            { k: "net", h: "Net", r: (t) => <span className={t.net > 0 ? "text-neon" : t.net < 0 ? "text-bear" : ""}>{num(t.net)}</span> },
            { k: "r", h: "R", r: (t) => num(t.r) }, { k: "outcome", h: "Outcome" },
            { k: "exit_type", h: "Exit", r: (t) => t.exit_type.replace(/_/g, " ").toLowerCase() },
            { k: "attribution", h: "Attribution", r: (t) => t.attribution === "ENGINE_SIGNAL" ? (t.strategies || []).join(", ") : t.attribution.replace(/_/g, " ").toLowerCase() },
            { k: "holding_seconds", h: "Held", r: (t) => dur(t.holding_seconds) },
            { k: "mae_r", h: "MAE / MFE R", sort: false, r: (t) => `${num(t.mae_r)} / ${num(t.mfe_r)}` },
            { k: "costs", h: "Comm. / swap", sort: false, r: (t) => `${num(t.commission)} / ${num(t.swap)}` },
            { k: "regime", h: "Regime", r: (t) => t.regime || "—" }, { k: "session", h: "Session", r: (t) => t.session || "—" }]} />
      </Panel>
    </div>
  );
}
