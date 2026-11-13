"use client";
import { RefreshCw } from "lucide-react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Panel, Table } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function Economic() {
  const me = useMe();
  const { data, error, reload } = useApi<any>("/api/news?hours=96", 60000);
  const ok = data?.provider_status === "CONNECTED";
  return (
    <div>
      <PageTitle sub="High-impact events and central-bank decisions block new entries inside their configured risk windows."
        right={<div className="flex items-center gap-2"><Badge tone={ok ? "neon" : "bear"}>Provider {data?.provider_status || "…"}</Badge>
          {can(me, "system.manage") && <Btn small onClick={async () => { await post("/api/news/refresh"); reload(); }}><RefreshCw size={11} />Refresh</Btn>}</div>}>
        Economic Intelligence</PageTitle>
      <ErrorLine error={error} />
      {!ok && data && <div className="mb-3 rounded border border-warn/50 bg-warn/5 p-2 text-[12px] text-warn">{data.note} Configure RR_NEWS_PROVIDER / RR_NEWS_API_URL.</div>}
      <Panel>
        <Table rows={data?.events} empty={ok ? "No scheduled events in range" : "No calendar data available — news risk is UNKNOWN"}
          cols={[{ k: "time", h: "Scheduled", r: (e) => fmt.time(e.time) }, { k: "currency", h: "Ccy" },
            { k: "impact", h: "Impact", r: (e) => <Badge tone={e.impact === "HIGH" ? "bear" : e.impact === "MEDIUM" ? "warn" : "dim"}>{e.impact}</Badge> },
            { k: "title", h: "Event" }, { k: "forecast", h: "Fcst" }, { k: "previous", h: "Prev" }, { k: "actual", h: "Actual" },
            { k: "affected_symbols", h: "Affected", sort: false, r: (e) => <span className="block max-w-[220px] truncate text-dim">{e.affected_symbols.join(", ")}</span> },
            { k: "risk_window", h: "Risk window", sort: false, r: (e) => <span className="text-dim">{fmt.time(e.risk_window[0]).slice(-8)} → {fmt.time(e.risk_window[1]).slice(-8)}</span> },
            { k: "restricted_now", h: "Restriction", r: (e) => e.restricted_now ? <Badge tone="bear">ACTIVE</Badge> : <Badge tone="dim">inactive</Badge> }]} />
      </Panel>
    </div>
  );
}
