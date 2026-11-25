"use client";
import { MessageCircle, Send, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { post } from "@/lib/api";
import { ClaudeCodeTask } from "./ClaudeCode";
import { can, useMe } from "./Shell";

type Msg = { role: "you" | "engine"; text: string; data?: any };
const QUICK = ["Engine status", "Why was the last signal not executed?", "What should I do next?",
  "Audit my entire trading engine", "Find and repair all errors", "Is the engine ready for autonomous trading?",
  "Check all available trading symbols", "Hand control back to the engine", "Give me a market summary",
  "Review my recent trades", "Explain the open errors"];

/** Ask the engine from any page (or with a signal id from the Signal Centre). */
export function askAssistant(question: string, signalId?: number) {
  window.dispatchEvent(new CustomEvent("rr-assistant", { detail: { question, signalId } }));
}

/** Engine Assistant: a chat panel that explains what the engine is doing, where a valid signal stopped on its way to
 *  execution, and what to do next. Read-only: it never changes anything. */
export function AssistantPanel() {
  const me = useMe();
  const admin = can(me, "system.manage");
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  async function ask(question: string, signalId?: number) {
    if (!question.trim() || busy) return;
    setOpen(true); setBusy(true); setQ("");
    const history = msgs.slice(-6).map((m) => ({ role: m.role, text: m.text.slice(0, 500) }));
    setMsgs((m) => [...m, { role: "you", text: question }]);
    try {
      const r = await post<any>("/api/assistant/ask", { question, signal_id: signalId ?? null, history });
      setMsgs((m) => [...m, { role: "engine", text: r.answer, data: r }]);
    } catch (e: any) {
      setMsgs((m) => [...m, { role: "engine", text: `Could not ask the engine: ${e.message}` }]);
    } finally { setBusy(false); }
  }
  async function decide(taskId: number, what: "approve" | "cancel") {
    setBusy(true);
    try {
      const r = await post<any>(`/api/agent/tasks/${taskId}/${what}`);
      setMsgs((m) => [...m.map((x) => x.data?.task?.id === taskId ? { ...x, data: { ...x.data, task: { ...x.data.task, status: r.status } } } : x),
        { role: "engine", text: what === "approve" ? r.answer : `Task #${taskId} cancelled; nothing was changed.` }]);
    } catch (e: any) {
      setMsgs((m) => [...m, { role: "engine", text: `Could not ${what} the plan: ${e.message}` }]);
    } finally { setBusy(false); }
  }
  useEffect(() => {
    const h = (e: any) => ask(e.detail.question, e.detail.signalId);
    window.addEventListener("rr-assistant", h);
    return () => window.removeEventListener("rr-assistant", h);
  });
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [msgs, open]);
  if (!open) return (
    <button type="button" onClick={() => setOpen(true)} aria-label="Ask the engine"
      className="fixed bottom-4 right-4 z-40 flex items-center gap-1.5 rounded-full border border-cyan/60 bg-panel px-3 py-2 text-[12px] text-cyan shadow-lg hover:bg-cyan/10">
      <MessageCircle size={15} />Ask the engine</button>
  );
  return (
    <div className="fixed bottom-4 right-4 z-40 flex h-[min(560px,calc(100vh-90px))] w-[min(420px,calc(100vw-32px))] flex-col rounded border border-cyan/50 bg-panel shadow-2xl" role="dialog" aria-label="Engine Assistant">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <div><div className="text-[12px] font-bold text-cyan">Engine Assistant</div>
          <div className="text-[10px] text-dim">Engine runtime · Anthropic API analysis · Claude Code (/code, reviewed patches only)</div></div>
        <button type="button" aria-label="Close" onClick={() => setOpen(false)}><X size={14} /></button>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-3 text-[12px]">
        {msgs.length === 0 && <div className="text-dim">Ask what the engine is doing, why a signal was not executed (for example "why was signal #42 not executed?"), or what to do next.</div>}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "you" ? "ml-8 rounded bg-cyan/10 px-2 py-1.5" : "mr-4 rounded border border-line px-2 py-1.5"}>
            <div className="whitespace-pre-wrap">{m.text}</div>
            {m.data?.diagnosis && <Checks d={m.data.diagnosis} />}
            {m.data?.task?.kind === "claude_code" && <ClaudeCodeTask initial={m.data.task} />}
            {m.data?.task?.kind !== "claude_code" && m.data?.task?.status === "AWAITING_APPROVAL" && <div className="mt-1 flex items-center gap-2 border-t border-line pt-1 text-[11px]">
              {admin ? <>
                <button type="button" disabled={busy} className="rounded border border-neon/60 px-2 py-0.5 text-neon" onClick={() => decide(m.data.task.id, "approve")}>Approve plan ({m.data.task.plan.length} safe action{m.data.task.plan.length === 1 ? "" : "s"})</button>
                <button type="button" disabled={busy} className="text-dim underline" onClick={() => decide(m.data.task.id, "cancel")}>Cancel</button>
              </> : <span className="text-dim">An administrator approves this plan.</span>}</div>}
            {m.data && <div className="mt-1 text-[10px] text-dim">{m.data.source === "ai" ? `answered by ${m.data.ai.provider}/${m.data.ai.model} from the engine's facts${m.data.analysis ? ` (${m.data.analysis.replace(/_/g, " ")})` : ""}` : m.data.source === "claude_code" ? "handled by Claude Code in the isolated workspace" : "answered by the engine"}</div>}
          </div>
        ))}
        {busy && <div className="text-dim">Checking the engine…</div>}
        <div ref={end} />
      </div>
      <div className="flex flex-wrap gap-1 border-t border-line px-3 pt-2">
        {QUICK.map((x) => <button key={x} type="button" disabled={busy} className="rounded border border-line px-1.5 py-0.5 text-[10px] text-dim hover:text-text" onClick={() => ask(x)}>{x}</button>)}
      </div>
      <form className="flex gap-1 p-3" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <input className="flex-1 rounded border border-line bg-transparent px-2 py-1 text-[12px]" value={q} maxLength={4000}
          placeholder="Ask the engine, or /code for a development task…" onChange={(e) => setQ(e.target.value)} />
        <button type="submit" disabled={busy || !q.trim()} aria-label="Send" className="rounded border border-cyan/50 px-2 text-cyan disabled:opacity-40"><Send size={13} /></button>
      </form>
    </div>
  );
}

function Checks({ d }: { d: any }) {
  return (
    <div className="mt-1 border-t border-line pt-1 text-[11px]">
      {d.checks.map((c: any) => (
        <div key={c.check}><span className={c.ok ? "text-neon" : c.ok === false ? "text-bear" : "text-dim"}>{c.ok ? "✔" : c.ok === false ? "✖" : "?"}</span> {c.label}: <span className="text-dim">{c.detail}</span></div>))}
      {d.next_step && <div className="mt-1 text-warn">Next: {d.next_step}</div>}
    </div>
  );
}
