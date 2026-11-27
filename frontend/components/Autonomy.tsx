"use client";
import { useState } from "react";
import { Badge, Btn, Field, inputCls, Panel, Table } from "@/components/ui";
import { fmt, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const REVIEW: Record<string, string> = {
  off: "Off — the engine decides every candidate (no AI call)",
  selective: "Selective — only candidates the engine is uncertain about, capped per day",
  all: "All — every shortlisted candidate (previous behaviour, most tokens)",
};

/** Engine autonomy: proof that the engine runs trading itself, what the Anthropic API is still used for, the cost,
 *  the improvement proposals, and the engine-first settings. */
export function Autonomy({ admin }: { admin: boolean }) {
  const [days, setDays] = useState(1);
  const { data: a, reload } = useApi<any>(`/api/ai/autonomy?days=${days}`, 60000);
  const [msg, setMsg] = useState("");
  async function save(patch: any) {
    setMsg("");
    try { await put("/api/ai/engine-first", patch); setMsg("Saved."); reload(); } catch (e: any) { setMsg(e.message); }
  }
  if (!a) return <Panel title="Engine autonomy"><div className="text-[12px] text-dim">Loading…</div></Panel>;
  const i = a.internal, ai = a.ai, ind = a.independence, p = a.policy, b = a.budget;
  const share = i.engine_decision_share == null ? "—" : `${Math.round(i.engine_decision_share * 100)} %`;
  return (
    <Panel title="Engine autonomy" right={<select className="rounded border border-line bg-transparent px-1 text-[11px]" value={days} onChange={(e) => setDays(Number(e.target.value))}>
      <option value={1}>last 24 h</option><option value={7}>last 7 days</option><option value={30}>last 30 days</option></select>}>
      <div className="space-y-3 text-[12px]">
        <div className={`rounded border p-2 ${ind.routine_dependencies.length ? "border-warn/50" : "border-neon/50"}`}>
          <div className={ind.routine_dependencies.length ? "font-bold text-warn" : "font-bold text-neon"}>
            {ind.routine_dependencies.length ? "Routine operation still uses the AI" : "The engine runs routine trading by itself"}</div>
          <div className="text-dim">{ind.note}</div>
          {ind.routine_dependencies.map((d: string) => <div key={d} className="text-warn">• {d}</div>)}
          <div className="mt-1">Trading without the AI: {ind.can_trade_without_ai ? <Badge tone="neon">possible</Badge> : <Badge tone="bear">not possible — candidates wait for the AI</Badge>}</div>
        </div>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          {[["Scan cycles", i.scan_cycles], ["Analyses", i.analyses], ["Signals", i.signals], ["Orders", i.orders],
            ["Decided by the engine", i.engine_decided_candidates], ["Reviewed by the AI", i.ai_reviewed_candidates],
            ["Engine decision share", share], ["Learner jobs", i.learner_jobs]].map(([k, v]) => (
            <div key={k as string} className="rounded border border-line p-2"><div className="text-[10px] uppercase text-dim">{k}</div><div className="text-[15px] font-bold">{v as any}</div></div>))}
        </div>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          {[["Routine AI calls", ai.routine], ["Playbook AI calls", ai.playbook || { calls: 0, input_tokens: 0, output_tokens: 0, cost: 0 }], ["Improvement AI calls", ai.improvement], ["All AI calls", ai.total]].map(([k, t]: any) => (
            <div key={k} className="rounded border border-line p-2"><div className="text-[10px] uppercase text-dim">{k}</div>
              <div className="text-[15px] font-bold">{t.calls}</div>
              <div className="text-[10px] text-dim">{t.input_tokens.toLocaleString()} in / {t.output_tokens.toLocaleString()} out tokens · {t.cost ? `${t.cost} USD` : "no price set"}</div></div>))}
        </div>
        <div className="text-[11px] text-dim">Budget: today {b.spend_today_usd} USD{b.daily_budget_usd != null ? ` of ${b.daily_budget_usd}` : ""} · month {b.spend_month_usd} USD{b.monthly_budget_usd != null ? ` of ${b.monthly_budget_usd}` : ""}{b.exhausted ? ` · ${b.exhausted}` : b.alert ? ` · alert: ${b.alert}` : ""} · Claude Code tasks: {a.claude_code.tasks} ({a.claude_code.cost_usd} USD)</div>
        {ai.by_task.length > 0 && <Table rows={ai.by_task} rowKey={(r: any) => r.task}
          cols={[{ k: "task", h: "AI task" }, { k: "kind", h: "Kind", r: (r: any) => <Badge tone={r.kind === "routine" ? "warn" : r.kind === "playbook" ? "neon" : "cyan"}>{r.kind}</Badge> },
            { k: "calls", h: "Calls" }, { k: "input_tokens", h: "Tokens in / out", r: (r: any) => `${r.input_tokens} / ${r.output_tokens}` },
            { k: "cost", h: "Cost", r: (r: any) => r.cost == null ? <span className="text-dim">no price</span> : r.cost }]} />}
        <div>
          <div className="mb-1 text-[10px] uppercase text-dim">Improvement proposals (Anthropic → validated by the engine)</div>
          <Table rows={a.improvements} empty="No AI improvement proposals yet. The engine requests one when its own statistics find a weak strategy, or ask in the Engine Assistant." rowKey={(r: any) => r.id}
            cols={[{ k: "strategy", h: "Strategy" }, { k: "version", h: "Version" }, { k: "trigger", h: "Requested by", r: (r: any) => r.trigger || "user" },
              { k: "status", h: "Status", r: (r: any) => <Badge tone={r.status === "ACTIVE" || r.status === "DEMO_ACTIVE" ? "neon" : r.status === "REJECTED" ? "bear" : "dim"}>{r.status}</Badge> },
              { k: "validated", h: "Validation", r: (r: any) => r.validated == null ? <span className="text-dim">pending</span> : r.validated ? <span className="text-neon">passed</span> : <span className="text-bear">failed</span> },
              { k: "created_at", h: "Created", r: (r: any) => fmt.time(r.created_at) }]} />
        </div>
        {admin && <div className="grid grid-cols-2 gap-2 rounded border border-line p-2">
          <div className="col-span-2 text-[10px] uppercase text-dim">Engine-first settings</div>
          <div className="col-span-2"><Field label="AI review of scan candidates"><select className={inputCls} value={p.candidate_review} onChange={(e) => save({ candidate_review: e.target.value })}>
            {Object.entries(REVIEW).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field></div>
          {p.candidate_review === "selective" && <>
            <Field label="Review only below engine confidence"><input type="number" step="0.05" min="0" max="1" className={inputCls} defaultValue={p.review_confidence_below} onBlur={(e) => save({ review_confidence_below: Number(e.target.value) })} /></Field>
            <Field label="Max AI reviews per day"><input type="number" min="0" className={inputCls} defaultValue={p.review_daily_cap} onBlur={(e) => save({ review_daily_cap: Number(e.target.value) })} /></Field></>}
          <label className="flex items-center gap-2"><input type="checkbox" checked={p.auto_improvement} onChange={(e) => save({ auto_improvement: e.target.checked })} />Automatic improvement research for weak strategies</label>
          <Field label="Improvement proposals per day"><input type="number" min="0" max="20" className={inputCls} defaultValue={p.improvement_daily_cap} onBlur={(e) => save({ improvement_daily_cap: Number(e.target.value) })} /></Field>
          <label className="col-span-2 flex items-center gap-2"><input type="checkbox" checked={p.assistant_ai} onChange={(e) => save({ assistant_ai: e.target.checked })} />Engine Assistant phrases answers with the AI (off: the engine answers from its records; analyses you ask for still use the AI)</label>
          {msg && <div className="col-span-2 text-[11px] text-dim">{msg}</div>}
        </div>}
      </div>
    </Panel>
  );
}
