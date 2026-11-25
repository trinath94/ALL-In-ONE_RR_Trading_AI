"use client";
import { Bot } from "lucide-react";
import { useEffect, useState } from "react";
import { put } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { Badge, Btn, ErrorLine, Field, inputCls, Panel } from "./ui";

/** Single Active AI: one provider serves every AI task, with an optional fallback that is used only when enabled here.
 *  Off = the per-task routes below. Switching providers changes nothing in the trading engine. */
export default function ActiveAI({ providers, canManage, onSaved }: { providers: any[] | null | undefined; canManage: boolean; onSaved: () => void }) {
  const { data: c, reload } = useApi<any>("/api/ai/control", 30000);
  const [f, setF] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (c) setF({ single_active: c.single_active, active_provider_id: c.active_provider_id ?? "", active_model: c.active_model,
      fallback_enabled: c.fallback_enabled, fallback_provider_id: c.fallback_provider_id ?? "", fallback_model: c.fallback_model });
  }, [c]);
  async function save(next: any) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const body = { ...next, active_provider_id: next.active_provider_id === "" ? null : Number(next.active_provider_id),
        fallback_provider_id: next.fallback_provider_id === "" ? null : Number(next.fallback_provider_id) };
      const r = await put<any>("/api/ai/control", body);
      setMsg(r.single_active ? `Active AI: ${r.active_provider} (${r.active_model})${r.fallback_enabled ? `, fallback ${r.fallback_provider}` : ", no fallback"}.` : "Single Active AI off: the per-task routes apply.");
      reload(); onSaved();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const enabled = (providers || []).filter((p) => p.enabled);
  const chain = c?.effective_chain;
  return (
    <Panel title="Active AI" icon={<Bot size={12} />} accent="cyan">
      <ErrorLine error={err} />{msg && <div className="mb-2 text-[11px] text-neon">{msg}</div>}
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="text-dim">Mode:</span>
        {c ? (c.single_active ? <Badge tone="cyan">Single Active AI</Badge> : <Badge tone="dim">per-task routes</Badge>) : "…"}
        {chain && (chain.length
          ? <span>Now answering: {chain.map((h: any, i: number) => <span key={i}>{i ? " → fallback " : ""}<b>{h.provider}</b> <span className="text-dim">{h.model}</span></span>)}</span>
          : <Badge tone="bear">no provider available: AI tasks stop; candidates stay unreviewed and are not auto-executed</Badge>)}
      </div>
      {f && <div className="grid gap-2 md:grid-cols-2">
        <label className="col-span-full flex items-center gap-2 text-[11px]">
          <input type="checkbox" disabled={!canManage} checked={f.single_active} onChange={(e) => setF({ ...f, single_active: e.target.checked })} />
          Use one provider for every AI task</label>
        <Field label="Active provider"><select className={inputCls} disabled={!canManage || !f.single_active} value={f.active_provider_id}
          onChange={(e) => setF({ ...f, active_provider_id: e.target.value })}>
          <option value="">choose…</option>{enabled.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
        <Field label="Model"><input className={inputCls} disabled={!canManage || !f.single_active} value={f.active_model}
          list={f.active_provider_id ? `models-${f.active_provider_id}` : undefined} placeholder="use Discover in Providers"
          onChange={(e) => setF({ ...f, active_model: e.target.value })} /></Field>
        <label className="col-span-full flex items-center gap-2 text-[11px]">
          <input type="checkbox" disabled={!canManage || !f.single_active} checked={f.fallback_enabled} onChange={(e) => setF({ ...f, fallback_enabled: e.target.checked })} />
          Fallback: if the active provider fails, use this one (otherwise AI tasks stop and nothing is auto-executed)</label>
        {f.fallback_enabled && <>
          <Field label="Fallback provider"><select className={inputCls} disabled={!canManage} value={f.fallback_provider_id}
            onChange={(e) => setF({ ...f, fallback_provider_id: e.target.value })}>
            <option value="">choose…</option>{enabled.filter((p) => String(p.id) !== String(f.active_provider_id)).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
          <Field label="Fallback model"><input className={inputCls} disabled={!canManage} value={f.fallback_model}
            list={f.fallback_provider_id ? `models-${f.fallback_provider_id}` : undefined} onChange={(e) => setF({ ...f, fallback_model: e.target.value })} /></Field>
        </>}
        {canManage && <div className="col-span-full"><Btn small busy={busy} onClick={() => save(f)}>Save</Btn></div>}
      </div>}
      {c?.claude_pro && <div className="mt-3 rounded border border-line px-2 py-1.5 text-[11px]">
        <div className="flex items-center gap-2"><span>{c.claude_pro.label}</span><Badge tone="dim">not available</Badge></div>
        <div className="mt-0.5 text-dim">{c.claude_pro.reason}</div></div>}
    </Panel>
  );
}
