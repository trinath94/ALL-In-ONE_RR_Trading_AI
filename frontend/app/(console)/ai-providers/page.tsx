"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Field, inputCls, Panel, Table } from "@/components/ui";
import { api, fmt, patch, post, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const HEALTH: Record<string, any> = { healthy: "neon", degraded: "warn", failing: "bear", "cooling down": "bear" };

/** AI Studio (Part 14): providers (presets for Anthropic, OpenAI, Qwen, Ollama) and encrypted keys, the provider's own model
 *  list, which model handles which task (with fallback), per-minute limits, health, the usage / token / cost ledger and
 *  the model version history. Every provider gets the same schema validation and trading boundaries. */
export default function AIProviders() {
  const me = useMe();
  const admin = can(me, "system.manage");
  const { data: provs, error, reload } = useApi<any[]>("/api/ai/providers");
  const { data: routes, reload: reloadRoutes } = useApi<any[]>("/api/ai/routes");
  const { data: health, reload: reloadHealth } = useApi<any[]>("/api/ai/health", 30000);
  const { data: usage, reload: reloadUsage } = useApi<any>("/api/ai/usage?days=7", 30000);
  const [err, setErr] = useState<string | null>(null); const [msg, setMsg] = useState<string | null>(null);
  const { data: presets } = useApi<any[]>("/api/ai/presets");
  const { data: hist, reload: reloadHist } = useApi<any>("/api/ai/models/history?days=90", 60000);
  const blank = { name: "", kind: "openai_compatible", base_url: "", api_key: "", timeout_s: 120, max_retries: 2, price_in_per_mtok: "", price_out_per_mtok: "", max_requests_per_minute: "" };
  const [nw, setNw] = useState<any>(blank);
  const [preset, setPreset] = useState<any>(null);
  const [models, setModels] = useState<Record<number, string[]>>({});
  const [ed, setEd] = useState<any>(null);
  const num = (v: any) => (v === "" || v == null ? null : Number(v));
  function applyPreset(id: string) {
    const p = (presets || []).find((x) => x.id === id); setPreset(p || null);
    if (p) setNw({ ...nw, kind: p.kind, base_url: p.base_url || "", name: nw.name || p.label.split(" (")[0] });
  }
  async function discover(p: any) {
    setErr(null); setMsg(null);
    try { const r = await api<any>(`/api/ai/providers/${p.id}/models`); if (r.ok) { setModels({ ...models, [p.id]: r.models }); setMsg(`${p.name}: ${r.models.length} models offered by the provider.`); } else setErr(`${p.name}: ${r.error}`); }
    catch (e: any) { setErr(e.message); }
  }
  const [edit, setEdit] = useState<Record<string, any[]>>({});
  const [testModel, setTestModel] = useState<Record<number, string>>({});
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
        <Panel title="Providers">
          <Table rows={provs} empty="No providers. Without one, candidate review uses RR_ANTHROPIC_API_KEY if it is set; otherwise candidates stay unreviewed and are never auto-executed, unless the engine runs without AI (Control Center)." rowKey={(p) => p.id}
            cols={[{ k: "name", h: "Name" }, { k: "kind", h: "Kind" },
              { k: "base_url", h: "Endpoint", r: (p) => <span className="text-[10px] text-dim">{p.base_url || "Anthropic API"}</span> },
              { k: "has_key", h: "Key", r: (p) => p.has_key ? <Badge tone="neon">stored (encrypted)</Badge> : <Badge tone="dim">none</Badge> },
              { k: "enabled", h: "On", r: (p) => admin ? <Btn small tone={p.enabled ? "neon" : "dim"} onClick={() => run(() => patch(`/api/ai/providers/${p.id}`, { enabled: !p.enabled }), () => `${p.name} ${p.enabled ? "disabled" : "enabled"}.`)}>{p.enabled ? "on" : "off"}</Btn> : (p.enabled ? "on" : "off") },
              { k: "price", h: "Price / M tok", sort: false, r: (p) => p.price_in_per_mtok == null ? <span className="text-dim">not set</span> : `${p.price_in_per_mtok} / ${p.price_out_per_mtok}` },
              { k: "max_requests_per_minute", h: "Limit", r: (p) => p.max_requests_per_minute ? `${p.max_requests_per_minute}/min` : <span className="text-dim">none</span> },
              { k: "models", h: "Models", sort: false, r: (p) => admin ? <div className="flex items-center gap-1"><Btn small tone="dim" onClick={() => discover(p)}>Discover</Btn>{models[p.id] && <span className="text-[10px] text-dim">{models[p.id].length}</span>}
                <Btn small tone="dim" onClick={() => setEd({ id: p.id, name: p.name, timeout_s: p.timeout_s, max_retries: p.max_retries, price_in_per_mtok: p.price_in_per_mtok ?? "", price_out_per_mtok: p.price_out_per_mtok ?? "", max_requests_per_minute: p.max_requests_per_minute ?? "" })}>Edit</Btn></div> : null },
              { k: "test", h: "", sort: false, r: (p) => admin && <div className="flex gap-1"><div className="w-28"><input className={inputCls} placeholder="model" list={`models-${p.id}`} value={testModel[p.id] || ""} onChange={(e) => setTestModel({ ...testModel, [p.id]: e.target.value })} /></div>
                <Btn small disabled={!testModel[p.id]} onClick={() => run(() => post(`/api/ai/providers/${p.id}/test?model=${encodeURIComponent(testModel[p.id])}`), (r) => r.ok ? `${p.name}: OK (${r.model}, ${r.latency_ms} ms)` : `${p.name}: failed: ${r.error}`)}>Test</Btn></div> }]} />
          {Object.entries(models).map(([pid, ms]) => <datalist key={pid} id={`models-${pid}`}>{ms.map((m) => <option key={m} value={m} />)}</datalist>)}
          {admin && ed && (
            <div className="mt-3 grid grid-cols-3 gap-2 rounded border border-line p-2">
              <div className="col-span-3 text-[11px] text-dim">Edit {ed.name}</div>
              <Field label="Price in / M tok"><input type="number" className={inputCls} value={ed.price_in_per_mtok} onChange={(e) => setEd({ ...ed, price_in_per_mtok: e.target.value })} /></Field>
              <Field label="Price out / M tok"><input type="number" className={inputCls} value={ed.price_out_per_mtok} onChange={(e) => setEd({ ...ed, price_out_per_mtok: e.target.value })} /></Field>
              <Field label="Max requests / min"><input type="number" className={inputCls} value={ed.max_requests_per_minute} placeholder="none" onChange={(e) => setEd({ ...ed, max_requests_per_minute: e.target.value })} /></Field>
              <Field label="Timeout (s)"><input type="number" className={inputCls} value={ed.timeout_s} onChange={(e) => setEd({ ...ed, timeout_s: Number(e.target.value) })} /></Field>
              <Field label="Retries"><input type="number" className={inputCls} value={ed.max_retries} onChange={(e) => setEd({ ...ed, max_retries: Number(e.target.value) })} /></Field>
              <div className="flex items-end gap-1"><Btn small tone="neon" onClick={() => run(() => patch(`/api/ai/providers/${ed.id}`, { timeout_s: ed.timeout_s, max_retries: ed.max_retries, price_in_per_mtok: num(ed.price_in_per_mtok), price_out_per_mtok: num(ed.price_out_per_mtok), max_requests_per_minute: num(ed.max_requests_per_minute) }), () => { setEd(null); return `${ed.name} updated.`; })}>Save</Btn><Btn small tone="dim" onClick={() => setEd(null)}>Cancel</Btn></div>
              <div className="col-span-3 text-[10px] text-dim">Without prices, cost is shown as "no price configured", never estimated. Above the per-minute limit the gateway moves to the next provider in the route instead of calling.</div>
            </div>)}
          {admin && (
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Field label="Preset"><select className={inputCls} value={preset?.id || ""} onChange={(e) => applyPreset(e.target.value)}>
                <option value="">choose…</option>{(presets || []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select></Field>
              <Field label="Name"><input className={inputCls} value={nw.name} onChange={(e) => setNw({ ...nw, name: e.target.value })} placeholder="Qwen" /></Field>
              <Field label="Kind"><select className={inputCls} value={nw.kind} onChange={(e) => setNw({ ...nw, kind: e.target.value })}>
                <option value="openai_compatible">OpenAI-compatible (Qwen, Ollama, …)</option><option value="anthropic">Anthropic Claude</option></select></Field>
              {nw.kind === "openai_compatible" && <Field label="Base URL"><input className={inputCls} value={nw.base_url} onChange={(e) => setNw({ ...nw, base_url: e.target.value })} placeholder="https://…/v1 or http://host.docker.internal:11434/v1" /></Field>}
              <Field label="API key (stored encrypted, never shown)"><input type="password" autoComplete="off" className={inputCls} value={nw.api_key} onChange={(e) => setNw({ ...nw, api_key: e.target.value })} placeholder={nw.kind === "openai_compatible" ? "optional for local servers" : "sk-ant-…"} /></Field>
              <Field label="Timeout (s)"><input type="number" className={inputCls} value={nw.timeout_s} onChange={(e) => setNw({ ...nw, timeout_s: Number(e.target.value) })} /></Field>
              <Field label="Retries"><input type="number" className={inputCls} value={nw.max_retries} onChange={(e) => setNw({ ...nw, max_retries: Number(e.target.value) })} /></Field>
              <Field label="Price in / out per M tokens (optional)"><div className="flex gap-1"><input type="number" className={inputCls} value={nw.price_in_per_mtok} onChange={(e) => setNw({ ...nw, price_in_per_mtok: e.target.value })} /><input type="number" className={inputCls} value={nw.price_out_per_mtok} onChange={(e) => setNw({ ...nw, price_out_per_mtok: e.target.value })} /></div></Field>
              <Field label="Max requests / min (optional)"><input type="number" className={inputCls} value={nw.max_requests_per_minute} onChange={(e) => setNw({ ...nw, max_requests_per_minute: e.target.value })} /></Field>
              {preset && <div className="col-span-2 text-[10px] text-cyan">{preset.note}</div>}
              <div className="col-span-2"><Btn tone="neon" small disabled={!nw.name} onClick={() => run(() => post("/api/ai/providers", { ...nw, base_url: nw.base_url || null, api_key: nw.api_key || null, price_in_per_mtok: num(nw.price_in_per_mtok), price_out_per_mtok: num(nw.price_out_per_mtok), max_requests_per_minute: num(nw.max_requests_per_minute) }), (r) => { setNw(blank); setPreset(null); return `Provider ${r.name} added.`; })}>Add provider</Btn>
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
