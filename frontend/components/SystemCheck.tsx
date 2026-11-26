"use client";
import { ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { can, useMe } from "./Shell";

const ICON: Record<string, string> = { ok: "✔", warning: "!", failing: "✖" };
const TONE: Record<string, string> = { ok: "text-neon", warning: "text-warn", failing: "text-bear" };
const EVERY_MS = 5 * 60 * 1000;
const TRADE_STEPS = ["DEMO mode", "DEMO account active", "Pre-flight system check", "Test symbol", "Live quote",
  "Risk Engine approval", "Order filled by the broker", "Position visible in the engine", "Position closed",
  "Close confirmed by the broker"];

function dismissed(fp: string) { try { return sessionStorage.getItem("rr-syscheck-dismissed") === fp; } catch { return false; } }
function dismiss(fp: string) { try { sessionStorage.setItem("rr-syscheck-dismissed", fp); } catch { /* private mode */ } }

/** System check agent in the console: checks every system when the console opens and every 5 minutes, and pops up
 *  when something is failing with why and what to do. The header button opens it at any time; from there a DEMO test
 *  trade proves the whole execution path. */
export function SystemCheck() {
  const me = useMe();
  const [rep, setRep] = useState<any>(null);
  const [ex, setEx] = useState<any>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [trade, setTrade] = useState<"confirm" | "running" | any | null>(null);
  const check = useCallback(async (show: boolean) => {
    setBusy(true); setErr("");
    try {
      const r = await api<any>("/api/system/check");
      setRep(r);
      api<any>("/api/system/execution").then(setEx).catch(() => setEx(null));
      if (show || (r.overall === "failing" && !dismissed(r.fingerprint))) setOpen(true);
    } catch (e: any) { setErr(e.message); if (show) setOpen(true); } finally { setBusy(false); }
  }, []);
  useEffect(() => {
    check(false);
    const t = setInterval(() => check(false), EVERY_MS);
    const h = () => check(true);
    window.addEventListener("rr-system-check", h);
    return () => { clearInterval(t); window.removeEventListener("rr-system-check", h); };
  }, [check]);
  async function runTrade() {
    setTrade("running");
    try { setTrade(await post<any>("/api/system/test-trade", { confirm: true })); }
    catch (e: any) { setTrade({ ok: false, steps: [], fix: e.message }); }
    check(false);
  }
  const close = () => { if (rep) dismiss(rep.fingerprint); setOpen(false); setTrade(null); };
  const canTrade = can(me, "trade.manual");
  if (!open) return null;
  const bad = (rep?.systems || []).filter((s: any) => s.status !== "ok");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label="System check">
      <div className="flex max-h-[90vh] w-full max-w-2xl flex-col rounded border border-cyan/60 bg-panel text-[12px] shadow-2xl">
        <div className="flex items-center justify-between border-b border-line px-4 py-2">
          <div>
            <div className={`text-[13px] font-bold ${rep ? TONE[rep.overall] : "text-cyan"}`}>
              {!rep ? "System check" : rep.overall === "ok" ? "All systems are working" : rep.overall === "warning" ? "Systems working, with warnings" : "Some systems are not working"}</div>
            {rep && <div className="text-[10px] text-dim">Checked {new Date(rep.at).toLocaleTimeString()} · mode {rep.mode} · auto trading {rep.auto_trading ? "on" : "off"} · {rep.can_trade ? "orders can be executed" : "orders cannot be executed now"}</div>}
          </div>
          <button type="button" className="text-dim underline" disabled={busy} onClick={() => check(true)}>{busy ? "Checking…" : "Check again"}</button>
        </div>
        <div className="flex-1 space-y-2 overflow-y-auto p-4">
          {err && <div className="text-bear">The engine did not answer: {err}. Desktop app → Start All.</div>}
          {rep && <div className="grid grid-cols-2 gap-1">
            {rep.systems.map((s: any) => <div key={s.key} className="flex items-center gap-2">
              <span className={`w-4 text-center font-bold ${TONE[s.status]}`}>{ICON[s.status]}</span>{s.title}</div>)}
          </div>}
          {ex && <div className={`rounded border p-2 ${ex.can_execute && !ex.last_24h.risk_rejections.length ? "border-neon/40" : "border-warn/50"}`}>
            <div className="font-bold">Trade execution</div>
            <div className={ex.can_execute ? "text-neon" : "text-bear"}>{ex.verdict}</div>
            {ex.gates.filter((g: any) => !g.ok).map((g: any) => <div key={g.gate} className="mt-1"><span className="text-bear">✖ {g.gate}</span> <span className="text-dim">— {g.detail}</span><div className="text-cyan">What to do: {g.fix}</div></div>)}
            <div className="mt-1 text-[11px] text-dim">Last 24 h: {ex.last_24h.signals} BUY/SELL signals, {ex.last_24h.executed} executed.</div>
            {ex.last_24h.not_executed_reasons.map((x: any) => <div key={x.reason} className="text-[11px]">• {x.count}× {x.reason} <span className="text-cyan">→ {x.fix}</span></div>)}
            {ex.last_24h.risk_rejections.map((x: any) => <div key={x.check} className="text-[11px]">• {x.count}× Risk Engine: {x.check} <span className="text-dim">({x.example})</span> <span className="text-cyan">→ {x.fix}</span></div>)}
            {ex.can_execute && <div className="mt-1 text-[11px] text-cyan">Next: {ex.next_step}</div>}
          </div>}
          {bad.map((s: any) => (
            <div key={s.key} className={`rounded border p-2 ${s.status === "failing" ? "border-bear/50" : "border-warn/40"}`}>
              <div className={`font-bold ${TONE[s.status]}`}>{s.title}</div>
              {s.problems.map((p: any, i: number) => (
                <div key={i} className="mt-1 border-t border-line pt-1">
                  <div><b>{p.title}</b> <span className="text-dim">— {p.evidence}</span></div>
                  {p.impact && <div className="text-dim">Impact: {p.impact}</div>}
                  <div className="text-cyan">What to do: {p.fix}</div>
                </div>))}
              <div className="mt-1 text-[10px] text-dim">Where: {s.where}</div>
            </div>))}
          {trade && <TradeProgress trade={trade} onRun={runTrade} onCancel={() => setTrade(null)} />}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-2">
          {canTrade ? <button type="button" disabled={trade === "running" || rep?.mode !== "DEMO"} title={rep?.mode !== "DEMO" ? "DEMO only" : ""}
            className="rounded border border-neon/60 px-2 py-1 text-neon disabled:opacity-40" onClick={() => setTrade("confirm")}>Execute a DEMO test trade</button>
            : <span className="text-[10px] text-dim">A trader or administrator can run a DEMO test trade.</span>}
          <button type="button" disabled={trade === "running"} className="rounded border border-line px-2 py-1 text-dim disabled:opacity-40" onClick={close}>{trade === "running" ? "Test trade running…" : "Close"}</button>
        </div>
      </div>
    </div>
  );
}

