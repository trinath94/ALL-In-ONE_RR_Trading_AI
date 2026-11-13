"use client";
import { FlaskConical } from "lucide-react";
import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Badge, Btn, Empty, ErrorLine, Field, inputCls, Stat, Table, toneOf } from "@/components/ui";
import { api, fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export const KINDS = ["BACKTEST", "OOS", "WALKFORWARD", "MONTECARLO", "OPTIMISE", "SPREAD"];

export function JobView({ id }: { id: number }) {
  const [job, setJob] = useState<any>(null);
  useEffect(() => {
    let t: any;
    const load = async () => { const j = await api(`/api/backtests/${id}`); setJob(j); if (j.status === "QUEUED" || j.status === "RUNNING") t = setTimeout(load, 2000); };
    load(); return () => clearTimeout(t);
  }, [id]);
  if (!job) return <Empty>Loading…</Empty>;
  if (job.status !== "DONE") return <div className="text-[12px]"><Badge tone={job.status === "FAILED" ? "bear" : "cyan"}>{job.status}</Badge> {job.error}</div>;
  return (
    <div className="space-y-3">
      {job.results.map((r: any, i: number) => {
        const m = r.metrics;
        return (
          <div key={i}>
            <div className="mb-2 text-[10px] uppercase tracking-widest text-cyan">{r.segment}</div>
            {m.sensitivity ? <Table rows={m.sensitivity} cols={[{ k: "spread_points", h: "Spread pts" }, { k: "trades", h: "Trades" }, { k: "net_profit", h: "Net" }, { k: "profit_factor", h: "PF" }, { k: "max_drawdown_pct", h: "Max DD %" }]} /> : <>
              {m.sample_warning && <div className="mb-2"><Badge tone="warn">{m.sample_warning}</Badge></div>}
              <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
                <Stat label="Trades" value={m.total_trades} sub={`${m.winning_trades}W / ${m.losing_trades}L`} />
                <Stat label="Win rate" value={fmt.pct(m.win_rate, 1)} />
                <Stat label="Net profit" value={fmt.num(m.net_profit)} tone={toneOf(m.net_profit)} />
                <Stat label="Profit factor" value={fmt.num(m.profit_factor)} />
                <Stat label="Max DD" value={fmt.pctRaw(m.max_drawdown_pct)} />
                <Stat label="Avg R" value={fmt.num(m.avg_r)} />
                <Stat label="Sharpe" value={fmt.num(m.sharpe)} /><Stat label="Sortino" value={fmt.num(m.sortino)} />
                <Stat label="Recovery factor" value={fmt.num(m.recovery_factor)} /><Stat label="Expectancy" value={fmt.num(m.expected_value)} />
                <Stat label="Max consec. wins" value={m.max_consecutive_wins} /><Stat label="Max consec. losses" value={m.max_consecutive_losses} />
              </div>
              {m.monte_carlo && <div className="mt-2 text-[11px]">Monte Carlo ({m.monte_carlo.runs} runs): max DD p50 {m.monte_carlo.max_drawdown_pct?.[50]}% · p95 {m.monte_carlo.max_drawdown_pct?.[95]}% · P(loss) {fmt.pct(m.monte_carlo.probability_of_loss, 1)}</div>}
              {m.windows && <Table rows={m.windows} cols={[{ k: "window", h: "Window" }, { k: "start", h: "Start" }, { k: "params", h: "Chosen params", r: (w) => JSON.stringify(w.params) },
                { k: "is", h: "IS PF", r: (w) => fmt.num(w.in_sample?.profit_factor) }, { k: "oos", h: "OOS PF", r: (w) => fmt.num(w.out_of_sample?.profit_factor) }]} />}
              {r.equity_curve?.length > 1 && <div className="mt-2 h-56"><ResponsiveContainer><LineChart data={r.equity_curve.map((e: any) => ({ t: String(e[0]).slice(0, 10), eq: e[1] }))}>
                <CartesianGrid stroke="#1a2329" /><XAxis dataKey="t" stroke="#6b7f8a" fontSize={10} minTickGap={40} /><YAxis stroke="#6b7f8a" fontSize={10} domain={["auto", "auto"]} />
                <Tooltip contentStyle={{ background: "#0a0d10", border: "1px solid #1a2329" }} /><Line dataKey="eq" stroke="#39ff88" dot={false} /></LineChart></ResponsiveContainer></div>}
              {Object.keys(m.monthly_returns || {}).length > 0 && <div className="mt-2 flex flex-wrap gap-1 text-[10px]">{Object.entries(m.monthly_returns).map(([k, v]: any) =>
                <span key={k} className={"rounded border px-1 " + (v >= 0 ? "border-neon/30 text-neon" : "border-bear/30 text-bear")}>{k} {v}%</span>)}</div>}
            </>}
          </div>
        );
      })}
      {job.optimisation?.length > 0 && <Table rows={job.optimisation} cols={[{ k: "rank", h: "#" }, { k: "params", h: "Params", r: (o) => JSON.stringify(o.params) },
        { k: "is", h: "IS PF / trades", r: (o) => `${fmt.num(o.in_sample.profit_factor)} / ${o.in_sample.total_trades}` },
        { k: "oos", h: "OOS PF / trades", r: (o) => <span className={o.out_of_sample.profit_factor >= 1 ? "text-neon" : "text-bear"}>{fmt.num(o.out_of_sample.profit_factor)} / {o.out_of_sample.total_trades}</span> },
        { k: "dd", h: "OOS DD", r: (o) => fmt.pctRaw(o.out_of_sample.max_drawdown_pct) }]} />}
    </div>
  );
}

export function BacktestForm({ defaultKind = "BACKTEST", kinds = KINDS, onCreated }: { defaultKind?: string; kinds?: string[]; onCreated: (id: number) => void }) {
  const { data: syms } = useApi<any[]>("/api/symbols");
  const { data: strats } = useApi<any[]>("/api/strategies");
  const [f, setF] = useState<any>({ kind: defaultKind, symbol: "", timeframe: "H1", strategies: "", bars: 5000, initial_balance: 10000, risk_pct: 1,
    sizing: "risk", spread_points: "", commission_per_lot: 0, slippage_points: 0, use_signal_engine: false, htf: "", grid: "", start: "", end: "" });
  const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  async function submit() {
    setBusy(true); setErr(null);
    try {
      const body: any = { ...f, strategies: f.strategies.split(",").map((s: string) => s.trim()).filter(Boolean), bars: +f.bars, initial_balance: +f.initial_balance,
        risk_pct: +f.risk_pct, commission_per_lot: +f.commission_per_lot, slippage_points: +f.slippage_points,
        spread_points: f.spread_points === "" ? null : +f.spread_points, htf: f.htf || null, grid: f.grid ? JSON.parse(f.grid) : null,
        start: f.start || null, end: f.end || null };
      const r = await post("/api/backtests", body); onCreated(r.id);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Field label="Test type"><select className={inputCls} value={f.kind} onChange={set("kind")}>{kinds.map((k) => <option key={k}>{k}</option>)}</select></Field>
        <Field label="Symbol"><select className={inputCls} value={f.symbol} onChange={set("symbol")}><option value="">choose…</option>{(syms || []).map((s) => <option key={s.id}>{s.symbol}</option>)}</select></Field>
        <Field label="Timeframe"><select className={inputCls} value={f.timeframe} onChange={set("timeframe")}>{["M5", "M15", "M30", "H1", "H4", "D1"].map((t) => <option key={t}>{t}</option>)}</select></Field>
        <Field label="Bars of broker history"><input className={inputCls} value={f.bars} onChange={set("bars")} /></Field>
        <Field label="Start (optional)"><input type="date" className={inputCls} value={f.start} onChange={set("start")} /></Field>
        <Field label="End (optional)"><input type="date" className={inputCls} value={f.end} onChange={set("end")} /></Field>
        <Field label="Initial balance"><input className={inputCls} value={f.initial_balance} onChange={set("initial_balance")} /></Field>
        <Field label="Risk %"><input className={inputCls} value={f.risk_pct} onChange={set("risk_pct")} /></Field>
        <Field label="Position sizing"><select className={inputCls} value={f.sizing} onChange={set("sizing")}><option value="risk">fixed fractional</option><option value="fixed">fixed lots</option></select></Field>
        <Field label="Spread pts (blank = broker history)"><input className={inputCls} value={f.spread_points} onChange={set("spread_points")} /></Field>
        <Field label="Commission / lot / side"><input className={inputCls} value={f.commission_per_lot} onChange={set("commission_per_lot")} /></Field>
        <Field label="Slippage pts"><input className={inputCls} value={f.slippage_points} onChange={set("slippage_points")} /></Field>
        <Field label="HTF filter"><select className={inputCls} value={f.htf} onChange={set("htf")}><option value="">none</option>{["H1", "H4", "D1"].map((t) => <option key={t}>{t}</option>)}</select></Field>
        <label className="flex items-center gap-2 self-end text-[11px] text-dim"><input type="checkbox" checked={f.use_signal_engine} onChange={set("use_signal_engine")} />Use signal engine (confluence/grade)</label>
      </div>
      <Field label="Strategies (comma-separated codes)"><input className={inputCls} list="strat-codes" value={f.strategies} onChange={set("strategies")} placeholder="smc_order_block, smc_fvg" /></Field>
      <datalist id="strat-codes">{(strats || []).map((s) => <option key={s.code} value={s.code}>{s.name}</option>)}</datalist>
      {(f.kind === "OPTIMISE" || f.kind === "WALKFORWARD") && <Field label='Parameter grid (JSON) e.g. {"fast":[10,20],"slow":[50,100]}'><input className={inputCls} value={f.grid} onChange={set("grid")} /></Field>}
      <ErrorLine error={err} />
      <Btn tone="neon" busy={busy} disabled={!f.symbol || !f.strategies} onClick={submit}><FlaskConical size={12} />Run</Btn>
    </div>
  );
}

