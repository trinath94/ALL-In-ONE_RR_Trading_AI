"use client";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { ci, DriftPanel, JobsPanel, ModelsPanel, MonthlyLine, RecommendationsPanel, ResearchLab, STAGE_TONE, Verdict } from "@/components/Intel";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, inputCls, Panel, Stat, Table, toneOf } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const DIMS = ["strategy", "combination", "symbol", "timeframe", "session", "regime", "volatility", "entry_method", "sl_method", "tp_method", "model_version", "grade", "spread_bucket"];

export default function Learning() {
  const me = useMe();
  const [mode, setMode] = useState("DEMO");
  const [dim, setDim] = useState("strategy");
  const { data: ov, error, reload } = useApi<any>(`/api/learning/overview?mode=${mode}`, 30000);
  const { data: pf } = useApi<any>(`/api/learning/performance?mode=${mode}&dimension=${dim}`, 60000);
  const { data: mem } = useApi<any[]>(`/api/learning/memory?mode=${mode}`, 60000);
  const o = ov?.overall || {};
  const rows = Object.entries(pf?.dimensions?.[dim] || {}).map(([k, m]: any) => ({ key: k, ...m }));
  return (
    <div>
      <PageTitle sub="Learns only from actual MT5-confirmed trade outcomes (net of costs). Verdicts require ≥30 trades and a confidence interval that excludes zero."
        right={<div className="flex items-center gap-2">
          {["DEMO", "LIVE"].map((m) => <Btn key={m} small tone={m === mode ? (m === "LIVE" ? "bear" : "cyan") : "dim"} onClick={() => setMode(m)}>{m}</Btn>)}
          {can(me, "trade.control") && <Btn small onClick={async () => { await post("/api/learning/jobs/trade_sync/run"); reload(); }}><RefreshCw size={11} />Sync MT5 history</Btn>}</div>}>
        AI Learning & Strategy Evolution</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Trades analysed" value={ov?.trades_analysed ?? "—"} sub={`${ov?.features_enriched ?? 0} enriched`} />
        <Stat label="Wins / losses" value={`${ov?.wins ?? 0} / ${ov?.losses ?? 0}`} />
        <Stat label="Verdict" value={<Verdict v={o.verdict} />} sub={`n=${o.sample_size ?? 0}`} />
        <Stat label="Expectancy" value={fmt.num(o.expectancy)} sub={`95% ${ci(o.expectancy_ci95)}`} tone={toneOf(o.expectancy)} />
        <Stat label="Win rate" value={fmt.pct(o.win_rate, 1)} sub={`95% ${ci(o.win_rate_ci95, 3)}`} />
        <Stat label="Profit factor" value={fmt.num(o.profit_factor)} sub={`95% ${ci(o.profit_factor_ci95)}`} />
        <Stat label="Pending approvals" value={(ov?.pending_approvals?.experiments ?? 0) + (ov?.pending_approvals?.models ?? 0)} tone="warn" />
        <Stat label="Drift alerts 7d" value={ov?.drift_alerts_7d ?? "—"} tone={ov?.drift_alerts_7d ? "down" : "plain"} />
      </div>
      <div className="mb-3 flex flex-wrap gap-2 text-[11px]">
        {Object.entries(ov?.experiments_by_stage || {}).map(([k, v]: any) => <Badge key={k} tone={STAGE_TONE[k]}>{k} {v}</Badge>)}
        {Object.entries(ov?.models_by_status || {}).map(([k, v]: any) => <Badge key={k} tone="cyan">model {k} {v}</Badge>)}
        <Badge tone="dim">backtest queue {ov?.backtest_queue ?? 0}</Badge><Badge tone="dim">consensus {ov?.consensus_mode}</Badge>
        {ov?.sync?.map((s: any) => <Badge key={s.account_id} tone={s.status === "OK" ? "neon" : "bear"}>sync acct {s.account_id}: {s.status} {s.last_time ? fmt.time(s.last_time) : ""}</Badge>)}
      </div>
      <div className="grid gap-3 xl:grid-cols-[1fr_380px]">
        <Panel title="Performance by dimension" right={<select className={inputCls + " w-40"} value={dim} onChange={(e) => setDim(e.target.value)}>{DIMS.map((d) => <option key={d}>{d}</option>)}</select>}>
          <Table rows={rows} empty="No closed trades in this dimension yet" rowKey={(r) => r.key}
            cols={[{ k: "key", h: dim }, { k: "sample_size", h: "n" }, { k: "verdict", h: "Verdict", r: (r) => <Verdict v={r.verdict} /> },
              { k: "win_rate", h: "Win", r: (r) => fmt.pct(r.win_rate, 1) }, { k: "wr_ci", h: "Win 95% CI", sort: false, r: (r) => ci(r.win_rate_ci95, 3) },
              { k: "expectancy", h: "Expectancy", r: (r) => <span className={r.expectancy > 0 ? "text-neon" : r.expectancy < 0 ? "text-bear" : ""}>{fmt.num(r.expectancy)}</span> },
              { k: "ex_ci", h: "Exp 95% CI", sort: false, r: (r) => ci(r.expectancy_ci95) }, { k: "profit_factor", h: "PF", r: (r) => fmt.num(r.profit_factor) },
              { k: "average_r", h: "Avg R", r: (r) => fmt.num(r.average_r) }, { k: "max_drawdown", h: "Max DD", r: (r) => fmt.num(r.max_drawdown) },
              { k: "avg_mae_r", h: "MAE R", r: (r) => fmt.num(r.avg_mae_r) }, { k: "avg_mfe_r", h: "MFE R", r: (r) => fmt.num(r.avg_mfe_r) }]} />
          <div className="mt-3 text-[10px] uppercase text-dim">Monthly net (all trades, {mode})</div><MonthlyLine monthly={o.monthly} />
        </Panel>
        <Panel title="Learning memory">
          <div className="max-h-[520px] space-y-1 overflow-y-auto">
            {mem?.length ? mem.map((m, i) => <div key={i} className="rounded border border-line p-1.5 text-[11px]"><Badge tone={m.kind === "strength" ? "neon" : "warn"}>{m.kind}</Badge> <span className="text-cyan">{m.key}</span>
              <div className="text-dim">{m.summary} · evidence {m.evidence_count}</div></div>) : <div className="py-4 text-center text-[12px] text-dim">No learned patterns yet</div>}
          </div>
        </Panel>
      </div>
      <div className="mt-3 space-y-3"><ResearchLab /><ModelsPanel /><div className="grid gap-3 xl:grid-cols-2"><DriftPanel /><RecommendationsPanel /></div><JobsPanel /></div>
    </div>
  );
}
