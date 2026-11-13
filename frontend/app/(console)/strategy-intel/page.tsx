"use client";
import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { PageTitle } from "@/components/Shell";
import { Empty, ErrorLine, inputCls, Panel, Stat, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function StrategyIntel() {
  const { data, error } = useApi<any[]>("/api/strategies");
  const { data: corr } = useApi<any>("/api/strategies-analytics/correlation");
  const { data: monthly } = useApi<any>("/api/strategies-analytics/monthly");
  const [cat, setCat] = useState(""); const [regime, setRegime] = useState(""); const [tf, setTf] = useState("");
  const all = data || [];
  const cats = useMemo(() => Array.from(new Set(all.map((s) => s.category))), [all]);
  const rows = all.filter((s) => (!cat || s.category === cat) && (!regime || !s.regimes.length || s.regimes.includes(regime)) && (!tf || s.timeframes.includes(tf)));
  const withPerf = rows.filter((s) => s.performance && s.performance.trades >= 30);
  const best = [...withPerf].sort((a, b) => (b.performance.profit_factor ?? 0) - (a.performance.profit_factor ?? 0)).slice(0, 8);
  const worst = [...withPerf].sort((a, b) => (a.performance.profit_factor ?? 0) - (b.performance.profit_factor ?? 0)).slice(0, 8);
  const months = monthly ? Object.entries(monthly).flatMap(([s, m]: any) => Object.entries(m).map(([k, v]) => ({ s, k, v }))) : [];
  const byMonth = Object.values(months.reduce((acc: any, x: any) => { acc[x.k] = acc[x.k] || { month: x.k, pnl: 0 }; acc[x.k].pnl += x.v; return acc; }, {})).sort((a: any, b: any) => a.month.localeCompare(b.month));
  const perfCols = [{ k: "name", h: "Strategy" }, { k: "trades", h: "Trades", r: (s: any) => s.performance.trades },
    { k: "wr", h: "Win rate", r: (s: any) => fmt.pct(s.performance.win_rate, 1) }, { k: "pf", h: "PF", r: (s: any) => fmt.num(s.performance.profit_factor) },
    { k: "r", h: "Avg R", r: (s: any) => fmt.num(s.performance.avg_r) }, { k: "dd", h: "Max DD", r: (s: any) => fmt.pctRaw(s.performance.max_drawdown) }];
  return (
    <div>
      <PageTitle sub="Performance comes only from out-of-sample, walk-forward, paper and live results; < 30 trades is treated as inconclusive.">Strategy Intelligence</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <Stat label="Total strategies" value={all.length} />
        <Stat label="Enabled" value={all.filter((s) => s.enabled).length} tone="up" />
        <Stat label="Disabled" value={all.filter((s) => !s.enabled).length} />
        <Stat label="Unavailable (data)" value={all.filter((s) => s.availability.startsWith("unavailable")).length} tone="warn" />
        <Stat label="In open positions" value={all.reduce((a, s) => a + s.active_positions, 0)} />
      </div>
      <Panel className="mb-3">
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          <select className={inputCls} value={cat} onChange={(e) => setCat(e.target.value)}><option value="">all categories</option>{cats.map((c) => <option key={c}>{c}</option>)}</select>
          <select className={inputCls} value={regime} onChange={(e) => setRegime(e.target.value)}><option value="">all regimes</option>
            {["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "SIDEWAYS", "CONSOLIDATION", "BREAKOUT", "REVERSAL", "HIGH_VOLATILITY", "LOW_VOLATILITY"].map((r) => <option key={r}>{r}</option>)}</select>
          <select className={inputCls} value={tf} onChange={(e) => setTf(e.target.value)}><option value="">all timeframes</option>{["M1", "M5", "M15", "M30", "H1", "H4", "D1"].map((t) => <option key={t}>{t}</option>)}</select>
          <div className="self-center text-[11px] text-dim">{rows.length} strategies match · {withPerf.length} with ≥30 validated trades</div>
        </div>
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Best performing (validated)" accent="neon"><Table rows={best} cols={perfCols} empty="No strategy has 30+ validated trades yet" /></Panel>
        <Panel title="Underperforming (validated)" accent="bear"><Table rows={worst} cols={perfCols} empty="No strategy has 30+ validated trades yet" /></Panel>
        <Panel title="Monthly realised P&L (strategy-attributed)">
          {byMonth.length ? <div className="h-56"><ResponsiveContainer><BarChart data={byMonth as any[]}>
            <CartesianGrid stroke="#1a2329" /><XAxis dataKey="month" stroke="#6b7f8a" fontSize={10} /><YAxis stroke="#6b7f8a" fontSize={10} />
            <Tooltip contentStyle={{ background: "#0a0d10", border: "1px solid #1a2329" }} /><Bar dataKey="pnl" fill="#22e4ff" /></BarChart></ResponsiveContainer></div>
            : <Empty>No closed trades attributed to strategies yet</Empty>}
        </Panel>
        <Panel title="Strategy correlation (daily realised P&L)">
          {corr?.strategies?.length ? (
            <div className="overflow-x-auto"><table className="text-[10px]"><thead><tr><th />{corr.strategies.map((s: string) => <th key={s} className="px-1 text-dim [writing-mode:vertical-rl]">{s}</th>)}</tr></thead>
              <tbody>{corr.matrix.map((row: number[], i: number) => <tr key={i}><td className="pr-2 text-dim">{corr.strategies[i]}</td>
                {row.map((v, j) => <td key={j} className="h-6 w-6 text-center" style={{ background: v > 0 ? `rgba(255,61,90,${Math.abs(v) * 0.6})` : `rgba(57,255,136,${Math.abs(v) * 0.6})` }}>{v.toFixed(1)}</td>)}</tr>)}</tbody></table></div>
          ) : <Empty>{corr?.note || "Insufficient data"}</Empty>}
        </Panel>
      </div>
    </div>
  );
}
