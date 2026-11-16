"use client";
import { PageTitle } from "@/components/Shell";
import { Badge, dirTone, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/** Part 17 item 9: the trading worker's state, the gates it applies, and why each open candidate would or would not
 *  be executed. Read-only: the worker is started and stopped only from the desktop application, with a typed phrase. */
export default function TradingWorker() {
  const { data: w, error } = useApi<any>("/api/worker/status", 10000);
  return (
    <div>
      <PageTitle sub={w?.control ?? "The trading worker executes only through every gate: auto trading, AI confirmation of the same direction, the Risk Engine and idempotency."}>
        Trading Worker</PageTitle>
      <ErrorLine error={error} />
      {w && <>
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">
          <Stat label="Worker" value={w.running == null ? "unknown" : w.running ? "RUNNING" : "STOPPED"} tone={w.running ? "up" : "plain"} />
          <Stat label="Auto trading" value={w.auto_trading ? "ON" : "OFF"} sub={w.auto_trading_since ? `since ${fmt.time(w.auto_trading_since)}` : undefined} tone={w.auto_trading ? "up" : "plain"} />
          <Stat label="Mode" value={w.mode} tone={w.mode === "LIVE" ? "down" : "plain"} />
          <Stat label="Reconciliation" value={w.startup_checks_passed ? "passed" : "pending"} tone={w.startup_checks_passed ? "plain" : "warn"} />
          <Stat label="Kill switch" value={w.kill_switch ? "SET" : "clear"} tone={w.kill_switch ? "down" : "plain"} />
          <Stat label="Risk lock" value={w.risk_lock ? "LOCKED" : "clear"} tone={w.risk_lock ? "down" : "plain"} />
          <Stat label="New entries" value={w.pause_new_entries ? "PAUSED" : "allowed"} tone={w.pause_new_entries ? "warn" : "plain"} />
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          <Panel title="Open candidates and what the worker would do">
            <Table rows={w.candidates} rowKey={(c) => c.id} empty="No NEW, unexpired signals."
              cols={[{ k: "created_at", h: "Created", r: (c) => fmt.time(c.created_at) }, { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
                { k: "decision", h: "Signal", r: (c) => <Badge tone={dirTone(c.decision)}>{c.decision}</Badge> },
                { k: "review", h: "AI review", r: (c) => c.review ?? "—" },
                { k: "status", h: "Worker", cls: "whitespace-normal", r: (c) => <span className={c.status.startsWith("eligible") ? "text-neon" : "text-dim"}>{c.status}</span> },
                { k: "expires_at", h: "Expires", r: (c) => fmt.time(c.expires_at) }]} />
          </Panel>
          <Panel title="Recent execution attempts">
            <Table rows={w.runs} rowKey={(r, i) => `${r.signal_id}${i}`} empty="The trading worker has not attempted an execution yet."
              cols={[{ k: "at", h: "Time", r: (r) => fmt.time(r.at) }, { k: "signal_id", h: "Signal" },
                { k: "status", h: "Result", r: (r) => <Badge tone={r.status === "ok" ? "neon" : r.status === "error" ? "bear" : "dim"}>{r.status}</Badge> },
                { k: "output", h: "Order / signal", sort: false, r: (r) => <span className="text-dim">{r.output?.order_status ?? "no order"} · {r.output?.signal_status ?? ""}</span> },
                { k: "errors", h: "Errors", sort: false, r: (r) => r.errors?.length ? <span className="text-bear">{r.errors.join("; ")}</span> : "—" }]} />
          </Panel>
        </div>
      </>}
    </div>
  );
}
