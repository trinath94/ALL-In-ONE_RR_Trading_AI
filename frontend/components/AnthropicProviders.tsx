"use client";
import { useEffect, useState } from "react";
import { Badge, Btn, Field, inputCls, Panel, Table } from "@/components/ui";
import { api, fmt, patch, post } from "@/lib/api";

type Msg = { tone: "ok" | "err"; text: string } | null;
const blank = { name: "Anthropic", api_key: "", timeout_s: 120, max_retries: 2, price_in_per_mtok: "", price_out_per_mtok: "", max_requests_per_minute: "" };
const num = (v: any) => (v === "" || v == null ? null : Number(v));

/** Step-by-step: how to create an Anthropic API key and add it here. */
export function AnthropicKeyGuide() {
  const [open, setOpen] = useState(true);
  return (
    <Panel title="How to get an Anthropic API key and add it" right={<Btn small tone="dim" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"}</Btn>}>
      {open && <div className="space-y-3 text-[12px]">
        <div>
          <div className="mb-1 font-bold text-cyan">1. Create the key (Claude Console)</div>
          <ol className="list-decimal space-y-1 pl-5">
            <li>Open <a className="text-cyan underline" href="https://platform.claude.com" target="_blank" rel="noreferrer">platform.claude.com</a> (the Claude Console, formerly console.anthropic.com) and sign up or sign in.</li>
            <li>Add API credits: <b>Settings → Billing</b>, buy credits or add a payment method. Without credits every call fails, even with a valid key.</li>
            <li>Go to <b>Settings → API keys</b> and click <b>Create key</b>. Give it a name such as "RR Trading One" (and choose the workspace if you use several).</li>
            <li>Copy the key immediately. It starts with <code>sk-ant-</code> and is shown <b>only once</b>. If you lose it, create a new key and delete the old one.</li>
          </ol>
          <div className="mt-1 text-[11px] text-warn">A Claude.ai Pro or Max subscription does not include API access: the engine needs an API key with API credits.</div>
        </div>
        <div>
          <div className="mb-1 font-bold text-cyan">2. Add it to the engine (below, under Anthropic API)</div>
          <ol className="list-decimal space-y-1 pl-5">
            <li>Paste the key into <b>API key</b> and click <b>Save and test</b>. The engine stores it encrypted, never shows it again and never sends it to the browser.</li>
            <li>The test makes one small call with the engine's configured Claude model. A green <b>OK</b> means the key and credits work.</li>
            <li>To replace the key later: <b>Edit</b> → paste the new key → <b>Save and test</b>. <b>Remove key</b> deletes it.</li>
            <li>Then choose which tasks use it: <b>Active AI</b> (one provider for everything) or <b>Model per task</b> below. Set a spending limit under <b>Budget</b>.</li>
          </ol>
        </div>
        <div className="text-[11px] text-dim">Keep the key private: never paste it into a chat, an e-mail or a screenshot. In the Claude Console you can see usage, set a monthly spend limit and revoke the key at any time.</div>
      </div>}
    </Panel>
  );
}

/** Anthropic API providers: add (name + key), edit (replace or remove the key, limits, prices), enable, test. */
export function AnthropicProviders({ provs, admin, onChanged, onModels }: { provs?: any[] | null; admin: boolean; onChanged: () => void; onModels: (pid: number, ms: string[]) => void }) {
  const [nw, setNw] = useState<any>(blank);
  const [ed, setEd] = useState<any>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState<number | "new" | null>(null);
  const [results, setResults] = useState<Record<number, any>>({});
  const [check, setCheck] = useState<{ id: number; name: string; result?: any } | null>(null);

  /** Runs the connection test inside a pop-up that stays open until the test has finished and shows each step. */
  async function test(p: { id: number; name: string }) {
    setBusy(p.id); setCheck({ id: p.id, name: p.name });
    let r: any;
    try { r = await post<any>(`/api/ai/providers/${p.id}/test`); }
    catch (e: any) { r = { ok: false, title: "The engine did not answer", fix: "Check that the engine is running (desktop app → Start All) and try again.", error: e.message, steps: [] }; }
    setResults((x) => ({ ...x, [p.id]: { ...r, at: new Date().toISOString() } }));
    setCheck({ id: p.id, name: p.name, result: r });
    setMsg(r.ok ? { tone: "ok", text: `${p.name}: connected — ${r.model}, ${r.latency_ms} ms.` } : { tone: "err", text: `${p.name}: ${r.title || "test failed"}` });
    setBusy(null); onChanged();
  }
  async function add() {
    setMsg(null); setBusy("new");
    try {
      const r = await post<any>("/api/ai/providers", { ...nw, kind: "anthropic", api_key: nw.api_key || null, price_in_per_mtok: num(nw.price_in_per_mtok), price_out_per_mtok: num(nw.price_out_per_mtok), max_requests_per_minute: num(nw.max_requests_per_minute) });
      setNw(blank); onChanged();
      if (r.has_key) await test(r); else setMsg({ tone: "ok", text: `${r.name} saved without a key. Edit it to add the key.` });
    } catch (e: any) { setMsg({ tone: "err", text: e.message }); } finally { setBusy(null); }
  }
  async function save(removeKey = false) {
    setMsg(null); setBusy(ed.id);
    const body: any = { timeout_s: ed.timeout_s, max_retries: ed.max_retries, price_in_per_mtok: num(ed.price_in_per_mtok), price_out_per_mtok: num(ed.price_out_per_mtok), max_requests_per_minute: num(ed.max_requests_per_minute) };
    if (removeKey) body.api_key = "";
    else if (ed.api_key) body.api_key = ed.api_key;
    try {
      const r = await patch<any>(`/api/ai/providers/${ed.id}`, body);
      setEd(null); onChanged();
      if (removeKey) setMsg({ tone: "ok", text: `${r.name}: key removed.` });
      else if (r.has_key) await test(r);
      else setMsg({ tone: "ok", text: `${r.name} updated (no key stored).` });
    } catch (e: any) { setMsg({ tone: "err", text: e.message }); } finally { setBusy(null); }
  }
  async function discover(p: any) {
    try { const r = await api<any>(`/api/ai/providers/${p.id}/models`); if (r.ok) { onModels(p.id, r.models); setMsg({ tone: "ok", text: `${p.name}: ${r.models.length} models available to this key.` }); } else setMsg({ tone: "err", text: `${p.name}: ${r.error}` }); }
    catch (e: any) { setMsg({ tone: "err", text: e.message }); }
  }
  return (
    <Panel title="Anthropic API">
      {check && <TestPopup check={check} onClose={() => setCheck(null)}
        onEdit={() => { const p = (provs || []).find((x) => x.id === check.id); setCheck(null); if (p) setEd({ id: p.id, name: p.name, has_key: p.has_key, api_key: "", timeout_s: p.timeout_s, max_retries: p.max_retries, price_in_per_mtok: p.price_in_per_mtok ?? "", price_out_per_mtok: p.price_out_per_mtok ?? "", max_requests_per_minute: p.max_requests_per_minute ?? "" }); }}
        onRetry={() => test({ id: check.id, name: check.name })} />}
      {msg && <div className={`mb-2 rounded border px-2 py-1 text-[11px] ${msg.tone === "ok" ? "border-neon/40 bg-neon/5 text-neon" : "border-bear/40 bg-bear/5 text-bear"}`}>{msg.text}</div>}
      <Table rows={provs} empty="No Anthropic API key yet. Follow the guide above, then add it below." rowKey={(p) => p.id}
        cols={[{ k: "name", h: "Name" },
          { k: "has_key", h: "API key", r: (p) => p.has_key ? <Badge tone="neon">stored (encrypted)</Badge> : <Badge tone="warn">missing</Badge> },
          { k: "enabled", h: "On", r: (p) => admin ? <Btn small tone={p.enabled ? "neon" : "dim"} onClick={async () => { await patch(`/api/ai/providers/${p.id}`, { enabled: !p.enabled }); onChanged(); }}>{p.enabled ? "on" : "off"}</Btn> : (p.enabled ? "on" : "off") },
          { k: "last", h: "Last test / call", sort: false, r: (p) => { const t = results[p.id]; return <div className="text-[10px]">
            {t ? (t.ok ? <span className="text-neon">test OK {fmt.time(t.at)}</span> : <span className="text-bear" title={t.error}>test failed {fmt.time(t.at)}</span>)
              : p.last_success_at ? <span className="text-neon">ok {fmt.time(p.last_success_at)}</span> : <span className="text-dim">not tested yet</span>}
            {!t && p.last_error_at && <div className="text-bear" title={p.last_error}>error {fmt.time(p.last_error_at)}</div>}</div>; } },
          { k: "price", h: "Price / M tok", sort: false, r: (p) => p.price_in_per_mtok == null ? <span className="text-dim">not set</span> : `${p.price_in_per_mtok} / ${p.price_out_per_mtok}` },
          { k: "actions", h: "", sort: false, r: (p) => admin && <div className="flex gap-1">
            <Btn small tone="neon" disabled={busy !== null || !p.has_key} onClick={() => test(p)}>{busy === p.id ? "Testing…" : "Test"}</Btn>
            <Btn small tone="dim" onClick={() => setEd({ id: p.id, name: p.name, has_key: p.has_key, api_key: "", timeout_s: p.timeout_s, max_retries: p.max_retries, price_in_per_mtok: p.price_in_per_mtok ?? "", price_out_per_mtok: p.price_out_per_mtok ?? "", max_requests_per_minute: p.max_requests_per_minute ?? "" })}>Edit</Btn>
            <Btn small tone="dim" disabled={!p.has_key} onClick={() => discover(p)}>Models</Btn></div> }]} />
      {admin && ed && (
        <div className="mt-3 grid grid-cols-3 gap-2 rounded border border-cyan/40 p-2">
          <div className="col-span-3 text-[11px] text-cyan">Edit {ed.name}</div>
          <div className="col-span-3"><Field label={ed.has_key ? "New API key (leave empty to keep the current key)" : "API key"}>
            <input type="password" autoComplete="off" className={inputCls} value={ed.api_key} placeholder="sk-ant-…" onChange={(e) => setEd({ ...ed, api_key: e.target.value.trim() })} /></Field></div>
          <Field label="Timeout (s)"><input type="number" className={inputCls} value={ed.timeout_s} onChange={(e) => setEd({ ...ed, timeout_s: Number(e.target.value) })} /></Field>
          <Field label="Retries"><input type="number" className={inputCls} value={ed.max_retries} onChange={(e) => setEd({ ...ed, max_retries: Number(e.target.value) })} /></Field>
          <Field label="Max requests / min"><input type="number" className={inputCls} value={ed.max_requests_per_minute} placeholder="none" onChange={(e) => setEd({ ...ed, max_requests_per_minute: e.target.value })} /></Field>
          <Field label="Price in / M tok (USD)"><input type="number" className={inputCls} value={ed.price_in_per_mtok} onChange={(e) => setEd({ ...ed, price_in_per_mtok: e.target.value })} /></Field>
          <Field label="Price out / M tok (USD)"><input type="number" className={inputCls} value={ed.price_out_per_mtok} onChange={(e) => setEd({ ...ed, price_out_per_mtok: e.target.value })} /></Field>
          <div className="flex items-end gap-1">
            <Btn small tone="neon" disabled={busy !== null} onClick={() => save()}>{ed.api_key ? "Save and test" : "Save"}</Btn>
            {ed.has_key && <Btn small tone="bear" disabled={busy !== null} onClick={() => save(true)}>Remove key</Btn>}
            <Btn small tone="dim" onClick={() => setEd(null)}>Cancel</Btn></div>
          <div className="col-span-3 text-[10px] text-dim">Prices are used for cost tracking and the budget (check the current prices for your model in the Claude Console). Without prices, cost is shown as "no price configured", never estimated.</div>
        </div>)}
      {admin && (
        <div className="mt-3 grid grid-cols-2 gap-2 rounded border border-line p-2">
          <div className="col-span-2 text-[11px] text-dim">Add an Anthropic API key</div>
          <Field label="Name"><input className={inputCls} value={nw.name} onChange={(e) => setNw({ ...nw, name: e.target.value })} /></Field>
          <Field label="API key (stored encrypted, never shown again)"><input type="password" autoComplete="off" className={inputCls} value={nw.api_key} placeholder="sk-ant-…" onChange={(e) => setNw({ ...nw, api_key: e.target.value.trim() })} /></Field>
          <Field label="Price in / out per M tokens (optional, USD)"><div className="flex gap-1"><input type="number" className={inputCls} value={nw.price_in_per_mtok} onChange={(e) => setNw({ ...nw, price_in_per_mtok: e.target.value })} /><input type="number" className={inputCls} value={nw.price_out_per_mtok} onChange={(e) => setNw({ ...nw, price_out_per_mtok: e.target.value })} /></div></Field>
          <Field label="Max requests / min (optional)"><input type="number" className={inputCls} value={nw.max_requests_per_minute} onChange={(e) => setNw({ ...nw, max_requests_per_minute: e.target.value })} /></Field>
          <div className="col-span-2 flex items-center gap-2">
            <Btn tone="neon" small disabled={!nw.name || busy !== null} onClick={add}>{busy === "new" ? "Saving…" : nw.api_key ? "Save and test" : "Save"}</Btn>
            <span className="text-[10px] text-dim">The engine uses only the Anthropic API. Other AI connectors have been removed.</span></div>
        </div>)}
    </Panel>
  );
}

const STEPS = ["API key stored", "API key readable", "Anthropic API reached", "Valid answer from the model"];

/** Modal while the Anthropic API configuration is tested: it cannot be closed until the test has finished. */
function TestPopup({ check, onClose, onEdit, onRetry }: { check: { name: string; result?: any }; onClose: () => void; onEdit: () => void; onRetry: () => void }) {
  const [secs, setSecs] = useState(0);
  const r = check.result;
  useEffect(() => {
    if (r) return;
    setSecs(0);
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [r]);
  const done: Record<string, boolean> = Object.fromEntries((r?.steps || []).map((s: any) => [s.step, s.ok]));
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label="Testing the Anthropic API configuration">
      <div className="w-full max-w-md rounded border border-cyan/60 bg-panel p-4 text-[12px] shadow-2xl">
        <div className="mb-1 text-[13px] font-bold text-cyan">{r ? (r.ok ? "Anthropic API connected" : "Anthropic API not connected") : "Testing the Anthropic API configuration…"}</div>
        <div className="mb-3 text-[11px] text-dim">{check.name}{r?.model ? ` · model ${r.model}` : ""}{!r && ` · ${secs} s`}</div>
        <ul className="mb-3 space-y-1">
          {STEPS.map((s, i) => {
            const st = s in done ? (done[s] ? "ok" : "bad") : r ? "skip" : (i === Object.keys(done).length ? "run" : "wait");
            return <li key={s} className="flex items-center gap-2">
              <span className={`inline-block w-4 text-center ${st === "ok" ? "text-neon" : st === "bad" ? "text-bear" : st === "run" ? "animate-pulse text-cyan" : "text-dim"}`}>
                {st === "ok" ? "✔" : st === "bad" ? "✖" : st === "run" ? "●" : "–"}</span>
              <span className={st === "skip" || st === "wait" ? "text-dim" : ""}>{s}</span></li>;
          })}
        </ul>
        {!r && <div className="text-[11px] text-dim">Sending one small request to the Anthropic API. This usually takes a few seconds; please wait.</div>}
        {r?.ok && <div className="rounded border border-neon/40 bg-neon/5 p-2 text-neon">The key and credits work ({r.latency_ms} ms). Choose which tasks use it under Active AI or Model per task, and set a Budget.</div>}
        {r && !r.ok && <div className="space-y-1 rounded border border-bear/40 bg-bear/5 p-2">
          <div className="font-bold text-bear">{r.title || "Test failed"}</div>
          {r.fix && <div>{r.fix}</div>}
          {r.error && <details><summary className="cursor-pointer text-[11px] text-dim">Error details</summary><div className="whitespace-pre-wrap break-words text-[10px] text-dim">{r.error}</div></details>}
        </div>}
        <div className="mt-3 flex justify-end gap-2">
          {r && !r.ok && <Btn small tone="dim" onClick={onEdit}>Edit key</Btn>}
          {r && !r.ok && <Btn small tone="cyan" onClick={onRetry}>Test again</Btn>}
          <Btn small tone={r?.ok ? "neon" : "dim"} disabled={!r} onClick={onClose}>{r ? "Close" : "Testing…"}</Btn>
        </div>
      </div>
    </div>
  );
}
