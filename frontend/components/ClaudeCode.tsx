"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { can, useMe } from "./Shell";

const LIVE = ["QUEUED", "RUNNING"];
const RESUMABLE = ["CANCELLED", "FAILED", "INTERRUPTED"];

/** A Claude Code task in the Engine Assistant: live phase and progress, files inspected and changed, the engine's own
 *  test run, the approval gates the patch touches, the patch itself, usage and cost. Merge needs an administrator,
 *  passing tests and an acknowledgement of every gate; it merges inside the Claude Code workspace only (never deploys). */
export function ClaudeCodeTask({ initial }: { initial: any }) {
  const me = useMe();
  const admin = can(me, "system.manage") && can(me, "users.manage");
  const [t, setT] = useState<any>(initial);
  const [acks, setAcks] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (!LIVE.includes(t.status)) return;
    const id = setInterval(async () => {
      try { setT(await api<any>(`/api/agent/tasks/${t.id}`)); } catch { /* keep the last state */ }
    }, 2000);
    return () => clearInterval(id);
  }, [t.id, t.status]);
  async function act(what: "approve" | "cancel" | "resume") {
    setBusy(true); setErr("");
    try {
      const body = what === "approve" ? { acknowledged_gates: acks } : what === "resume" ? { instruction: "" } : undefined;
      setT(await post<any>(`/api/agent/tasks/${t.id}/${what}`, body));
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const r = t.report || {};
  const u = t.usage || {};
  const gates: string[] = r.gates || [];
  const last = (t.progress || []).slice(-12);
  const testsOk = r.tests?.passed;
  const canMerge = admin && t.status === "AWAITING_APPROVAL" && testsOk && gates.every((g) => acks.includes(g));
  return (
    <div className="mt-1 space-y-1 border-t border-line pt-1 text-[11px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-cyan/50 px-1 text-cyan">Claude Code #{t.id}</span>
        <span className={t.status === "FAILED" ? "text-bear" : t.status === "MERGED" || t.status === "DONE" ? "text-neon" : "text-warn"}>{t.status.replace("_", " ").toLowerCase()}</span>
        {t.branch && <span className="text-dim">branch {t.branch}</span>}
        {LIVE.includes(t.status) && <span className="text-dim">updating…</span>}
      </div>
      <div className="max-h-40 overflow-y-auto rounded bg-black/20 p-1">
        {last.map((e: any, i: number) => (
          <div key={i} className={e.kind === "error" ? "text-bear" : e.kind === "approval" ? "text-warn" : e.kind === "tool" ? "text-dim" : ""}>
            <span className="text-dim">[{e.phase}]</span> {e.text}</div>))}
      </div>
      {(r.files_inspected?.length > 0 || r.files_changed?.length > 0) && <div className="text-dim">
        Files inspected: {r.files_inspected?.length || 0}{r.files_changed?.length ? ` · changed: ${r.files_changed.map((f: any) => `${f.status} ${f.path}`).join(", ")}` : ""}</div>}
      {r.tests && <div className={testsOk ? "text-neon" : "text-bear"}>Engine test run: {testsOk ? "passed" : "FAILED"} <span className="text-dim">({r.tests.command})</span>
        {!testsOk && r.tests.output && <details><summary className="cursor-pointer text-dim">output</summary><pre className="max-h-40 overflow-auto whitespace-pre-wrap text-[10px]">{r.tests.output}</pre></details>}</div>}
      {r.patch && <details><summary className="cursor-pointer text-dim">Patch ({r.stat}){r.patch_truncated ? " — truncated" : ""}</summary>
        <pre className="max-h-64 overflow-auto whitespace-pre text-[10px]">{r.patch.split("\n").map((l: string, i: number) => (
          <span key={i} className={l.startsWith("+") ? "text-neon" : l.startsWith("-") ? "text-bear" : ""}>{l}{"\n"}</span>))}</pre></details>}
      {r.policy_denials?.length > 0 && <div className="text-dim">Blocked by the workspace policy: {r.policy_denials.length} tool call(s).</div>}
      {(u.cost_usd !== undefined) && <div className="text-dim">Usage: {u.cost_usd} USD · {u.input_tokens} in / {u.output_tokens} out tokens{u.cache_read_tokens ? ` · ${u.cache_read_tokens} cached` : ""} · {u.turns} turns</div>}
      {t.status === "AWAITING_APPROVAL" && <div className="space-y-1 rounded border border-warn/50 p-1">
        <div className="text-warn">Review required. Merging puts the change on the workspace's base branch only; it is never deployed from here.</div>
        {gates.length === 0 ? <div className="text-dim">No approval gate touched.</div> : gates.map((g) => (
          <label key={g} className="flex items-start gap-1">
            <input type="checkbox" disabled={!admin} checked={acks.includes(g)} onChange={(e) => setAcks(e.target.checked ? [...acks, g] : acks.filter((x) => x !== g))} />
            <span><b>{g.replace(/_/g, " ")}</b>: <span className="text-dim">{r.gate_labels?.[g]}</span></span></label>))}
        {admin ? <div className="flex gap-2">
          <button type="button" disabled={busy || !canMerge} className="rounded border border-neon/60 px-2 py-0.5 text-neon disabled:opacity-40" onClick={() => act("approve")}>Approve and merge</button>
          <button type="button" disabled={busy} className="text-dim underline" onClick={() => act("cancel")}>Reject</button>
        </div> : <div className="text-dim">An administrator reviews and merges this patch.</div>}
      </div>}
      {admin && LIVE.includes(t.status) && <button type="button" disabled={busy || t.cancel_requested} className="text-dim underline" onClick={() => act("cancel")}>{t.cancel_requested ? "Cancelling…" : "Cancel"}</button>}
      {admin && RESUMABLE.includes(t.status) && t.session_id && <button type="button" disabled={busy} className="rounded border border-cyan/50 px-2 py-0.5 text-cyan" onClick={() => act("resume")}>Resume</button>}
      {r.merged && <div className="text-neon">Merged into {r.merged.into} at {r.merged.commit}. {r.deployment}</div>}
      {r.claude_result && !LIVE.includes(t.status) && <details><summary className="cursor-pointer text-dim">Claude's report</summary><div className="whitespace-pre-wrap">{r.claude_result}</div></details>}
      {err && <div className="text-bear">{err}</div>}
    </div>
  );
}

/** AI Studio: Claude Code availability and settings (administrator). The API key is never shown. */
export function ClaudeCodeSettings() {
  const me = useMe();
  const admin = can(me, "system.manage") && can(me, "users.manage");
  const [s, setS] = useState<any>(null);
  const [f, setF] = useState<any>({});
  const [msg, setMsg] = useState("");
  useEffect(() => { api<any>("/api/claude-code/status").then((x) => { setS(x); setF({ model: x.model_setting, max_budget_usd: x.max_budget_usd, max_turns: x.max_turns, test_command: x.test_command }); }).catch((e) => setMsg(e.message)); }, []);
  async function save(extra: any = {}) {
    setMsg("");
    try {
      const x = await api<any>("/api/claude-code/config", { method: "PUT", body: { ...f, max_budget_usd: Number(f.max_budget_usd), max_turns: Number(f.max_turns), ...extra } });
      setS(x); setMsg("Saved.");
    } catch (e: any) { setMsg(e.message); }
  }
  if (!s) return <div className="text-[12px] text-dim">{msg || "Loading Claude Code status…"}</div>;
  return (
    <div className="space-y-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className={s.ready ? "text-neon" : "text-warn"}>{s.ready ? "Ready" : "Not ready"}</span>
        <span className="text-dim">SDK {s.sdk.installed ? `${s.sdk.version} (CLI ${s.sdk.cli})` : "not installed"} · workspace {s.workspace.ready ? `${s.workspace.base_branch}@${s.workspace.head}` : s.workspace.configured ? "not initialised" : "not configured"} · API key {s.api_key_configured ? "configured" : "missing"} · model {s.model}</span>
      </div>
      {s.not_ready.length > 0 && <ul className="list-disc pl-5 text-dim">{s.not_ready.map((x: string) => <li key={x}>{x}</li>)}</ul>}
      <div className="text-dim">Tasks: {s.tasks.queued} queued · {s.tasks.running} running · {s.tasks.awaiting_approval} awaiting review. {s.deployment}</div>
      {admin && <div className="grid grid-cols-2 gap-2">
        <label>Model (empty = engine default)<input className="w-full rounded border border-line bg-transparent px-1" value={f.model || ""} onChange={(e) => setF({ ...f, model: e.target.value })} /></label>
        <label>Budget per task (USD)<input type="number" min="0.1" step="0.1" className="w-full rounded border border-line bg-transparent px-1" value={f.max_budget_usd} onChange={(e) => setF({ ...f, max_budget_usd: e.target.value })} /></label>
        <label>Max turns per task<input type="number" min="1" max="200" className="w-full rounded border border-line bg-transparent px-1" value={f.max_turns} onChange={(e) => setF({ ...f, max_turns: e.target.value })} /></label>
        <label>Test command (run by the engine)<input className="w-full rounded border border-line bg-transparent px-1" value={f.test_command} onChange={(e) => setF({ ...f, test_command: e.target.value })} /></label>
        <div className="col-span-2 flex gap-2">
          <button type="button" className="rounded border border-cyan/50 px-2 py-0.5 text-cyan" onClick={() => save()}>Save</button>
          <button type="button" className={`rounded border px-2 py-0.5 ${s.enabled ? "border-bear/60 text-bear" : "border-neon/60 text-neon"}`} onClick={() => save({ enabled: !s.enabled })}>{s.enabled ? "Switch Claude Code off" : "Switch Claude Code on"}</button>
        </div>
      </div>}
      <div className="text-[11px] text-dim">Use it from the Engine Assistant: start a request with /code (for example "/code fix the bug in the symbol mapper and add a test"). Claude Code works only in its own workspace branch; every patch is tested by the engine and merged only after an administrator's review. It never changes LIVE, risk limits, credentials or orders by itself, and the engine keeps trading without it.</div>
      {msg && <div className="text-[11px] text-dim">{msg}</div>}
    </div>
  );
}
