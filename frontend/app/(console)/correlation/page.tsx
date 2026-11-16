"use client";
import { PageTitle } from "@/components/Shell";
import { Badge, Empty, Panel, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

function Matrix({ names, matrix }: { names: string[]; matrix: number[][] }) {
  const cell = (v: number) => {
    const a = Math.min(Math.abs(v), 1);
    return { background: v >= 0 ? `rgba(0,255,163,${a * 0.6})` : `rgba(255,77,109,${a * 0.6})` };
  };
  return (
    <div className="overflow-x-auto"><table className="text-[10px]">
      <thead><tr><th />{names.map((s) => <th key={s} className="px-1 text-dim [writing-mode:vertical-rl]">{s}</th>)}</tr></thead>
      <tbody>{matrix.map((row, i) => <tr key={i}><td className="pr-2 text-dim">{names[i]}</td>
        {row.map((v, j) => <td key={j} className="h-6 w-8 text-center" style={cell(v)} title={`${names[i]} × ${names[j]}: ${v}`}>{fmt.num(v, 2)}</td>)}</tr>)}</tbody>
    </table></div>
  );
}

/** Part 17 item 15: how strategies relate, from four independent records: realised P&L, scanner co-firing, the
 *  specialists' votes and code fingerprints. Report only: no strategy is merged, disabled or removed. */
export default function StrategyCorrelation() {
  const { data: pnl } = useApi<any>("/api/strategies-analytics/correlation", 120000);
  const { data: red } = useApi<any>("/api/intelligence/adaptive/redundancy", 120000);
  const { data: an } = useApi<any>("/api/intelligence/consensus/analytics", 120000);
  const vc = an?.vote_correlation;
  const co = red?.co_firing;
  return (
    <div>
      <PageTitle sub="Four independent views of how strategies relate. Correlated evidence is counted once by the consensus; nothing here changes a strategy.">
        Strategy Correlation</PageTitle>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title={`Daily realised P&L correlation${pnl?.days ? ` (${pnl.days} days)` : ""}`}>
          {pnl?.strategies?.length ? <Matrix names={pnl.strategies} matrix={pnl.matrix} /> : <Empty>{pnl?.note || "No closed trades with strategies yet."}</Empty>}
        </Panel>
        <Panel title={`Specialist vote correlation${vc?.rows ? ` (${vc.rows} decisions)` : ""}`}>
          {vc?.available ? <>
            <Matrix names={vc.specialists} matrix={vc.matrix} />
            <div className="mt-1 text-[11px] text-dim">Effective independent specialists: <b>{vc.effective_independent_specialists}</b> of {vc.specialists.length}.</div>
          </> : <Empty>{vc?.note || "Not enough consensus decisions yet."}</Empty>}
        </Panel>
        <Panel title={`Co-firing in the scanner (${co?.days ?? 30} days, ${co?.analyses ?? 0} analyses)`}>
          <Table rows={[...(co?.redundant || []), ...(co?.conflicting || [])]} rowKey={(p) => `${p.a}${p.b}`} empty="No pair of strategies fired together 20 times."
            cols={[{ k: "kind", h: "Kind", r: (p) => <Badge tone={p.kind === "redundant" ? "warn" : "bear"}>{p.kind}</Badge> }, { k: "a", h: "A" }, { k: "b", h: "B" },
              { k: "together", h: "Together" }, { k: "jaccard", h: "Overlap", r: (p) => fmt.num(p.jaccard) },
              { k: "same_direction", h: "Same direction" }, { k: "opposite_direction", h: "Opposite" }]} />
        </Panel>
        <Panel title={`Code fingerprints (${red?.fingerprints?.fingerprints ?? 0} strategies)`}>
          <Table rows={red?.fingerprints?.static} rowKey={(p) => `${p.a}${p.b}`} empty="No pair with near-identical construction."
            cols={[{ k: "a", h: "A" }, { k: "b", h: "B" }, { k: "kind", h: "Kind", r: (p) => p.kind.replace(/_/g, " ") },
              { k: "similarity", h: "Similarity", r: (p) => fmt.num(p.similarity) }]} />
        </Panel>
      </div>
    </div>
  );
}
