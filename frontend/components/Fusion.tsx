"use client";
import { Badge, dirTone, Panel, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const STATUS: Record<string, any> = { SUPPORTS: "neon", OPPOSES: "bear", BLOCKS: "bear", CAUTION: "warn", NEUTRAL: "dim", UNAVAILABLE: "dim" };
const EVIDENCE: Record<string, [any, string]> = {
  CALIBRATED: ["neon", "calibrated"], LOW_DATA_CONFIDENCE: ["warn", "low data confidence"], INSUFFICIENT_DATA: ["dim", "insufficient data"],
};

export function EvidenceBadge({ e }: { e: string }) {
  const [tone, label] = EVIDENCE[e] || ["dim", e];
  return <Badge tone={tone}>{label}</Badge>;
}

/** Part 13: the fused decision for one analysis, every factor, and the original engine's decision (the benchmark). */
export function FusionDetail({ f }: { f: any }) {
  if (!f) return <div className="text-[11px] text-dim">No fused decision recorded for this signal (generated before Phase 13).</div>;
  if (f.error) return <div className="text-[11px] text-warn">Fusion unavailable for this analysis: {f.error}</div>;
  const changed = f.decision !== f.benchmark.decision;
  return (
    <div className="text-[11px]">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className="text-dim">Fused</span><Badge tone={dirTone(f.decision)}>{f.decision}{f.decision === "WAIT" && f.direction ? ` ${f.direction}` : ""}</Badge>
        <span className="text-dim">original engine</span><Badge tone={dirTone(f.benchmark.decision)}>{f.benchmark.decision} {f.benchmark.grade}</Badge>
        {changed && <Badge tone="warn">more cautious</Badge>}
        <EvidenceBadge e={f.evidence} />
        <Badge tone={f.mode === "enforce" ? "cyan" : "dim"}>{f.mode === "enforce" ? "used for the signal" : "advisory"}</Badge>
        {!f.executable && <span className="text-dim">not executable</span>}
      </div>
      <div className="mb-1">
        {f.probability ? <span>{f.probability.kind}: <b>{fmt.pct(f.probability.value, 1)}</b> <span className="text-dim">({f.probability.source}, n={f.probability.samples})</span></span>
          : <span className="text-dim">No calibrated probability: {EVIDENCE[f.evidence]?.[1] || f.evidence}.</span>}
        <span className="ml-2 text-dim">{f.score.label}: {f.score.value}</span>
      </div>
      <Table rows={f.factors} rowKey={(x) => x.factor}
        cols={[{ k: "factor", h: "Factor", r: (x) => x.factor.replace(/_/g, " ") }, { k: "status", h: "", r: (x) => <Badge tone={STATUS[x.status]}>{x.status.toLowerCase()}</Badge> },
          { k: "effect", h: "Effect", r: (x) => x.effect === "explains" ? <span className="text-dim">explains</span> : <span className="text-warn">{x.effect.replace(/_/g, " ")}</span> },
          { k: "detail", h: "Evidence", cls: "whitespace-normal", sort: false }, { k: "source", h: "Source", r: (x) => <span className="text-dim">{x.source}</span> }]} />
      <p className="mt-1 text-dim">{f.explanation}</p>
    </div>
  );
}

/** Latest fused decisions and how they compare with the original engine's. */
export function FusionOverview() {
  const { data: latest } = useApi<any>("/api/intelligence/fusion/latest?limit=50", 30000);
  const { data: bench } = useApi<any>("/api/intelligence/fusion/benchmark?days=30", 60000);
  return (
    <Panel title={`Signal fusion · ${latest?.mode === "enforce" ? "used for signals" : "advisory (original engine decides)"}`}>
      {bench && <div className="mb-2 flex flex-wrap gap-3 text-[11px]">
        <span>{bench.fused_analyses} analyses in {bench.days} days</span>
        <span>differ from the original engine: <b>{bench.differ}</b>{Object.keys(bench.changes || {}).length ? ` (${Object.entries(bench.changes).map(([k, v]) => `${k}: ${v}`).join(", ")})` : ""}</span>
        <span>held-back trades with an outcome: {bench.held_back_trades_with_outcome}{bench.r_avoided != null ? `, R avoided ${fmt.num(bench.r_avoided, 2)}` : ""}</span>
        <Badge tone={bench.verdict === "FUSION_AVOIDED_LOSSES" ? "neon" : bench.verdict === "FUSION_HELD_BACK_WINNERS" ? "bear" : "dim"}>{bench.verdict.replace(/_/g, " ").toLowerCase()}</Badge>
        {Object.entries(bench.evidence || {}).map(([k, v]: any) => <span key={k}><EvidenceBadge e={k} /> {v}</span>)}
      </div>}
      <Table rows={latest?.decisions} rowKey={(r) => r.analysis_id} empty="No fused decisions yet: they are recorded with every scan analysis."
        cols={[{ k: "at", h: "Time", r: (r) => fmt.time(r.at) }, { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
          { k: "benchmark", h: "Original", sort: false, r: (r) => <Badge tone={dirTone(r.benchmark.decision)}>{r.benchmark.decision}</Badge> },
          { k: "decision", h: "Fused", r: (r) => <Badge tone={dirTone(r.decision)}>{r.decision}</Badge> },
          { k: "evidence", h: "Evidence", r: (r) => <EvidenceBadge e={r.evidence} /> },
          { k: "prob", h: "Probability", sort: false, r: (r) => r.probability ? fmt.pct(r.probability.value, 1) : <span className="text-dim">—</span> },
          { k: "blocking", h: "Blocks / downgrades", sort: false, r: (r) => r.factors.filter((x: any) => x.effect !== "explains").map((x: any) => x.factor.replace(/_/g, " ")).join(", ") || "—" }]} />
      <div className="mt-1 text-[10px] text-dim">The fused decision can only keep the original engine's decision or make it more cautious (WAIT, NO TRADE); it never reverses it. Probabilities are shown only when calibrated out of sample; the confluence score is not a probability.</div>
    </Panel>
  );
}
