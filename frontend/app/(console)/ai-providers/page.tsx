"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, inputCls, Panel, Table } from "@/components/ui";
import { fmt, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import ActiveAI from "@/components/ActiveAI";
import { ClaudeCodeSettings } from "@/components/ClaudeCode";
import { Autonomy } from "@/components/Autonomy";
import { AnthropicKeyGuide, AnthropicProviders } from "@/components/AnthropicProviders";

const HEALTH: Record<string, any> = { healthy: "neon", degraded: "warn", failing: "bear", "cooling down": "bear" };

/** AI Studio: the Anthropic API (the only AI provider) with its encrypted key (add, replace, remove, test), a guide to
 *  creating the key, the models the key can use, which model handles which task (with fallback), per-minute limits,
 *  health, the usage / token / cost ledger and the model version history. */
export default function AIProviders() {
  const me = useMe();
  const admin = can(me, "system.manage");
  const { data: provs, error, reload } = useApi<any[]>("/api/ai/providers");
  const { data: routes, reload: reloadRoutes } = useApi<any[]>("/api/ai/routes");
  const { data: health, reload: reloadHealth } = useApi<any[]>("/api/ai/health", 30000);
  const { data: usage, reload: reloadUsage } = useApi<any>("/api/ai/usage?days=7", 30000);
  const [err, setErr] = useState<string | null>(null); const [msg, setMsg] = useState<string | null>(null);
  const { data: hist, reload: reloadHist } = useApi<any>("/api/ai/models/history?days=90", 60000);
  const [models, setModels] = useState<Record<number, string[]>>({});
  const [edit, setEdit] = useState<Record<string, any[]>>({});
  async function run(fn: () => Promise<any>, ok: (r: any) => string) {
    setErr(null); setMsg(null);
    try { const r = await fn(); setMsg(ok(r)); reload(); reloadRoutes(); reloadHealth(); reloadUsage(); reloadHist(); } catch (e: any) { setErr(e.message); }
  }
  const chainOf = (r: any) => edit[r.task] ?? r.chain.map((h: any) => ({ provider_id: h.provider_id, model: h.model }));
  // Parts 27-30: candidate review can ask the first two providers independently (both must confirm).
  const [modes, setModes] = useState<Record<string, string>>({});
  const modeOf = (r: any) => modes[r.task] ?? r.mode ?? "fallback";
  const setChain = (task: string, c: any[]) => setEdit({ ...edit, [task]: c });
  return (
    <div>
      <PageTitle sub="One gateway for every AI call. AI output is untrusted: it is validated against a typed schema, and the same trading boundaries apply to every provider (confirm, downgrade or reject only; never reverse, upgrade, size, or place an order). Credential-like fields are removed before anything is sent. Keys are encrypted and never shown again.">
        AI Studio</PageTitle>
      <ErrorLine error={error || err} />
      {msg && <div className="mb-2 rounded border border-neon/40 bg-neon/5 px-2 py-1 text-[11px] text-neon">{msg}</div>}
      <div className="grid gap-3 xl:grid-cols-2">
        <div className="xl:col-span-2"><Autonomy admin={admin} /></div>
        <div className="xl:col-span-2"><ActiveAI providers={provs} canManage={admin} onSaved={() => { reload(); reloadRoutes(); reloadHealth(); }} /></div>
        <div className="xl:col-span-2"><Panel title="Claude Code (development tasks)"><ClaudeCodeSettings /></Panel></div>
        <div className="xl:col-span-2"><AnthropicKeyGuide /></div>
        <AnthropicProviders provs={provs} admin={admin} onChanged={() => { reload(); reloadRoutes(); reloadHealth(); reloadUsage(); reloadHist(); }}
          onModels={(pid, ms) => setModels({ ...models, [pid]: ms })} />
        {Object.entries(models).map(([pid, ms]) => <datalist key={pid} id={`models-${pid}`}>{ms.map((m) => <option key={m} value={m} />)}</datalist>)}
        <Panel title="Health (from the usage ledger)">
          <Table rows={health} empty="No provider has been called yet." rowKey={(x) => x.provider}
            cols={[{ k: "provider", h: "Provider" }, { k: "state", h: "State", r: (x) => <Badge tone={HEALTH[x.state] || "dim"}>{x.state}</Badge> },
              { k: "last_success", h: "Last success", r: (x) => x.last_success ? fmt.time(x.last_success) : "—" },
              { k: "calls_last_hour", h: "Calls / failures (1 h)", r: (x) => `${x.calls_last_hour} / ${x.failures_last_hour}` },
              { k: "last_error", h: "Last error", sort: false, r: (x) => <span className="block max-w-[240px] truncate text-bear">{x.last_error ? `${x.last_error.status}: ${x.last_error.error || ""}` : ""}</span> }]} />
        </Panel>
      </div>
      <Panel title="Model per task (tried in order; the next one is the fallback)" className="mt-3">
        <Table rows={routes} rowKey={(r) => r.task}
          cols={[{ k: "label", h: "Task", r: (r) => <span>{r.label}{!r.used_by_engine && <span className="text-dim"> (not used by the engine)</span>}</span> },
            { k: "effective", h: "In effect", sort: false, r: (r) => r.effective.length ? r.effective.map((h: any) => `${h.provider} · ${h.model}`).join(" → ") : <span className="text-dim">not configured</span> },
            { k: "chain", h: "Route", sort: false, r: (r) => !admin ? (r.source) : (
              <div className="flex flex-wrap items-center gap-1">
                {chainOf(r).map((h: any, i: number) => (
                  <div key={i} className="flex items-center gap-1">
                    <div className="w-28"><select className={inputCls} value={h.provider_id} onChange={(e) => { const c = [...chainOf(r)]; c[i] = { ...h, provider_id: Number(e.target.value) }; setChain(r.task, c); }}>
                      {(provs || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
                    <div className="w-32"><input className={inputCls} value={h.model} placeholder="model" list={`models-${h.provider_id}`} onChange={(e) => { const c = [...chainOf(r)]; c[i] = { ...h, model: e.target.value }; setChain(r.task, c); }} /></div>
                    <Btn small tone="dim" onClick={() => setChain(r.task, chainOf(r).filter((_: any, j: number) => j !== i))}>×</Btn>
                  </div>))}
                {(provs || []).length > 0 && chainOf(r).length < 4 && <Btn small tone="dim" onClick={() => setChain(r.task, [...chainOf(r), { provider_id: provs![0].id, model: "" }])}>+ provider</Btn>}
                {(edit[r.task] || modes[r.task]) && <Btn small tone="neon" disabled={chainOf(r).some((h: any) => !h.model)} onClick={() => run(() => put(`/api/ai/routes/${r.task}`, { chain: chainOf(r), mode: modeOf(r) }), () => { const { [r.task]: _, ...rest } = edit; setEdit(rest); const { [r.task]: _m, ...restM } = modes; setModes(restM); return `Route for ${r.label} saved (${modeOf(r)}).`; })}>Save</Btn>}
              </div>) },
            { k: "mode", h: "Mode", sort: false, r: (r) => !r.consensus_supported ? <span className="text-dim">fallback</span> : !admin ? <Badge tone={r.mode === "consensus" ? "violet" : "dim"}>{r.mode}</Badge> : (
              <div className="w-48" title="Consensus: the first two providers review independently; the candidate is confirmed only if both confirm the engine's direction. Any rejection rejects; if either gives no valid answer the candidate stays unreviewed and is never auto-executed.">
                <select className={inputCls} value={modeOf(r)} onChange={(e) => setModes({ ...modes, [r.task]: e.target.value })}>
                  <option value="fallback">fallback (first valid answer)</option>
                  <option value="consensus">consensus (first two must agree)</option></select></div>) }]} />
      </Panel>
      <Panel title="Model version history (what each requested model was actually served as)" className="mt-3">
        <Table rows={hist?.models} empty="No AI calls recorded yet." rowKey={(g) => `${g.provider}|${g.requested_model}`}
          cols={[{ k: "provider", h: "Provider" }, { k: "requested_model", h: "Requested", r: (g) => g.requested_model || <span className="text-dim">not recorded (before Phase 14)</span> },
            { k: "current", h: "Served now" },
            { k: "versions", h: "Versions", r: (g) => g.changed ? <Badge tone="warn">{g.versions} (changed)</Badge> : g.versions },
            { k: "served", h: "History", sort: false, cls: "whitespace-normal", r: (g) => g.served.map((v: any) => `${v.model}: ${v.calls} calls, ${fmt.time(v.first_seen)} – ${fmt.time(v.last_seen)} (${v.tasks.join(", ")})`).join(" → ") }]} />
        <div className="mt-2 text-[10px] uppercase text-dim">Configuration changes</div>
        <Table rows={hist?.configuration_changes} empty="No provider or route changes recorded." rowKey={(c, i) => i}
          cols={[{ k: "at", h: "When", r: (c) => fmt.time(c.at) }, { k: "action", h: "Change" }, { k: "target", h: "Target" },
            { k: "details", h: "Details", sort: false, cls: "whitespace-normal", r: (c) => <span className="text-[10px] text-dim">{JSON.stringify(c.details)}</span> }]} />
      </Panel>
      <Panel title={`Usage (last ${usage?.days ?? 7} days)`} className="mt-3">
        <Table rows={usage?.summary} empty="No AI calls recorded." rowKey={(u) => u.provider + u.model + u.task + u.status}
          cols={[{ k: "provider", h: "Provider" }, { k: "model", h: "Model (as reported)" }, { k: "task", h: "Task" },
            { k: "status", h: "Status", r: (u) => <Badge tone={u.status === "ok" ? "neon" : u.status === "skipped" ? "dim" : "bear"}>{u.status}</Badge> },
            { k: "calls", h: "Calls" }, { k: "input_tokens", h: "Tokens in / out", r: (u) => `${u.input_tokens} / ${u.output_tokens}` },
            { k: "cost", h: "Cost", r: (u) => u.cost == null ? <span className="text-dim">{u.cost_note || "—"}</span> : u.cost.toFixed(4) },
            { k: "avg_latency_ms", h: "Avg latency", r: (u) => `${Math.round(u.avg_latency_ms)} ms` },
            { k: "last_seen", h: "Model seen", r: (u) => `${fmt.time(u.first_seen)} – ${fmt.time(u.last_seen)}` }]} />
      </Panel>
    </div>
  );
}
