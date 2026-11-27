"use client";
import { useState } from "react";
import { Badge, Field, inputCls, Panel, Table } from "@/components/ui";
import { fmt, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const POSTURE: Record<string, any> = { normal: "neon", reduced: "warn", stand_aside: "bear" };

/** Market playbooks: the AI's trading plan per symbol and regime, saved and applied by the engine to every candidate
 *  without another AI call; reused when it proved profitable; graded from the trades it governed. */
export function Playbooks({ admin }: { admin: boolean }) {
  const { data, reload } = useApi<any>("/api/ai/playbooks", 60000);
  const { data: a, reload: reloadA } = useApi<any>("/api/ai/autonomy?days=1", 60000);
  const [msg, setMsg] = useState("");
  const p = a?.policy;
  async function save(patch: any) {
    setMsg("");
    try { await put("/api/ai/engine-first", patch); setMsg("Saved."); reloadA(); reload(); } catch (e: any) { setMsg(e.message); }
  }
  return (
    <Panel title="Market playbooks (trader plans, saved and reused)">
      <div className="space-y-2 text-[12px]">
        <div className="text-dim">For each symbol and market regime the AI writes one plan like a professional trader: bias, directions to trade, strategy families that suit the market, conditions to avoid and risk posture. The engine applies it to every signal itself. A plan can only hold back a trade or reduce size; it never forces a trade or bypasses the Risk Engine. Plans that made money are reused for the same symbol and regime without asking the AI again.</div>
        {data && <div className="text-[11px]">Last 24 h: <b>{data.today.ai}</b> new plans from the AI, <b>{data.today.reused}</b> reused from proven plans (no AI call).</div>}
        <Table rows={data?.playbooks} empty="No playbook yet. One is made the first time a symbol has a trade candidate (needs an Anthropic API key)." rowKey={(r: any) => r.id}
          cols={[{ k: "symbol", h: "Symbol" }, { k: "regime", h: "Regime" },
            { k: "bias", h: "Plan", r: (r: any) => <span><Badge tone={r.bias === "BUY" ? "neon" : r.bias === "SELL" ? "bear" : "dim"}>{r.bias}</Badge> <span className="text-dim">{r.allowed_directions.join("/") || "none"}</span></span> },
            { k: "risk_posture", h: "Posture", r: (r: any) => <Badge tone={POSTURE[r.risk_posture] || "dim"}>{r.risk_posture.replace("_", " ")} ×{r.size_multiplier}</Badge> },
            { k: "reasoning", h: "Trader's reasoning", sort: false, cls: "whitespace-normal", r: (r: any) => <div className="max-w-[420px] text-[11px]">{r.reasoning}{r.avoid?.length ? <div className="text-warn">Avoid: {r.avoid.join("; ")}</div> : null}</div> },
            { k: "source", h: "From", r: (r: any) => r.source === "ai" ? <Badge tone="cyan">AI</Badge> : <Badge tone="neon">reused #{r.reused_from}</Badge> },
            { k: "status", h: "Status", r: (r: any) => <span>{r.status}<div className="text-[10px] text-dim">until {fmt.time(r.valid_until)}</div></span> },
            { k: "outcomes", h: "Results", sort: false, r: (r: any) => r.outcomes?.trades ? <span>{r.outcomes.trades} trades, {r.outcomes.wins} won, net {r.outcomes.net}</span> : <span className="text-dim">no trades yet</span> }]} />
        {admin && p && <div className="grid grid-cols-2 gap-2 rounded border border-line p-2 md:grid-cols-4">
          <label className="col-span-2 flex items-center gap-2 md:col-span-4"><input type="checkbox" checked={p.playbook_enabled} onChange={(e) => save({ playbook_enabled: e.target.checked })} />Use market playbooks</label>
          <Field label="Plan valid for (hours)"><input type="number" min="1" max="168" className={inputCls} defaultValue={p.playbook_hours} onBlur={(e) => save({ playbook_hours: Number(e.target.value) })} /></Field>
          <Field label="Max new AI plans per day"><input type="number" min="0" className={inputCls} defaultValue={p.playbook_daily_cap} onBlur={(e) => save({ playbook_daily_cap: Number(e.target.value) })} /></Field>
          <Field label="Trades before a plan can be reused"><input type="number" min="1" className={inputCls} defaultValue={p.playbook_reuse_min_trades} onBlur={(e) => save({ playbook_reuse_min_trades: Number(e.target.value) })} /></Field>
          {msg && <div className="self-end text-[11px] text-dim">{msg}</div>}
        </div>}
      </div>
    </Panel>
  );
}
