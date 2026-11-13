"use client";
import { Send } from "lucide-react";
import { useState } from "react";
import AgentsPanel from "@/components/Agents";
import ControlCenter from "@/components/ControlCenter";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, Confirm, ErrorLine, Field, inputCls, Panel, Table } from "@/components/ui";
import { api, fmt, post, rid } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function Terminal() {
  const me = useMe();
  const { data: sys } = useApi<any>("/api/system/status", 10000);
  const { data: syms } = useApi<any[]>("/api/symbols");
  const { data: st } = useApi<any>("/api/control/state");
  const [o, setO] = useState<any>({ symbol: "", direction: "BUY", order_type: "MARKET", price: "", stop_loss: "", take_profit: "", volume: "" });
  const [tick, setTick] = useState<any>(null);
  const [dlg, setDlg] = useState(false); const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null); const [res, setRes] = useState<any>(null);
  const b = sys?.services?.mt5_connector?.detail || {};
  const set = (k: string) => (e: any) => setO({ ...o, [k]: e.target.value });
  async function quote(sym: string) { setO({ ...o, symbol: sym }); try { setTick(await api(`/api/market/${encodeURIComponent(sym)}/tick`)); } catch { setTick(null); } }
  async function submit() {
    setBusy(true); setErr(null);
    try {
      setRes(await post("/api/orders", { ...o, price: o.price ? +o.price : null, stop_loss: +o.stop_loss, take_profit: o.take_profit ? +o.take_profit : null,
        volume: o.volume ? +o.volume : null, client_request_id: rid(), confirm_live: st?.mode === "LIVE" }));
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); setDlg(false); }
  }
  return (
    <div>
      <PageTitle sub="Windows MT5 connector, terminal health and manual order entry (always routed through the Risk Engine)">MT5 Terminal</PageTitle>
      <div className="grid gap-3 xl:grid-cols-3">
        <Panel title="Connector & terminal">
          <div className="grid grid-cols-2 gap-1 text-[11px]">
            {[["Connector", sys?.services?.mt5_connector?.status], ["Terminal connected", String(b.connected ?? "—")], ["Trade allowed", String(b.trade_allowed ?? "—")],
              ["Identity verified", String(b.identity_ok ?? "—")], ["Account", b.account_login ?? "—"], ["Server", b.server ?? "—"], ["Build", b.terminal_build ?? "—"],
              ["Ping", b.ping_last_us ? `${(b.ping_last_us / 1000).toFixed(1)} ms` : "—"], ["Unresolved executions", b.unresolved_executions ?? "—"],
              ["Bridge latency", b.latency_ms ? `${b.latency_ms} ms` : "—"], ["Last heartbeat", fmt.time(b.ts)], ["Error", b.error ?? "none"]].map(([k, v]) => (
              <div key={k as string} className="contents"><div className="text-dim">{k}</div><div className="truncate">{v as any}</div></div>))}
          </div>
        </Panel>
        <Panel title="Manual order">
          {can(me, "trade.manual") ? <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <Field label="Symbol"><select className={inputCls} value={o.symbol} onChange={(e) => quote(e.target.value)}><option value="">choose…</option>{(syms || []).filter((s) => s.tradable).map((s) => <option key={s.id}>{s.symbol}</option>)}</select></Field>
              <Field label="Side"><select className={inputCls} value={o.direction} onChange={set("direction")}><option>BUY</option><option>SELL</option></select></Field>
              <Field label="Type"><select className={inputCls} value={o.order_type} onChange={set("order_type")}><option>MARKET</option><option>LIMIT</option><option>STOP</option></select></Field>
              <Field label="Price (pending)"><input className={inputCls} disabled={o.order_type === "MARKET"} value={o.price} onChange={set("price")} /></Field>
              <Field label="Stop loss (required)"><input className={inputCls} value={o.stop_loss} onChange={set("stop_loss")} /></Field>
              <Field label="Take profit"><input className={inputCls} value={o.take_profit} onChange={set("take_profit")} /></Field>
              <Field label="Max lots (optional cap)"><input className={inputCls} value={o.volume} onChange={set("volume")} /></Field>
              <div className="self-end text-[11px] text-dim">{tick ? `bid ${tick.bid} · ask ${tick.ask}` : "no quote"}</div>
            </div>
            <ErrorLine error={err} />
            {res && <div className="text-[11px]"><Badge tone={res.status === "CONFIRMED" ? "neon" : "warn"}>{res.status}</Badge> {res.volume} lots · {res.status_detail}</div>}
            <Btn tone={o.direction === "BUY" ? "neon" : "bear"} disabled={!o.symbol || !o.stop_loss} onClick={() => setDlg(true)}><Send size={12} />Submit to Risk Engine</Btn>
            <Confirm open={dlg} title={`${st?.mode} ${o.direction} ${o.symbol}`} tone={st?.mode === "LIVE" ? "bear" : "neon"} busy={busy}
              phrase={st?.mode === "LIVE" ? "CONFIRM LIVE" : undefined} body="Position size is calculated by the Risk Engine from live symbol specifications; the order may be rejected."
              onCancel={() => setDlg(false)} onConfirm={submit} />
          </div> : <div className="text-[12px] text-dim">Read-only access</div>}
        </Panel>
        <ControlCenter />
      </div>
      <AgentsPanel />
      <Panel title="Broker instruments" className="mt-3">
        <Table rows={syms} empty="No symbols discovered"
          cols={[{ k: "symbol", h: "Broker symbol" }, { k: "canonical", h: "Canonical" }, { k: "category", h: "Class" }, { k: "digits", h: "Digits" },
            { k: "contract_size", h: "Contract" }, { k: "volume_min", h: "Min lot" }, { k: "volume_step", h: "Step" },
            { k: "tradable", h: "Tradable", r: (s) => <Badge tone={s.tradable ? "neon" : "dim"}>{s.tradable ? "yes" : "no"}</Badge> },
            { k: "scan_enabled", h: "Scanned", r: (s) => s.scan_enabled ? "yes" : "no" }]} />
      </Panel>
    </div>
  );
}
