"use client";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, Confirm, dirTone, ErrorLine, Field, inputCls, Panel, Table, toneOf } from "@/components/ui";
import { fmt, post, rid } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

export default function Trades() {
  const me = useMe();
  const live = useLive(["positions", "order"]);
  const { data: rows, error, reload } = useApi<any[]>("/api/positions", 5000, live.tick);
  const { data: orders } = useApi<any[]>("/api/orders?limit=50", 10000, live.tick);
  const [sel, setSel] = useState<any>(null);
  const [sl, setSl] = useState(""); const [tp, setTp] = useState(""); const [vol, setVol] = useState("");
  const [dlg, setDlg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const manual = can(me, "trade.manual");

  async function act(action: string, extra: any = {}) {
    setBusy(true); setErr(null);
    try { await post(`/api/positions/${sel.ticket}/action`, { action, client_request_id: rid(), ...extra }); await reload(); setDlg(null); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const pnl = (rows || []).reduce((a, r) => a + r.profit + r.swap, 0);
  return (
    <div>
      <PageTitle sub={`${rows?.length ?? 0} open positions · floating ${fmt.num(pnl)} · mirrored from MT5`} right={manual &&
        <Btn small onClick={async () => { try { await post("/api/positions/sync"); reload(); } catch (e: any) { setErr(e.message); } }}><RefreshCw size={11} />Sync MT5</Btn>}>
        Active Trades</PageTitle>
      <ErrorLine error={error || err} />
      <Panel>
        <Table rows={rows} onRow={(r) => { setSel(r); setSl(r.stop_loss ?? ""); setTp(r.take_profit ?? ""); setVol(""); }} empty="No open positions at the broker"
          rowKey={(r) => r.ticket}
          cols={[{ k: "ticket", h: "Ticket" }, { k: "symbol", h: "Symbol" }, { k: "direction", h: "Dir", r: (r) => <Badge tone={dirTone(r.direction)}>{r.direction}</Badge> },
            { k: "entry", h: "Entry", r: (r) => fmt.px(r.entry) }, { k: "current", h: "Current", r: (r) => fmt.px(r.current) }, { k: "volume", h: "Lots" },
            { k: "stop_loss", h: "SL", r: (r) => r.stop_loss ? fmt.px(r.stop_loss) : <span className="text-bear">NONE</span> }, { k: "take_profit", h: "TP", r: (r) => fmt.px(r.take_profit) },
            { k: "profit", h: "P&L", r: (r) => <span className={toneOf(r.profit) === "up" ? "text-neon" : toneOf(r.profit) === "down" ? "text-bear" : ""}>{fmt.num(r.profit)}</span> },
            { k: "risk_pct", h: "Risk %", r: (r) => fmt.pctRaw(r.risk_pct, 2) },
            { k: "strategies", h: "Strategy", sort: false, r: (r) => <span className="block max-w-[160px] truncate text-dim">{(r.strategies || []).join(", ") || "manual/external"}</span> },
            { k: "opened_at", h: "Opened", r: (r) => fmt.time(r.opened_at) }, { k: "duration_s", h: "Duration", r: (r) => fmt.dur(r.duration_s) },
            { k: "status", h: "Status", r: (r) => <Badge tone={r.status === "PROTECTED" ? "neon" : "bear"}>{r.status}</Badge> }]} />
      </Panel>
      {sel && manual && (
        <Panel title={`Manage #${sel.ticket} ${sel.symbol} ${sel.direction}`} className="mt-3">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
            <Field label="Stop loss"><input className={inputCls} value={sl} onChange={(e) => setSl(e.target.value)} /></Field>
            <Field label="Take profit"><input className={inputCls} value={tp} onChange={(e) => setTp(e.target.value)} /></Field>
            <div className="flex items-end"><Btn busy={busy} onClick={() => act("MODIFY_SLTP", { stop_loss: sl ? +sl : null, take_profit: tp ? +tp : null })}>Modify SL/TP</Btn></div>
            <div className="flex items-end"><Btn tone="neon" busy={busy} onClick={() => act("BREAK_EVEN")}>Break even</Btn></div>
            <Field label="Partial close lots"><div className="flex gap-1"><input className={inputCls} value={vol} onChange={(e) => setVol(e.target.value)} />
              <Btn tone="warn" busy={busy} disabled={!vol} onClick={() => act("PARTIAL_CLOSE", { volume: +vol })}>Close</Btn></div></Field>
          </div>
          <div className="mt-3"><Btn tone="bear" onClick={() => setDlg("close")}>Close position</Btn></div>
          <Confirm open={dlg === "close"} title={`Close #${sel.ticket}`} busy={busy} body={`Close ${sel.volume} lots of ${sel.symbol} at market.`}
            onCancel={() => setDlg(null)} onConfirm={() => act("CLOSE")} />
        </Panel>
      )}
      <Panel title="Recent orders & executions" className="mt-3">
        <Table rows={orders} empty="No orders"
          cols={[{ k: "created_at", h: "Time", r: (r) => fmt.time(r.created_at) }, { k: "execution_id", h: "Execution ID", r: (r) => <span className="text-dim">{r.execution_id.slice(0, 12)}…</span> },
            { k: "action", h: "Action" }, { k: "symbol", h: "Symbol" }, { k: "volume", h: "Lots" }, { k: "source", h: "Source" },
            { k: "status", h: "Status", r: (r) => <Badge tone={r.status === "CONFIRMED" ? "neon" : r.status === "UNKNOWN" || r.status === "SENT" ? "warn" : r.status === "PENDING" ? "cyan" : "bear"}>{r.status}</Badge> },
            { k: "status_detail", h: "Detail", r: (r) => <span className="block max-w-[260px] truncate text-dim">{r.status_detail}</span> }]} />
      </Panel>
    </div>
  );
}
