"use client";
import { useState } from "react";
import { BacktestForm, JobView } from "@/components/Backtest";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Empty, Panel, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function Optimisation() {
  const me = useMe();
  const { data: jobs, reload } = useApi<any[]>("/api/backtests", 5000);
  const [sel, setSel] = useState<number | null>(null);
  const opt = (jobs || []).filter((j) => ["OPTIMISE", "WALKFORWARD"].includes(j.kind));
  return (
    <div>
      <PageTitle sub="Candidates are ranked in-sample but always shown with their out-of-sample result. Optimised parameters are never applied to live automatically.">Strategy Optimisation</PageTitle>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Grid search / walk-forward">{can(me, "backtest.run") ? <BacktestForm defaultKind="WALKFORWARD" kinds={["WALKFORWARD", "OPTIMISE"]} onCreated={(id) => { setSel(id); reload(); }} /> : <Empty>Requires backtest permission</Empty>}</Panel>
        <Panel title="Optimisation jobs"><Table rows={opt} onRow={(j) => setSel(j.id)} empty="No optimisation runs yet"
          cols={[{ k: "id", h: "#" }, { k: "kind", h: "Type" }, { k: "s", h: "Strategy", r: (j) => j.params.strategies[0] }, { k: "sym", h: "Symbol", r: (j) => j.params.symbol },
            { k: "grid", h: "Grid", r: (j) => <span className="text-dim">{JSON.stringify(j.params.grid)}</span> },
            { k: "status", h: "Status", r: (j) => <Badge tone={j.status === "DONE" ? "neon" : j.status === "FAILED" ? "bear" : "cyan"}>{j.status}</Badge> },
            { k: "c", h: "Created", r: (j) => fmt.time(j.created_at) }]} /></Panel>
      </div>
      {sel && <Panel title={`Results · job #${sel}`} className="mt-3"><JobView id={sel} /></Panel>}
    </div>
  );
}
