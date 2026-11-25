"use client";
import { Users } from "lucide-react";
import { fmt } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";
import { Badge, Panel, Table } from "./ui";

/** Multi-account: which accounts the engine trades now, and each one's reconciliation, connector, equity and open
 *  positions. Read-only; settings are on each account's Settings panel. */
export default function ExecutionAccounts() {
  const live = useLive(["account", "control", "positions"]);
  const { data } = useApi<any>("/api/accounts/status", 15000, live.tick);
  return (
    <Panel title={`Execution accounts${data ? ` · ${data.active_mode}` : ""}`} icon={<Users size={12} />}>
      <Table rows={data?.accounts} rowKey={(a) => a.id} empty="No trading accounts configured"
        cols={[
          { k: "label", h: "Account", r: (a) => <span>{a.label || a.login} <span className="text-dim">{a.login}@{a.server}</span></span> },
          { k: "mode", h: "Mode", r: (a) => <Badge tone={a.mode === "LIVE" ? "bear" : "cyan"}>{a.mode}</Badge> },
          { k: "trading_now", h: "Traded now", r: (a) => a.trading_now ? <Badge tone="neon">yes</Badge>
              : <span className="text-dim" title={a.not_trading_reason}>{a.not_trading_reason}</span> },
          { k: "reconciled_now", h: "Reconciled", r: (a) => !a.trading_now ? "—" : a.reconciled_now ? <Badge tone="neon">yes</Badge> : <Badge tone="warn">pending</Badge> },
          { k: "connector", h: "Connector", r: (a) => a.is_active && !a.connector ? <span className="text-dim">engine bridge</span>
              : a.connector ? <Badge tone={a.connector.online ? "neon" : "bear"}>{a.connector.online ? "online" : "offline"}</Badge>
              : a.trading_now ? <Badge tone="bear">none</Badge> : "—" },
          { k: "equity", h: "Equity", r: (a) => a.equity == null ? "—" : <span className="tabular">{fmt.num(a.equity)}</span> },
          { k: "open_positions", h: "Open", r: (a) => a.open_positions },
          { k: "own_risk_limits", h: "Own limits", sort: false, r: (a) => a.own_risk_limits.length ? a.own_risk_limits.length : <span className="text-dim">template</span> },
        ]} />
      {data?.bridge_mode !== "relay" && (data?.accounts || []).filter((a: any) => a.trading_now).length > 1 &&
        <div className="mt-2 text-[11px] text-warn">Trading several accounts needs relay mode: one RR MT5 Connector per MT5 terminal. Only the active account is reachable now.</div>}
    </Panel>
  );
}
