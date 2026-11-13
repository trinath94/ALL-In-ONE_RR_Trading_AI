"use client";
import { OctagonX, Pause, Play, Power, ShieldOff, XCircle } from "lucide-react";
import { useState } from "react";
import { fmt, post } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";
import { can, useMe } from "./Shell";
import { Badge, Btn, Confirm, ErrorLine, Panel } from "./ui";

type Dialog = null | "live" | "kill" | "closeall" | "autolive" | "demo";

export default function ControlCenter() {
  const me = useMe();
  const live = useLive(["control", "account", "positions", "order"]);
  const { data: st, reload } = useApi<any>("/api/control/state", 10000, live.tick);
  const { data: sum } = useApi<any>("/api/dashboard/summary", 15000, live.tick);
  const { data: risk } = useApi<any>(st ? `/api/risk/config/${st.mode}` : null);
  const [dlg, setDlg] = useState<Dialog>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const control = can(me, "trade.control");

  async function run(fn: () => Promise<any>) {
    setBusy(true); setErr(null);
    try { await fn(); await reload(); setDlg(null); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const acct = sum?.account;
  return (
    <Panel title="Live Trading Control Center" icon={<Power size={12} />} accent={st?.mode === "LIVE" ? "bear" : "cyan"}>
      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <div><div className="text-dim">AUTO TRADING</div>
          <div className={st?.auto_trading ? "font-bold text-neon" : "font-bold text-dim"}>{st?.auto_trading ? "ON" : "OFF"}</div></div>
        <div><div className="text-dim">ACCOUNT MODE</div>
          <div className={st?.mode === "LIVE" ? "font-bold text-bear" : "font-bold text-cyan"}>{st?.mode ?? "…"}</div></div>
        <div><div className="text-dim">Trading account</div><div>{st?.account ? `${st.account.login} ${st.account.verified ? "✓" : "(unverified)"}` : "none"}</div></div>
        <div><div className="text-dim">Broker server</div><div className="truncate">{st?.account?.server ?? "—"}</div></div>
        <div><div className="text-dim">Balance / Equity</div><div className="tabular">{fmt.num(acct?.balance)} / {fmt.num(acct?.equity)}</div></div>
        <div><div className="text-dim">Risk per trade · Max daily loss</div><div>{risk ? `${risk.risk_per_trade_pct}% · ${risk.max_daily_loss_pct}%` : "—"}</div></div>
        <div><div className="text-dim">Open positions</div><div>{sum?.performance?.active_positions ?? "—"}</div></div>
        <div><div className="text-dim">Execution status</div>
          <div>{st?.kill_switch ? <Badge tone="bear">kill switch</Badge> : st?.risk_lock ? <Badge tone="bear">risk lock</Badge>
            : !st?.startup_checks_passed ? <Badge tone="warn">reconciling</Badge> : st?.pause_new_entries ? <Badge tone="warn">paused</Badge> : <Badge tone="neon">ready</Badge>}</div></div>
      </div>
      {(st?.kill_reason || st?.risk_lock_reason) && <div className="mt-2 text-[11px] text-bear">{st.kill_reason || st.risk_lock_reason}</div>}
      <div className="mt-3 flex flex-wrap gap-2">
        {st?.auto_trading
          ? <Btn tone="warn" disabled={!control} busy={busy} onClick={() => run(() => post("/api/control/auto-trading", { enabled: false }))}><Pause size={12} />Stop auto</Btn>
          : <Btn tone="neon" disabled={!control || st?.kill_switch} busy={busy}
              onClick={() => st?.mode === "LIVE" ? setDlg("autolive") : run(() => post("/api/control/auto-trading", { enabled: true }))}><Play size={12} />Start auto</Btn>}
        <Btn tone="warn" disabled={!control} onClick={() => run(() => post("/api/control/pause", { enabled: !st?.pause_new_entries }))}>
          {st?.pause_new_entries ? "Resume entries" : "Pause new entries"}</Btn>
        {st?.mode === "LIVE"
          ? <Btn tone="cyan" disabled={!control} onClick={() => setDlg("demo")}>Switch to DEMO</Btn>
          : <Btn tone="bear" disabled={!can(me, "trade.live")} onClick={() => setDlg("live")}>Switch to LIVE</Btn>}
        <Btn tone="bear" disabled={!control} onClick={() => setDlg("closeall")}><XCircle size={12} />Close all</Btn>
        <Btn tone="bear" disabled={!control} onClick={() => setDlg("kill")}><OctagonX size={12} />Emergency kill switch</Btn>
        {st?.kill_switch && can(me, "system.manage") && <Btn tone="dim" onClick={() => run(() => post("/api/control/kill-switch/reset"))}><ShieldOff size={12} />Reset kill switch</Btn>}
      </div>
      <div className="mt-2"><ErrorLine error={err} /></div>
      <Confirm open={dlg === "live"} title="Activate LIVE trading" phrase="ENABLE LIVE TRADING" busy={busy}
        body={<>Real money will be at risk. Auto trading will be OFF after the switch and startup reconciliation must pass before any entry.</>}
        onCancel={() => setDlg(null)} onConfirm={(t) => run(() => post("/api/control/mode", { mode: "LIVE", confirm_text: t }))} />
      <Confirm open={dlg === "demo"} title="Switch to DEMO" tone="warn" busy={busy} body="Live auto trading stops. Open live positions remain at the broker and keep their protective stops."
        onCancel={() => setDlg(null)} onConfirm={() => run(() => post("/api/control/mode", { mode: "DEMO" }))} />
      <Confirm open={dlg === "autolive"} title="Enable LIVE auto trading" busy={busy} phrase="AUTO LIVE"
        body="The engine will place real orders for Claude-confirmed, risk-approved signals." onCancel={() => setDlg(null)}
        onConfirm={() => run(() => post("/api/control/auto-trading", { enabled: true, confirm: true }))} />
      <Confirm open={dlg === "closeall"} title="Close all positions" phrase="CLOSE ALL" busy={busy}
        body="Every open position on the active account will be closed at market." onCancel={() => setDlg(null)}
        onConfirm={(t) => run(() => post("/api/control/close-all", { confirm_text: t }))} />
      <Confirm open={dlg === "kill"} title="Emergency kill switch" phrase="KILL" busy={busy}
        body="Stops all new trading immediately, blocks pending executions, and applies the configured existing-position policy (PROTECT / REDUCE / CLOSE_ALL)."
        onCancel={() => setDlg(null)} onConfirm={() => run(() => post("/api/control/kill-switch", { reason: `Manual kill switch by ${me?.username}` }))} />
    </Panel>
  );
}
