"use client";
import { useApi } from "@/lib/hooks";
import { Badge } from "./ui";

const list = (xs: any[]) => (xs && xs.length ? xs.join(" · ") : "—");

/** The structured pre-trade analysis of an opportunity (from its decision trace, recorded before any order). */
export function Thesis({ id }: { id: number }) {
  const { data: t } = useApi<any>(`/api/opportunities/${id}/thesis`);
  if (!t) return null;
  const row = (h: string, v: any) => <div className="grid grid-cols-[150px_1fr] gap-2"><span className="text-dim">{h}</span><span>{v}</span></div>;
  return (
    <div className="mt-2 rounded border border-line p-2 text-[11px]">
      <div className="mb-1 flex items-center gap-2 text-[10px] uppercase text-dim">Pre-trade analysis
        {t.should_reject ? <Badge tone="bear">should be rejected</Badge> : <Badge tone="neon">no reason to reject</Badge>}</div>
      {row("Why", t.why)}
      {row("Supporting strategies", list((t.supporting_strategies || []).map((x: any) => `${x.strategy} (${x.strength})`)))}
      {row("Favourable", list(t.favourable_conditions))}
      {row("Contradicting", list(t.contradicting_conditions))}
      {row("Invalidation", list(t.invalidation))}
      {row("Entry", `${t.entry_justification.outcome}${(t.entry_justification.checks || []).filter((c: any) => !c.ok).length ? ": failed " + t.entry_justification.checks.filter((c: any) => !c.ok).map((c: any) => c.check).join(", ") : ""}`)}
      {row("Stop", t.stop_justification === "unavailable" ? "unavailable" : `risk distance ${t.stop_justification.risk_distance}`)}
      {row("Targets", t.target_justification === "unavailable" ? "unavailable" : list(t.target_justification.map((x: any) => `${x.target} ${x.r_multiple}R`)))}
      {row("Could fail because", `${t.failure_risks.level}: ${list((t.failure_risks.factors || []).map((f: any) => f.factor || f.name || JSON.stringify(f)))}`)}
      {t.should_reject && row("Reject reasons", list(t.reject_reasons))}
    </div>
  );
}

/** The structured post-trade review recorded by the learner. */
export function TradeReview({ closedId }: { closedId: number }) {
  const { data: r, error } = useApi<any>(`/api/trades/${closedId}/review`);
  if (error) return <div className="text-[11px] text-dim">{String((error as any).message || error)}</div>;
  if (!r || !r.review) return <div className="text-[11px] text-dim">Review not available yet.</div>;
  const v = r.review;
  const tone: any = { win: "neon", break_even: "dim", normal_loss: "cyan", mistake: "warn", recurring_defect_suspected: "bear" };
  return (
    <div className="text-[11px]">
      <div className="mb-1 flex items-center gap-2"><Badge tone={tone[v.verdict]}>{v.verdict.replaceAll("_", " ")}</Badge><span>{v.summary}</span>
        <span className="text-dim">net {v.net_profit}</span></div>
      {Object.entries(v.questions).map(([k, q]: any) => (
        <div key={k} className="grid grid-cols-[170px_60px_1fr] gap-2"><span className="text-dim">{k.replaceAll("_", " ")}</span>
          <Badge tone={q.ok ? "neon" : "bear"}>{q.ok ? "yes" : "no"}</Badge><span className="text-dim">{q.detail}</span></div>))}
      {v.improvements.length > 0 && <div className="mt-1">Improve: {v.improvements.join(" · ")}</div>}
      {v.loss_significance.map((s: any) => <div key={s.strategy} className="mt-1 text-dim">{s.strategy}: {s.detail || s.basis}</div>)}
    </div>
  );
}
