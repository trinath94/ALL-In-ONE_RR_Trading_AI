"use client";
import { MessageCircle, Send, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { post } from "@/lib/api";

type Msg = { role: "you" | "engine"; text: string; data?: any };
const QUICK = ["Engine status", "Why was the last signal not executed?", "What should I do next?"];

/** Ask the engine from any page (or with a signal id from the Signal Centre). */
export function askAssistant(question: string, signalId?: number) {
  window.dispatchEvent(new CustomEvent("rr-assistant", { detail: { question, signalId } }));
}

/** Engine Assistant: a chat panel that explains what the engine is doing, where a valid signal stopped on its way to
 *  execution, and what to do next. Read-only: it never changes anything. */
export function AssistantPanel() {
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
          <div className="text-[10px] text-dim">Explains from the engine's records. Read-only: it never changes anything.</div></div>
        <button type="button" aria-label="Close" onClick={() => setOpen(false)}><X size={14} /></button>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-3 text-[12px]">
        {msgs.length === 0 && <div className="text-dim">Ask what the engine is doing, why a signal was not executed (for example "why was signal #42 not executed?"), or what to do next.</div>}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "you" ? "ml-8 rounded bg-cyan/10 px-2 py-1.5" : "mr-4 rounded border border-line px-2 py-1.5"}>
            <div className="whitespace-pre-wrap">{m.text}</div>
            {m.data?.diagnosis && <Checks d={m.data.diagnosis} />}
            {m.data && <div className="mt-1 text-[10px] text-dim">{m.data.source === "ai" ? `answered by ${m.data.ai.provider}/${m.data.ai.model} from the engine's facts` : "answered by the engine"}</div>}
          </div>
        ))}
        {busy && <div className="text-dim">Checking the engine…</div>}
        <div ref={end} />
      </div>
      <div className="flex flex-wrap gap-1 border-t border-line px-3 pt-2">
        {QUICK.map((x) => <button key={x} type="button" disabled={busy} className="rounded border border-line px-1.5 py-0.5 text-[10px] text-dim hover:text-text" onClick={() => ask(x)}>{x}</button>)}
      </div>
      <form className="flex gap-1 p-3" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <input className="flex-1 rounded border border-line bg-transparent px-2 py-1 text-[12px]" value={q} maxLength={1000}
          placeholder="Ask about the engine…" onChange={(e) => setQ(e.target.value)} />
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
