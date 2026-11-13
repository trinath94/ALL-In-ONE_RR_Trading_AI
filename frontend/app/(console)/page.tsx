"use client";
import { CandlestickChart, Radar } from "lucide-react";
import { useEffect, useState } from "react";
import AiPanel from "@/components/AiPanel";
import ControlCenter from "@/components/ControlCenter";
import { PageTitle } from "@/components/Shell";
import TradingChart from "@/components/TradingChart";
import { Badge, dirTone, Empty, ErrorLine, gradeTone, inputCls, Panel, Stat, Table, toneOf } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

export default function CommandCenter() {
  const live = useLive(["account", "positions", "signal", "order"]);
  const { data: sum, error } = useApi<any>("/api/dashboard/summary", 15000, live.tick);
  const { data: syms } = useApi<any[]>("/api/symbols");
  const { data: scan } = useApi<any[]>("/api/scanner", 30000, live.tick);
  const [symbol, setSymbol] = useState("");
  const [tf, setTf] = useState("H1");
  useEffect(() => { if (!symbol && syms?.length) setSymbol((syms.find((s) => s.scan_enabled) || syms[0]).symbol); }, [syms, symbol]);
  const a = sum?.account, p = sum?.performance;
  const top = (scan || []).filter((r) => r.decision === "BUY" || r.decision === "SELL").slice(0, 8);
  const sel = (scan || []).find((r) => r.symbol === symbol && r.timeframe === tf && (r.decision === "BUY" || r.decision === "SELL"));
  return (
    <div>
      <PageTitle sub={a ? `Account ${a.login} @ ${a.server} · data as of ${fmt.time(a.as_of)}` : "No synchronised account yet"}>Command Center</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
        <Stat label="Balance" value={fmt.num(a?.balance)} sub={a?.currency} />
        <Stat label="Equity" value={fmt.num(a?.equity)} tone={a ? toneOf(a.equity - a.balance) : "plain"} />
        <Stat label="Free margin" value={fmt.num(a?.free_margin)} />
        <Stat label="Used margin" value={fmt.num(a?.margin)} />
        <Stat label="Floating P&L" value={fmt.num(a?.floating_pnl)} tone={toneOf(a?.floating_pnl)} />
        <Stat label="Today realised" value={fmt.num(p?.today_pnl)} tone={toneOf(p?.today_pnl)} />
        <Stat label="Week P&L" value={fmt.num(p?.week_pnl)} tone={toneOf(p?.week_pnl)} />
        <Stat label="Month P&L" value={fmt.num(p?.month_pnl)} tone={toneOf(p?.month_pnl)} />
        <Stat label="Win rate" value={fmt.pct(p?.win_rate, 1)} sub={`${p?.total_trades ?? 0} trades`} />
        <Stat label="Profit factor" value={fmt.num(p?.profit_factor)} />
        <Stat label="Active positions" value={p?.active_positions ?? "—"} />
        <Stat label="Max drawdown" value={fmt.pctRaw(p?.max_drawdown_pct)} tone={p?.max_drawdown_pct > 5 ? "down" : "plain"} />
        <Stat label="Account risk" value={fmt.pctRaw(p?.open_risk_pct, 2)} sub="open risk to stops" tone={p?.open_risk_pct > 2 ? "warn" : "plain"} />
        <Stat label="Leverage" value={a?.leverage ? `1:${a.leverage}` : "—"} />
      </div>
      <div className="grid gap-3 xl:grid-cols-[1fr_340px]">
        <div className="space-y-3 min-w-0">
          <Panel title="Market workspace" icon={<CandlestickChart size={12} />} right={
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className={inputCls + " w-44"}>
              {(syms || []).map((s) => <option key={s.id} value={s.symbol}>{s.symbol} · {s.category}</option>)}
            </select>}>
            {symbol ? <TradingChart symbol={symbol} timeframe={tf} onTimeframe={setTf}
              levels={sel ? { entry: sel.entry, stop_loss: sel.stop_loss, tp2: sel.take_profit, direction: sel.direction } : undefined} />
              : <Empty>No broker symbols discovered yet. Configure an account and run symbol discovery (Settings).</Empty>}
            {sel && <div className="mt-2 flex flex-wrap gap-2 text-[11px]"><Badge tone={dirTone(sel.direction)}>{sel.direction}</Badge>
              <Badge tone={gradeTone(sel.grade)}>{sel.grade}</Badge><span>score {sel.score}</span><span className="text-dim">regime {sel.regime}</span>
              <span className="text-dim">{sel.strategies.join(", ")}</span></div>}
          </Panel>
          <Panel title="Top qualified opportunities" icon={<Radar size={12} />}>
            <Table rows={top} empty="No BUY/SELL candidates in the latest scan (NO TRADE is a valid outcome)." onRow={(r) => { setSymbol(r.symbol); setTf(r.timeframe); }}
              cols={[{ k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
                { k: "direction", h: "Signal", r: (r) => <Badge tone={dirTone(r.direction)}>{r.direction}</Badge> },
                { k: "grade", h: "Grade", r: (r) => <Badge tone={gradeTone(r.grade)}>{r.grade}</Badge> },
                { k: "score", h: "AI score" }, { k: "entry", h: "Entry", r: (r) => fmt.px(r.entry) },
                { k: "stop_loss", h: "SL", r: (r) => fmt.px(r.stop_loss) }, { k: "take_profit", h: "TP", r: (r) => fmt.px(r.take_profit) },
                { k: "risk_reward", h: "R:R" }, { k: "regime", h: "Regime" }]} />
          </Panel>
        </div>
        <div className="space-y-3"><ControlCenter /><AiPanel /></div>
      </div>
    </div>
  );
}
