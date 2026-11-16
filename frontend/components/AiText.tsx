"use client";
import { useState } from "react";
import { can, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Panel } from "@/components/ui";
import { post } from "@/lib/api";

/** Part 14: plain-language explanation of a recorded result (task 'learning_explanation'). AI-generated text, shown as
 *  such; it is not evidence and changes nothing. */
export function ExplainButton({ subject, id }: { subject: "model" | "fusion"; id: number }) {
  const me = useMe();
  const [res, setRes] = useState<any>(null); const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  if (!can(me, "strategy.manage")) return null;
  async function go() {
    setBusy(true); setErr(null);
    try { setRes(await post("/api/learning/explain", { subject, id })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  return (
    <div className="mt-2 text-[11px]">
      {!res && <Btn small tone="violet" busy={busy} onClick={go}>Explain in plain language (AI)</Btn>}
      <ErrorLine error={err} />
      {res && <div className="rounded border border-violet/40 p-2">
        <div className="mb-1 flex flex-wrap items-center gap-2"><Badge tone="violet">AI-generated</Badge><span className="text-dim">{res.label} · {res.provider} · {res.model}</span></div>
        <p>{res.summary}</p>
        {[["Key points", res.key_points], ["Limitations", res.limitations], ["What would change it", res.what_would_change_it]].map(([h, xs]: any) => xs?.length ? (
          <div key={h} className="mt-1"><span className="text-dim">{h}:</span><ul className="list-disc pl-4">{xs.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></div>) : null)}
      </div>}
    </div>
  );
}

/** Part 14: digest of recent research records (task 'research_summary'), stored as an OPEN recommendation. */
export function ResearchSummaryPanel() {
  const me = useMe();
  const [res, setRes] = useState<any>(null); const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const [days, setDays] = useState(14);
  async function go() {
    setBusy(true); setErr(null);
    try { setRes(await post("/api/learning/research-summary", { days })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  return (
    <Panel title="Research summary (AI digest)" right={can(me, "strategy.manage") && <div className="flex items-center gap-1">
      <select className="rounded border border-line bg-void px-2 py-1 text-[11px]" value={days} onChange={(e) => setDays(Number(e.target.value))}>{[7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} days</option>)}</select>
      <Btn small tone="violet" busy={busy} onClick={go}>Summarise</Btn></div>}>
      <ErrorLine error={err} />
      {!res ? <div className="text-[11px] text-dim">Summarises the research records of the period (trade research, retraining recommendations, strategy proposals, model and profile validations, fusion versus the original engine), citing the records it used. Stored with the recommendations for reading; it applies nothing.</div> : (
        <div className="text-[11px]">
          <div className="mb-1 flex flex-wrap items-center gap-2"><Badge tone="violet">AI-generated</Badge><b>{res.headline}</b><span className="text-dim">{res.provider} · {res.model} · recommendation {res.recommendation_id}</span></div>
          {res.items.map((x: any, i: number) => <div key={i} className="mt-1"><span className="text-cyan">{x.topic}:</span> {x.summary} <span className="text-dim">[{x.sources.join(", ")}]</span></div>)}
          {res.open_questions?.length > 0 && <div className="mt-1"><span className="text-dim">Open questions:</span> {res.open_questions.join(" · ")}</div>}
        </div>)}
    </Panel>
  );
}
