"use client";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { PageTitle } from "@/components/Shell";
import { Badge, Empty, ErrorLine, Panel, Stat } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

const tip = { background: "#0a0d10", border: "1px solid #1a2329" };

export default function Portfolio() {
  const live = useLive(["positions", "account"]);
  const { data: p, error } = useApi<any>("/api/portfolio", 15000, live.tick);
  const { data: eq } = useApi<any[]>("/api/portfolio/equity?days=60", 60000);
  const { data: sum } = useApi<any>("/api/dashboard/summary", 30000, live.tick);
  const ccy = Object.entries(p?.net_currency_exposure || {}).map(([k, v]) => ({ k, v }));
  const conc = Object.entries(p?.symbol_concentration || {}).map(([k, v]: any) => ({ k, v: v * 100 }));
  const perf = sum?.performance;
  return (
    <div>
      <PageTitle sub="Exposure in account currency, derived from live MT5 positions and broker tick values">Portfolio Analytics</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Total exposure" value={fmt.num(p?.total_exposure, 0)} />
        <Stat label="Long" value={fmt.num(p?.long_exposure, 0)} tone="up" />
        <Stat label="Short" value={fmt.num(p?.short_exposure, 0)} tone="down" />
        <Stat label="Leverage" value={p?.leverage !== undefined && p?.leverage !== null ? `${fmt.num(p.leverage)}x` : "—"} />
        <Stat label="Open risk" value={fmt.pctRaw(p?.open_risk_pct, 2)} sub={fmt.num(p?.open_risk)} />
        <Stat label="Margin used" value={fmt.pctRaw(p?.margin_utilisation)} />
        <Stat label="Daily / weekly" value={`${fmt.num(perf?.today_pnl)} / ${fmt.num(perf?.week_pnl)}`} />
        <Stat label="Monthly" value={fmt.num(perf?.month_pnl)} />
      </div>
      {p?.warnings?.map((w: string) => <div key={w} className="mb-2"><Badge tone="warn">Concentration: {w}</Badge></div>)}
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Equity & balance (60d)">{eq?.length ? <div className="h-64"><ResponsiveContainer><AreaChart data={eq.map((e) => ({ ...e, t: String(e.t).slice(5, 16) }))}>
          <CartesianGrid stroke="#1a2329" /><XAxis dataKey="t" stroke="#6b7f8a" fontSize={10} minTickGap={50} /><YAxis stroke="#6b7f8a" fontSize={10} domain={["auto", "auto"]} />
          <Tooltip contentStyle={tip} /><Area dataKey="equity" stroke="#39ff88" fill="rgba(57,255,136,.08)" /><Area dataKey="balance" stroke="#22e4ff" fill="transparent" /></AreaChart></ResponsiveContainer></div>
          : <Empty>No account snapshots yet</Empty>}</Panel>
        <Panel title="Net currency exposure">{ccy.length ? <div className="h-64"><ResponsiveContainer><BarChart data={ccy}><CartesianGrid stroke="#1a2329" />
          <XAxis dataKey="k" stroke="#6b7f8a" fontSize={10} /><YAxis stroke="#6b7f8a" fontSize={10} /><Tooltip contentStyle={tip} /><Bar dataKey="v" fill="#b45cff" /></BarChart></ResponsiveContainer></div>
          : <Empty>No open exposure</Empty>}</Panel>
        <Panel title="Symbol concentration (% of notional)">{conc.length ? <div className="h-56"><ResponsiveContainer><BarChart data={conc} layout="vertical"><CartesianGrid stroke="#1a2329" />
          <XAxis type="number" stroke="#6b7f8a" fontSize={10} /><YAxis type="category" dataKey="k" stroke="#6b7f8a" fontSize={10} width={90} /><Tooltip contentStyle={tip} />
          <Bar dataKey="v" fill="#22e4ff" /></BarChart></ResponsiveContainer></div> : <Empty>No open positions</Empty>}</Panel>
        <Panel title="Drawdown & risk"><div className="grid grid-cols-2 gap-2">
          <Stat label="Max drawdown (equity snapshots)" value={fmt.pctRaw(perf?.max_drawdown_pct)} /><Stat label="Win rate" value={fmt.pct(perf?.win_rate, 1)} />
          <Stat label="Profit factor" value={fmt.num(perf?.profit_factor)} /><Stat label="Closed trades" value={perf?.total_trades ?? "—"} /></div></Panel>
      </div>
    </div>
  );
}
