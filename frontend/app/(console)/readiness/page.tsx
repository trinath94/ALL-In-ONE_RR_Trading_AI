"use client";
import { PageTitle } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Table } from "@/components/ui";
import { useApi } from "@/lib/hooks";

/** Part 29.10: the checklist the engine's LIVE switch requires. Read-only: this page cannot enable anything. */
export default function Readiness() {
  const { data, error } = useApi<any>("/api/live/readiness", 15000);
  return (
    <div>
      <PageTitle sub="Every required item must pass before the engine accepts the typed LIVE switch. Passing the checklist does not enable LIVE: that stays an explicit administrator action, and the MT5 connector on the PC keeps its own LIVE lock.">
        LIVE Readiness</PageTitle>
      <ErrorLine error={error} />
      {data && (
        <div className="mb-3 text-[12px]">
          {data.ready ? <Badge tone="neon">all required items pass</Badge> : <Badge tone="bear">not ready: {data.failing.join(", ")}</Badge>}
        </div>)}
      <Panel title="Checklist">
        <Table rows={data?.items} rowKey={(i) => i.key}
          cols={[{ k: "label", h: "Item" },
            { k: "required", h: "Kind", r: (i) => i.required ? <Badge tone="warn">required</Badge> : <span className="text-dim">informational</span> },
            { k: "passed", h: "Status", r: (i) => i.passed === null ? <Badge tone="dim">n/a</Badge> : i.passed ? <Badge tone="neon">pass</Badge> : <Badge tone="bear">fail</Badge> },
            { k: "detail", h: "Detail", sort: false, cls: "whitespace-normal", r: (i) => <span className="text-dim">{i.detail}</span> }]} />
      </Panel>
    </div>
  );
}
