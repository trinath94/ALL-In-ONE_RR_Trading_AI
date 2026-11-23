"use client";
import { Bot, Cpu, OctagonX, Pause, Play, Power, ShieldOff, XCircle } from "lucide-react";
import { useState } from "react";
import { fmt, post } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";
import { can, useMe } from "./Shell";
import { Badge, Btn, Confirm, ErrorLine, Panel } from "./ui";

type Dialog = null | "live" | "kill" | "closeall" | "autolive" | "demo" | "noai";

export default function ControlCenter() {
  const me = useMe();
  const live = useLive(["control", "account", "positions", "order"]);
  const { data: st, reload } = useApi<any>("/api/control/state", 10000, live.tick);
  const { data: sum } = useApi<any>("/api/dashboard/summary", 15000, live.tick);
  const { data: risk } = useApi<any>(st ? `/api/risk/config/${st.mode}` : null);
  const [dlg, setDlg] = useState<Dialog>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const control = can(me, "trade.control");

  async function run(fn: () => Promise<any>) {
    setBusy(true); setErr(null);
    setNotes([]);
    try {
      const r = await fn(); await reload(); setDlg(null);
      // Phase 16: a mode switch reports positions left on the other account and that entries wait for reconciliation
      if (r && r.new_entries) setNotes([...(r.warnings || []), `New entries ${r.new_entries}.`]);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const acct = sum?.account;
  const engineOnly = st?.ai_mode === "ENGINE_ONLY";
  // Run the engine with AI (only AI-confirmed candidates are auto-executed) or without AI (the engine's own BUY/SELL
  // signals are auto-executed). The Risk Engine decides in both. LIVE engine-only needs a typed confirmation.
  function setAiMode(mode: "AI" | "ENGINE_ONLY") {
    if (mode === "ENGINE_ONLY" && st?.mode === "LIVE") { setDlg("noai"); return; }
    run(() => post("/api/control/ai-mode", { mode }));
  }
  return (
    <Panel title="Live Trading Control Center" icon={<Power size={12} />} accent={st?.mode === "LIVE" ? "bear" : "cyan"}>
      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <div><div className="text-dim">AUTO TRADING</div>
          <div className={st?.auto_trading ? "font-bold text-neon" : "font-bold text-dim"}>{st?.auto_trading ? "ON" : "OFF"}</div></div>
        <div className="col-span-2 flex items-center justify-between gap-2 rounded border border-line px-2 py-1.5">
          <div><div className="text-dim">ENGINE</div>
            <div className={engineOnly ? "font-bold text-warn" : "font-bold text-cyan"}>
              {st ? (engineOnly ? "Running without AI" : "Running with AI") : "…"}</div>
            <div className="text-[10px] text-dim">{!st ? "" : engineOnly
              ? "Engine BUY/SELL signals are auto-executed; the Risk Engine decides. No AI cost."
              : st.ai_configured ? "Only AI-confirmed candidates are auto-executed; the Risk Engine decides."
              : "No AI provider configured: nothing is auto-executed. Add one in AI Studio, or run without AI."}</div></div>
          <div className="flex shrink-0 overflow-hidden rounded border border-line" role="group" aria-label="Run engine with or without AI">
            <button type="button" disabled={!control || busy || !st} aria-pressed={!engineOnly}
              className={`flex items-center gap-1 px-2 py-1 ${!engineOnly ? "bg-cyan/20 font-bold text-cyan" : "text-dim hover:text-white"}`}
              onClick={() => engineOnly && setAiMode("AI")}><Bot size={12} />With AI</button>
            <button type="button" disabled={!control || busy || !st} aria-pressed={engineOnly}
              className={`flex items-center gap-1 border-l border-line px-2 py-1 ${engineOnly ? "bg-warn/20 font-bold text-warn" : "text-dim hover:text-white"}`}
              onClick={() => !engineOnly && setAiMode("ENGINE_ONLY")}><Cpu size={12} />Without AI</button>
          </div>
        </div>
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
      <div className="mt-2"><ErrorLine error={err} />
      {notes.map((n, i) => <div key={i} className="mt-1 rounded border border-warn/40 bg-warn/5 px-2 py-1 text-[11px] text-warn">{n}</div>)}</div>
      <Confirm open={dlg === "live"} title="Activate LIVE trading" phrase="ENABLE LIVE TRADING" busy={busy}
        body={<>Real money will be at risk. Auto trading will be OFF after the switch and startup reconciliation must pass before any entry.</>}
        onCancel={() => setDlg(null)} onConfirm={(t) => run(() => post("/api/control/mode", { mode: "LIVE", confirm_text: t }))} />
      <Confirm open={dlg === "demo"} title="Switch to DEMO" tone="warn" busy={busy} body="Live auto trading stops. Open live positions remain at the broker and keep their protective stops."
        onCancel={() => setDlg(null)} onConfirm={() => run(() => post("/api/control/mode", { mode: "DEMO" }))} />
      <Confirm open={dlg === "autolive"} title="Enable LIVE auto trading" busy={busy} phrase="AUTO LIVE"
        body={engineOnly ? "The engine will place real orders for its own risk-approved signals, without AI review."
          : "The engine will place real orders for AI-confirmed, risk-approved signals."} onCancel={() => setDlg(null)}
        onConfirm={() => run(() => post("/api/control/auto-trading", { enabled: true, confirm: true }))} />
      <Confirm open={dlg === "noai"} title="Run LIVE without AI" phrase="RUN WITHOUT AI" busy={busy}
        body="Real orders will be placed for the engine's own BUY/SELL signals without AI review. Every risk limit, the kill switch and the Risk Engine still apply. Only signals produced after the switch are affected."
        onCancel={() => setDlg(null)} onConfirm={(t) => run(() => post("/api/control/ai-mode", { mode: "ENGINE_ONLY", confirm_text: t }))} />
      <Confirm open={dlg === "closeall"} title="Close all positions" phrase="CLOSE ALL" busy={busy}
        body="Every open position on the active account will be closed at market." onCancel={() => setDlg(null)}
        onConfirm={(t) => run(() => post("/api/control/close-all", { confirm_text: t }))} />
      <Confirm open={dlg === "kill"} title="Emergency kill switch" phrase="KILL" busy={busy}
        body="Stops all new trading immediately, blocks pending executions, and applies the configured existing-position policy (PROTECT / REDUCE / CLOSE_ALL)."
        onCancel={() => setDlg(null)} onConfirm={() => run(() => post("/api/control/kill-switch", { reason: `Manual kill switch by ${me?.username}` }))} />
    </Panel>
  );
}