function TradeProgress({ trade, onRun, onCancel }: { trade: any; onRun: () => void; onCancel: () => void }) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (trade !== "running") return;
    setSecs(0);
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [trade]);
  if (trade === "confirm") return (
    <div className="rounded border border-neon/50 p-2">
      <div className="font-bold text-neon">Execute a DEMO test trade?</div>
      <ul className="my-1 list-disc pl-5 text-dim">
        <li>Places one real order on your connected <b>DEMO</b> account through the normal path: Risk Engine, MT5 connector, broker. It is refused in LIVE.</li>
        <li>The broker's minimum volume, with a stop loss and take profit. The position is closed again immediately; it costs the spread.</li>
        <li>Every trading control applies (kill switch, risk lock, limits). It is never counted as a trading result or toward LIVE readiness.</li>
      </ul>
      <div className="flex gap-2"><button type="button" className="rounded border border-neon/60 px-2 py-0.5 text-neon" onClick={onRun}>Execute test trade</button>
        <button type="button" className="text-dim underline" onClick={onCancel}>Cancel</button></div>
    </div>);
  const done: Record<string, any> = Object.fromEntries((trade?.steps || []).map((s: any) => [s.step, s]));
  const running = trade === "running";
  return (
    <div className={`rounded border p-2 ${running ? "border-cyan/50" : trade.ok ? "border-neon/50" : "border-bear/50"}`}>
      <div className={`font-bold ${running ? "text-cyan" : trade.ok ? "text-neon" : "text-bear"}`}>
        {running ? `Test trade in progress… ${secs} s (please wait, do not close)` : trade.ok ? `Test trade passed: all systems are in place (${trade.symbol} ${trade.volume} lots, ${trade.seconds} s)` : "Test trade failed"}</div>
      <ul className="my-1 space-y-0.5">
        {TRADE_STEPS.map((name, i) => {
          const s = done[name];
          const st = s ? (s.ok ? "ok" : "bad") : running ? (i === 0 ? "run" : "wait") : "skip";
          return <li key={name} className="flex gap-2">
            <span className={`w-4 text-center ${st === "ok" ? "text-neon" : st === "bad" ? "text-bear" : st === "run" ? "animate-pulse text-cyan" : "text-dim"}`}>{st === "ok" ? "✔" : st === "bad" ? "✖" : st === "run" ? "●" : "–"}</span>
            <span className={st === "skip" || st === "wait" ? "text-dim" : ""}>{name}{s?.detail ? <span className="text-dim"> — {s.detail}</span> : null}</span></li>;
        })}
      </ul>
      {!running && trade.fix && <div className="text-cyan">What to do: {trade.fix}</div>}
      {!running && trade.other_problems?.length > 0 && <div className="mt-1 text-[11px] text-warn">Also not working (did not stop the test): {trade.other_problems.join("; ")}</div>}
    </div>);
}

/** Header button: opens the system check (and the test trade) from any page. */
export function SystemCheckButton() {
  return <button type="button" title="System check" aria-label="System check" className="flex items-center gap-1 rounded border border-cyan/50 px-1.5 py-0.5 text-cyan hover:bg-cyan/10"
    onClick={() => window.dispatchEvent(new Event("rr-system-check"))}><ShieldCheck size={12} /><span className="hidden lg:inline">Check</span></button>;
}
