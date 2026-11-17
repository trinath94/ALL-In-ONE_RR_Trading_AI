"use client";
import { useState } from "react";
import { PageTitle } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const TONE: Record<string, any> = { PASS: "neon", ATTENTION: "bear", PENDING: "warn" };

/** Parts 27-30 Phase 9: DEMO acceptance evidence computed from what this installation recorded. Read-only. */
export default function Validation() {
  const [hours, setHours] = useState(72);
  const { data, error } = useApi<any>(`/api/validation/demo?hours=${hours}`, 30000);
  return (
    <div>
      <PageTitle sub="Evidence for DEMO acceptance, computed from what this installation recorded. PENDING means not enough activity yet and is never a pass. This page cannot change anything; LIVE stays an explicit administrator decision.">
        DEMO Validation</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <Stat label="Result" value={data ? (data.accepted ? "accepted" : "not yet") : "—"} tone={data?.accepted ? "up" : "warn"} />
        <Stat label="Pass" value={data?.counts.PASS ?? "—"} tone="up" />
        <Stat label="Attention" value={data?.counts.ATTENTION ?? "—"} tone={data?.counts.ATTENTION ? "down" : "plain"} />
        <Stat label="Pending" value={data?.counts.PENDING ?? "—"} tone={data?.counts.PENDING ? "warn" : "plain"} />
        <div className="rounded border border-line p-2 text-[11px]">
          <div className="text-dim">Window</div>
          <select className="mt-1 bg-transparent" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {[24, 72, 168, 720].map((h) => <option key={h} value={h}>{h < 168 ? `${h} h` : `${h / 24} days`}</option>)}</select>
        </div>
      </div>
      <Panel title={`Checklist${data ? " · generated " + fmt.time(data.generated_at) : ""}`}>
        <Table rows={data?.items} rowKey={(i) => i.key}
          cols={[{ k: "label", h: "Item" },
            { k: "status", h: "Status", r: (i) => <Badge tone={TONE[i.status]}>{i.status}</Badge> },
            { k: "detail", h: "Evidence", sort: false, cls: "whitespace-normal", r: (i) => <span className="text-dim">{i.detail}</span> }]} />
        {data && <div className="mt-2 text-[10px] text-dim">{data.note}</div>}
      </Panel>
    </div>
  );
}
