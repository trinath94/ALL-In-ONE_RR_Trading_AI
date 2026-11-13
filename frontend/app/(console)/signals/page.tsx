"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import TradingChart from "@/components/TradingChart";
import { Badge, Btn, Confirm, dirTone, ErrorLine, gradeTone, Panel, Table } from "@/components/ui";
import { api, fmt, post } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

export default function Signals() {
  const me = useMe();
  const live = useLive(["signal", "order"]);
  const { data: rows, error, reload } = useApi<any[]>("/api/signals?limit=300", 30000, live.tick);
  const { data: st } = useApi<any>("/api/control/state");
  const [sel, setSel] = useState<any>(null);
  const [tf, setTf] = useState("H1");
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function open(r: any) { setSel(await api(`/api/signals/${r.id}`)); setTf(r.timeframe); setMsg(null); setErr(null); }
  async function execute() {
    setBusy(true); setErr(null);
    try { const o = await post(`/api/signals/${sel.id}/execute`, { confirm_live: st?.mode === "LIVE" }); setMsg(`Order ${o.execution_id.slice(0, 10)}… ${o.status}: ${o.status_detail}`); reload(); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); setConfirm(false); }
  }
  return (
    <div>
      <PageTitle sub="Every signal shows its strategies, calibrated confidence (when statistically available), AI review and risk decisions.">Live Signals</PageTitle>
      <ErrorLine error={error} />
      <div className="grid gap-3 2xl:grid-cols-2">
        <Panel>
          <Table rows={rows} onRow={open} empty="No signals generated yet"
            cols={[{ k: "created_at", h: "Time", r: (r) => fmt.time(r.created_at) }, { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
              { k: "decision", h: "Dir", r: (r) => <Badge tone={dirTone(r.decision)}>{r.decision}</Badge> },
              { k: "grade", h: "Grade", r: (r) => <Badge tone={gradeTone(r.grade)}>{r.grade}</Badge> }, { k: "score", h: "Score" },
              { k: "calibrated_confidence", h: "Calibrated", r: (r) => r.calibrated_confidence === null ? <span className="text-dim" title={`${r.confidence_sample} OOS samples`}>n/a</span> : fmt.pct(r.calibrated_confidence, 0) },
              { k: "risk_reward", h: "R:R" }, { k: "ai", h: "AI", sort: false, r: (r) => r.ai_review ? <Badge tone={r.ai_review.decision === r.decision ? "neon" : "warn"}>{r.ai_review.decision}</Badge> : "—" },
              { k: "status", h: "Status" }]} />
        </Panel>
        {sel && (
          <Panel title={`${sel.symbol} ${sel.timeframe} · ${sel.decision} ${sel.grade}`}
            right={can(me, "trade.manual") && (sel.decision === "BUY" || sel.decision === "SELL") && sel.status === "NEW" &&
              <Btn tone={sel.decision === "BUY" ? "neon" : "bear"} onClick={() => setConfirm(true)}>Execute via Risk Engine</Btn>}>
            <TradingChart symbol={sel.symbol} timeframe={tf} onTimeframe={setTf} height={320}
              levels={{ entry: sel.entry, stop_loss: sel.stop_loss, tp1: sel.tp1, tp2: sel.tp2, tp3: sel.tp3, direction: sel.decision }} />
            <div className="mt-3 grid grid-cols-3 gap-2 text-[11px] sm:grid-cols-6">
              {[["Entry", sel.entry], ["Stop", sel.stop_loss], ["TP1", sel.tp1], ["TP2", sel.tp2], ["TP3", sel.tp3]].map(([k, v]) => (
                <div key={k}><div className="text-dim">{k}</div><div className="tabular">{fmt.px(v)}</div></div>))}
              <div><div className="text-dim">Expires</div><div>{fmt.time(sel.expires_at)}</div></div>
            </div>
            <p className="mt-2 text-[12px] leading-relaxed">{sel.explanation}</p>
            <div className="mt-2 text-[11px] text-dim">Confidence: {sel.calibrated_confidence === null ? `not reported — only ${sel.confidence_sample} out-of-sample outcomes (min 30)` : fmt.pct(sel.calibrated_confidence, 1) + ` (n=${sel.confidence_sample})`}</div>
            <div className="mt-2 flex flex-wrap gap-1">{sel.strategies.map((s: any, i: number) => <Badge key={i} tone="violet">{s.strategy}</Badge>)}</div>
            {sel.ai_review && (
              <div className="mt-3 rounded border border-violet/40 p-2 text-[11px]">
                <div className="mb-1 text-violet">Claude review: {sel.ai_review.decision}</div>
                {sel.ai_review.error ? <div className="text-warn">{sel.ai_review.error}</div> : <>
                  <div><span className="text-neon">Observations:</span> {sel.ai_review.observations?.join(" · ")}</div>
                  <div><span className="text-cyan">Hypotheses:</span> {sel.ai_review.hypotheses?.join(" · ")}</div>
                  <div><span className="text-warn">Conflicts:</span> {sel.ai_review.conflicts?.join(" · ") || "none"}</div>
                  <div><span className="text-bear">Invalidation:</span> {sel.ai_review.invalidation_conditions?.join(" · ")}</div>
                  <p className="mt-1 text-dim">{sel.ai_review.explanation}</p></>}
              </div>
            )}
            {sel.risk_evaluations?.map((e: any, i: number) => (
              <div key={i} className="mt-2 rounded border border-line p-2 text-[11px]">
                <Badge tone={e.approved ? "neon" : "bear"}>{e.approved ? `approved ${e.volume} lots (${fmt.num(e.risk_percent, 3)}%)` : "risk rejected"}</Badge>
                {!e.approved && <ul className="mt-1 list-disc pl-4 text-bear">{e.reasons.map((x: string, j: number) => <li key={j}>{x}</li>)}</ul>}
              </div>
            ))}
            <div className="mt-2"><ErrorLine error={err} />{msg && <div className="text-[11px] text-neon">{msg}</div>}</div>
            <Confirm open={confirm} title={`${st?.mode} order: ${sel.decision} ${sel.symbol}`} tone={st?.mode === "LIVE" ? "bear" : "neon"} busy={busy}
              phrase={st?.mode === "LIVE" ? "CONFIRM LIVE" : undefined}
              body={<>The Risk Engine will size the position from live broker specifications and may reject it. The order counts only once MT5 confirms it.</>}
              onCancel={() => setConfirm(false)} onConfirm={execute} />
          </Panel>
        )}
      </div>
    </div>
  );
}
