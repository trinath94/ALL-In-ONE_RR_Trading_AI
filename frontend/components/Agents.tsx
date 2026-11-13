"use client";
import { KeyRound, MonitorSmartphone } from "lucide-react";
import { useState } from "react";
import { can, useMe } from "@/components/Shell";
import { Badge, Btn, Confirm, ErrorLine, inputCls, Panel, Table } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/** RR MT5 Connector desktop agents: status, one-time registration tokens, revocation. */
export default function AgentsPanel() {
  const me = useMe();
  const { data, error, reload } = useApi<any>("/api/v1/mt5/status", 5000);
  const [label, setLabel] = useState("");
  const [token, setToken] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [revoke, setRevoke] = useState<any>(null);
  const admin = can(me, "system.manage");
  async function create() {
    setErr(null);
    try { setToken(await post("/api/v1/mt5/agent/tokens", { label })); setLabel(""); } catch (e: any) { setErr(e.message); }
  }
  return (
    <Panel title="RR MT5 Connector agents" icon={<MonitorSmartphone size={13} />} className="mt-3"
      right={<span className="text-[11px] text-dim">bridge mode <Badge tone={data?.bridge_mode === "relay" ? "neon" : "dim"}>{data?.bridge_mode ?? "—"}</Badge></span>}>
      <ErrorLine error={error || err} />
      {data?.bridge_mode !== "relay" && <div className="mb-2 text-[11px] text-warn">
        The engine is in <b>direct</b> mode (it calls a connector on the Windows PC). Set RR_BRIDGE_MODE=relay to trade through the desktop
        connector over its outbound connection.</div>}
      <Table rows={data?.agents} rowKey={(a) => a.id} empty="No desktop connector registered yet"
        cols={[{ k: "name", h: "PC" }, { k: "online", h: "Link", r: (a) => <Badge tone={a.online ? "neon" : "bear"}>{a.online ? "online" : "offline"}</Badge> },
          { k: "selected", h: "Routes trades", sort: false, r: (a) => a.id === data?.selected_agent_id ? <Badge tone="cyan">active</Badge> : "" },
          { k: "acct", h: "MT5 account", sort: false, r: (a) => a.account_login ? `${a.account_login} @ ${a.account_server}` : "—" },
          { k: "type", h: "Type", sort: false, r: (a) => { const t = a.status?.account?.account_type; return t ? <Badge tone={t === "DEMO" ? "cyan" : "bear"}>{t}</Badge> : "—"; } },
          { k: "eq", h: "Balance / equity", sort: false, r: (a) => a.status?.account?.balance != null ? `${fmt.num(a.status.account.balance)} / ${fmt.num(a.status.account.equity)} ${a.status.account.currency ?? ""}` : "—" },
          { k: "term", h: "Terminal", sort: false, r: (a) => a.status?.terminal ? `${a.status.terminal.connected ? "online" : "offline"}${a.status.terminal.trade_allowed === false ? " · algo off" : ""}` : "—" },
          { k: "live", h: "PC live auth", sort: false, r: (a) => a.status?.live_authorized ? <Badge tone="bear">authorised</Badge> : <span className="text-dim">no</span> },
          { k: "stop", h: "PC stop", sort: false, r: (a) => a.status?.local_kill_switch ? <Badge tone="bear">STOPPED</Badge> : "" },
          { k: "lat", h: "Latency", sort: false, r: (a) => a.status?.engine_rtt_ms != null ? `${Math.round(a.status.engine_rtt_ms)} ms` : "—" },
          { k: "last_seen_at", h: "Last seen", r: (a) => fmt.time(a.last_seen_at) }, { k: "app_version", h: "Ver" },
          { k: "x", h: "", sort: false, r: (a) => admin && a.enabled ? <Btn small tone="bear" onClick={() => setRevoke(a)}>Revoke</Btn> : (!a.enabled ? <Badge tone="dim">revoked</Badge> : null) }]} />
      {admin && <div className="mt-3 flex flex-wrap items-end gap-2">
        <input className={inputCls + " w-56"} placeholder="PC label (optional)" value={label} onChange={(e) => setLabel(e.target.value)} />
        <Btn onClick={create}><KeyRound size={12} />Create registration token</Btn>
        {token && <div className="w-full rounded border border-neon/40 bg-neon/5 p-2 text-[11px]">
          <div className="mb-1 text-dim">Shown once. Enter it in RR MT5 Connector setup (with this engine&apos;s HTTPS address). Expires {fmt.time(token.expires_at)}.</div>
          <code className="select-all break-all text-neon">{token.token}</code></div>}
      </div>}
      <Confirm open={!!revoke} title={`Revoke ${revoke?.name}?`} tone="bear" body="The PC is disconnected immediately and can no longer reach MT5 for the engine until it is registered again."
        onCancel={() => setRevoke(null)} onConfirm={async () => { try { await post(`/api/v1/mt5/agents/${revoke.id}/revoke`); } catch (e: any) { setErr(e.message); } setRevoke(null); reload(); }} />
    </Panel>
  );
}
