"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { BacktestForm, JobView } from "@/components/Backtest";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Empty, Panel, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

function Lab() {
  const me = useMe();
  const qp = useSearchParams();
  const { data: jobs, reload } = useApi<any[]>("/api/backtests", 5000);
  const [sel, setSel] = useState<number | null>(qp.get("job") ? Number(qp.get("job")) : null);
  return (
    <div>
      <PageTitle sub="Uses real broker history from MT5. Next-bar fills, spread/commission/slippage, pessimistic intrabar handling, closed-bar HTF only.">Backtesting Lab</PageTitle>
      <div className="grid gap-3 xl:grid-cols-[1fr_1fr]">
        <Panel title="New test">{can(me, "backtest.run") ? <BacktestForm onCreated={(id) => { setSel(id); reload(); }} /> : <Empty>Requires backtest permission</Empty>}</Panel>
        <Panel title="Jobs"><Table rows={jobs} onRow={(j) => setSel(j.id)} empty="No jobs yet"
          cols={[{ k: "id", h: "#" }, { k: "kind", h: "Type" }, { k: "symbol", h: "Symbol", r: (j) => j.params.symbol }, { k: "tf", h: "TF", r: (j) => j.params.timeframe },
            { k: "s", h: "Strategies", r: (j) => <span className="block max-w-[200px] truncate">{j.params.strategies.join(", ")}</span> },
            { k: "status", h: "Status", r: (j) => <Badge tone={j.status === "DONE" ? "neon" : j.status === "FAILED" ? "bear" : "cyan"}>{j.status}</Badge> },
            { k: "created_at", h: "Created", r: (j) => fmt.time(j.created_at) }]} /></Panel>
      </div>
      {sel && <Panel title={`Results · job #${sel}`} className="mt-3"><JobView id={sel} /></Panel>}
    </div>
  );
}

export default function Page() { return <Suspense><Lab /></Suspense>; }
