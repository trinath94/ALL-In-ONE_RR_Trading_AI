"use client";
import { useState } from "react";
import { PageTitle } from "@/components/Shell";
import { Badge, dirTone, ErrorLine, inputCls, Panel, Stat, Table, toneOf } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function History() {
  const [sym, setSym] = useState("");
  const { data, error } = useApi<any[]>(`/api/positions/history?limit=2000${sym ? `&symbol=${encodeURIComponent(sym)}` : ""}`);
  const rows = data || [];
  const net = rows.reduce((a, r) => a + r.profit + r.commission + r.swap, 0);
  const wins = rows.filter((r) => r.profit > 0);
  const gp = wins.reduce((a, r) => a + r.profit, 0), gl = -rows.filter((r) => r.profit <= 0).reduce((a, r) => a + r.profit, 0);
  const rs = rows.filter((r) => r.r_multiple !== null);
  return (
    <div>
      <PageTitle sub="Closed positions reconciled from MT5 deal history">Trade History</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <Stat label="Trades" value={rows.length} />
        <Stat label="Net P&L" value={fmt.num(net)} tone={toneOf(net)} />
        <Stat label="Win rate" value={rows.length ? fmt.pct(wins.length / rows.length, 1) : "—"} />
        <Stat label="Profit factor" value={gl > 0 ? fmt.num(gp / gl) : "—"} />
        <Stat label="Avg R" value={rs.length ? fmt.num(rs.reduce((a, r) => a + r.r_multiple, 0) / rs.length) : "—"} />
      </div>
      <Panel right={<input placeholder="filter by broker symbol" value={sym} onChange={(e) => setSym(e.target.value)} className={inputCls + " w-48"} />}>
        <Table rows={rows} empty="No closed trades yet" rowKey={(r) => r.ticket}
          cols={[{ k: "closed_at", h: "Closed", r: (r) => fmt.time(r.closed_at) }, { k: "ticket", h: "Ticket" }, { k: "symbol", h: "Symbol" },
            { k: "direction", h: "Dir", r: (r) => <Badge tone={dirTone(r.direction)}>{r.direction}</Badge> }, { k: "volume", h: "Lots" },
            { k: "entry", h: "Entry", r: (r) => fmt.px(r.entry) }, { k: "exit", h: "Exit", r: (r) => fmt.px(r.exit) },
            { k: "profit", h: "P&L", r: (r) => <span className={r.profit > 0 ? "text-neon" : "text-bear"}>{fmt.num(r.profit)}</span> },
            { k: "r_multiple", h: "R", r: (r) => fmt.num(r.r_multiple) }, { k: "reason", h: "Exit reason" },
            { k: "strategies", h: "Strategies", sort: false, r: (r) => <span className="text-dim">{(r.strategies || []).join(", ") || "—"}</span> }]} />
      </Panel>
    </div>
  );
}
