"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Field, inputCls, Panel, Table } from "@/components/ui";
import { fmt, patch, post, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const HEALTH: Record<string, any> = { healthy: "neon", degraded: "warn", failing: "bear", "cooling down": "bear" };

/** AI provider gateway: providers and encrypted keys, which model handles which task (with fallback), health, and
 *  the usage / token / cost ledger. Every provider gets the same schema validation and trading boundaries. */
export default function AIProviders() {
  const me = useMe();
  const admin = can(me, "system.manage");
  const { data: provs, error, reload } = useApi<any[]>("/api/ai/providers");
  const { data: routes, reload: reloadRoutes } = useApi<any[]>("/api/ai/routes");
  const { data: health, reload: reloadHealth } = useApi<any[]>("/api/ai/health", 30000);
  const { data: usage, reload: reloadUsage } = useApi<any>("/api/ai/usage?days=7", 30000);
  const [err, setErr] = useState<string | null>(null); const [msg, setMsg] = useState<string | null>(null);
  const [nw, setNw] = useState<any>({ name: "", kind: "openai_compatible", base_url: "", api_key: "", timeout_s: 120, max_retries: 2 });
  const [edit, setEdit] = useState<Record<string, any[]>>({});
  const [testModel, setTestModel] = useState<Record<number, string>>({});
  async function run(fn: () => Promise<any>, ok: (r: any) => string) {
    setErr(null); setMsg(null);
    try { const r = await fn(); setMsg(ok(r)); reload(); reloadRoutes(); reloadHealth(); reloadUsage(); } catch (e: any) { setErr(e.message); }
  }
  const chainOf = (r: any) => edit[r.task] ?? r.chain.map((h: any) => ({ provider_id: h.provider_id, model: h.model }));
  const setChain = (task: string, c: any[]) => setEdit({ ...edit, [task]: c });
  return (
    <div>
      <PageTitle sub="One gateway for every AI call. AI output is untrusted: it is validated against a typed schema, and the same trading boundaries apply to every provider (confirm, downgrade or reject only; never reverse, upgrade, size, or place an order). Credential-like fields are removed before anything is sent. Keys are encrypted and never shown again.">
        AI Providers</PageTitle>
      <ErrorLine error={error || err} />
      {msg && <div className="mb-2 rounded border border-neon/40 bg-neon/5 px-2 py-1 text-[11px] text-neon">{msg}</div>}
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Providers">
          <Table rows={provs} empty="No providers. Without one, candidate review uses RR_ANTHROPIC_API_KEY if it is set; otherwise candidates stay unreviewed and are never auto-executed." rowKey={(p) => p.id}
            cols={[{ k: "name", h: "Name" }, { k: "kind", h: "Kind" },
              { k: "base_url", h: "Endpoint", r: (p) => <span className="text-[10px] text-dim">{p.base_url || "Anthropic API"}</span> },
              { k: "has_key", h: "Key", r: (p) => p.has_key ? <Badge tone="neon">stored (encrypted)</Badge> : <Badge tone="dim">none</Badge> },
              { k: "enabled", h: "On", r: (p) => admin ? <Btn small tone={p.enabled ? "neon" : "dim"} onClick={() => run(() => patch(`/api/ai/providers/${p.id}`, { enabled: !p.enabled }), () => `${p.name} ${p.enabled ? "disabled" : "enabled"}.`)}>{p.enabled ? "on" : "off"}</Btn> : (p.enabled ? "on" : "off") },
              { k: "price", h: "Price / M tok", sort: false, r: (p) => p.price_in_per_mtok == null ? <span className="text-dim">not set</span> : `${p.price_in_per_mtok} / ${p.price_out_per_mtok}` },
              { k: "test", h: "", sort: false, r: (p) => admin && <div className="flex gap-1"><div className="w-28"><input className={inputCls} placeholder="model" value={testModel[p.id] || ""} onChange={(e) => setTestModel({ ...testModel, [p.id]: e.target.value })} /></div>
                <Btn small disabled={!testModel[p.id]} onClick={() => run(() => post(`/api/ai/providers/${p.id}/test?model=${encodeURIComponent(testModel[p.id])}`), (r) => r.ok ? `${p.name}: OK (${r.model}, ${r.latency_ms} ms)` : `${p.name}: failed: ${r.error}`)}>Test</Btn></div> }]} />
          {admin && (
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Field label="Name"><input className={inputCls} value={nw.name} onChange={(e) => setNw({ ...nw, name: e.target.value })} placeholder="Qwen" /></Field>
              <Field label="Kind"><select className={inputCls} value={nw.kind} onChange={(e) => setNw({ ...nw, kind: e.target.value })}>
                <option value="openai_compatible">OpenAI-compatible (Qwen, Ollama, …)</option><option value="anthropic">Anthropic Claude</option></select></Field>
              {nw.kind === "openai_compatible" && <Field label="Base URL"><input className={inputCls} value={nw.base_url} onChange={(e) => setNw({ ...nw, base_url: e.target.value })} placeholder="https://…/v1 or http://host.docker.internal:11434/v1" /></Field>}
              <Field label="API key (stored encrypted, never shown)"><input type="password" autoComplete="off" className={inputCls} value={nw.api_key} onChange={(e) => setNw({ ...nw, api_key: e.target.value })} placeholder={nw.kind === "openai_compatible" ? "optional for local servers" : "sk-ant-…"} /></Field>
              <Field label="Timeout (s)"><input type="number" className={inputCls} value={nw.timeout_s} onChange={(e) => setNw({ ...nw, timeout_s: Number(e.target.value) })} /></Field>
              <Field label="Retries"><input type="number" className={inputCls} value={nw.max_retries} onChange={(e) => setNw({ ...nw, max_retries: Number(e.target.value) })} /></Field>
              <div className="col-span-2"><Btn tone="neon" small disabled={!nw.name} onClick={() => run(() => post("/api/ai/providers", { ...nw, base_url: nw.base_url || null, api_key: nw.api_key || null }), (r) => { setNw({ ...nw, name: "", api_key: "", base_url: "" }); return `Provider ${r.name} added.`; })}>Add provider</Btn>
                <span className="ml-2 text-[10px] text-dim">A Claude subscription does not include API credits; an API key from the provider's console is needed.</span></div>
            </div>)}
        </Panel>
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
          cols={[{ k: "label", h: "Task", r: (r) => <span>{r.label}{!r.used_by_engine && <span className="text-dim"> (not used by the engine yet)</span>}</span> },
            { k: "effective", h: "In effect", sort: false, r: (r) => r.effective.length ? r.effective.map((h: any) => `${h.provider} · ${h.model}`).join(" → ") : <span className="text-dim">not configured</span> },
            { k: "chain", h: "Route", sort: false, r: (r) => !admin ? (r.source) : (
              <div className="flex flex-wrap items-center gap-1">
                {chainOf(r).map((h: any, i: number) => (
                  <div key={i} className="flex items-center gap-1">
                    <div className="w-28"><select className={inputCls} value={h.provider_id} onChange={(e) => { const c = [...chainOf(r)]; c[i] = { ...h, provider_id: Number(e.target.value) }; setChain(r.task, c); }}>
                      {(provs || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
                    <div className="w-32"><input className={inputCls} value={h.model} placeholder="model" onChange={(e) => { const c = [...chainOf(r)]; c[i] = { ...h, model: e.target.value }; setChain(r.task, c); }} /></div>
                    <Btn small tone="dim" onClick={() => setChain(r.task, chainOf(r).filter((_: any, j: number) => j !== i))}>×</Btn>
                  </div>))}
                {(provs || []).length > 0 && chainOf(r).length < 4 && <Btn small tone="dim" onClick={() => setChain(r.task, [...chainOf(r), { provider_id: provs![0].id, model: "" }])}>+ provider</Btn>}
                {edit[r.task] && <Btn small tone="neon" disabled={edit[r.task].some((h) => !h.model)} onClick={() => run(() => put(`/api/ai/routes/${r.task}`, { chain: edit[r.task] }), () => { const { [r.task]: _, ...rest } = edit; setEdit(rest); return `Route for ${r.label} saved.`; })}>Save</Btn>}
              </div>) }]} />
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
