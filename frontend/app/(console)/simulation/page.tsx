"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, inputCls, Panel, Table } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const OUT: Record<string, any> = { TARGET: "neon", STOP: "bear", STOP_GAP: "bear", TIME: "warn", OPEN_AT_END: "dim",
  NOT_FILLED: "dim", INVALID: "bear", NO_DATA: "dim" };
const r = (v: any) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v} R`);

/** Parts 30.5 / 30.6: scenario stress tests and counterfactual alternatives on the bars that followed an entry.
 *  Research only: nothing here can trade, and no decision reads these results. */
export default function Simulation() {
  const me = useMe();
  const allowed = can(me, "backtest.run");
  const { data: runs, reload } = useApi<any[]>("/api/simulations?limit=50", 0);
  const { data: opps } = useApi<any[]>("/api/opportunities?limit=50", 0);
  const [kind, setKind] = useState("scenario");
  const [src, setSrc] = useState("opportunity");
  const [id, setId] = useState("");
  const [sel, setSel] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() {
    setErr(null); setBusy(true);
    try {
      const body: any = { kind };
      body[src === "opportunity" ? "opportunity_id" : "closed_position_id"] = Number(id);
      const out = await post("/api/simulations", body);
      setSel(out); await reload();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const rows = sel ? (sel.kind === "scenario" ? sel.result.rows : sel.result.alternatives) : null;
  return (
    <div>
      <PageTitle sub="Stress-test a setup (continuation, reversal, double volatility, spread and slippage shocks, a gap through the stop) or ask what other stops, targets, exit timing and limit entries would have done on the bars that actually followed. Results are in R and are research only. Counterfactuals are hindsight and are labelled as such.">
        Simulation Laboratory</PageTitle>
      <ErrorLine error={err} />
      {allowed ? (
        <Panel title="Run">
          <div className="flex flex-wrap items-end gap-2 text-[11px]">
            <div className="w-40"><select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="scenario">Scenario stress test</option><option value="counterfactual">Counterfactual alternatives</option></select></div>
            <div className="w-40"><select className={inputCls} value={src} onChange={(e) => setSrc(e.target.value)}>
              <option value="opportunity">Opportunity</option><option value="closed">Closed position</option></select></div>
            {src === "opportunity" && opps?.length ? (
              <div className="w-72"><select className={inputCls} value={id} onChange={(e) => setId(e.target.value)}>
                <option value="">choose…</option>
                {opps.filter((o) => o.signal_id).map((o) => <option key={o.id} value={o.id}>#{o.id} {o.symbol} {o.timeframe} {o.direction} {o.state.toLowerCase()}</option>)}</select></div>
            ) : <div className="w-40"><input className={inputCls} placeholder="id" value={id} onChange={(e) => setId(e.target.value)} /></div>}
            <Btn busy={busy} disabled={!id} onClick={run}>Run</Btn>
          </div>
        </Panel>
      ) : <div className="text-[11px] text-dim">Running simulations needs the backtest permission.</div>}
      {sel && (
        <Panel title={`${sel.kind} · ${sel.symbol} ${sel.timeframe} · ${sel.setup.direction} entry ${sel.setup.entry} stop ${sel.setup.stop} target ${sel.setup.target} · ${sel.bars} bars after entry`} className="mt-3">
          {sel.kind === "counterfactual" && <div className="mb-2 text-[11px]">Actual: <Badge tone={OUT[sel.result.actual.outcome]}>{sel.result.actual.outcome}</Badge> {r(sel.result.actual.r)} ·
            best in hindsight: {sel.result.best_in_hindsight ? `${sel.result.best_in_hindsight.kind} ${r(sel.result.best_in_hindsight.r)}` : "—"} <span className="text-warn">({sel.result.note})</span></div>}
          <Table rows={rows} rowKey={(x) => JSON.stringify(x)}
            cols={sel.kind === "scenario"
              ? [{ k: "scenario", h: "Scenario" }, { k: "description", h: "What changes", sort: false, r: (x) => <span className="text-dim">{x.description}</span> },
                 { k: "outcome", h: "Outcome", r: (x) => <Badge tone={OUT[x.outcome]}>{x.outcome}</Badge> }, { k: "r", h: "Result", r: (x) => r(x.r) },
                 { k: "bars", h: "Bars" }, { k: "mae_r", h: "MAE", r: (x) => r(x.mae_r) }]
              : [{ k: "kind", h: "Alternative" }, { k: "detail", h: "Setting", sort: false, r: (x) => x.kind === "stop_target" ? `stop ×${x.stop_x}, target ${x.target_r} R` : x.kind === "exit_timing" ? `exit after ${x.max_bars} bars` : `limit ${x.better_r} R better${x.filled_after_bars != null ? `, filled after ${x.filled_after_bars} bars` : ""}` },
                 { k: "outcome", h: "Outcome", r: (x) => <Badge tone={OUT[x.outcome]}>{x.outcome}</Badge> }, { k: "r", h: "Result (original R)", r: (x) => r(x.r) }]} />
        </Panel>)}
      <Panel title="Recent runs" className="mt-3">
        <Table rows={runs} onRow={setSel} rowKey={(x) => x.id} empty="No simulations yet."
          cols={[{ k: "created_at", h: "When", r: (x) => fmt.time(x.created_at) }, { k: "kind", h: "Kind" },
            { k: "subject", h: "Subject", sort: false, r: (x) => `${x.subject.type.replace("_", " ")} #${x.subject.id}` },
            { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" }, { k: "bars", h: "Bars" }]} />
      </Panel>
    </div>
  );
}
