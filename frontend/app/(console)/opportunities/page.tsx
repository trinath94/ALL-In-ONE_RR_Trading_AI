"use client";
import { useState } from "react";
import { PageTitle } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { api, fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const STATE: Record<string, any> = {
  RESEARCHING: "dim", MONITORING: "cyan", WAITING_FOR_CONFIRMATION: "warn", QUALIFIED: "violet", EXECUTED: "neon",
  REJECTED: "bear", NO_TRADE: "dim", EXPIRED: "dim",
};
const STATES = Object.keys(STATE);
const RESULT_TONE = (v: any): any => (v === "FAIL" || v === "REJECTED" || v === "HIGH" ? "bear"
  : v === "WAIT" || v === "MEDIUM" || v === "unavailable" ? "warn" : v === "PASS" || v === "LOW" || v === "APPROVED" || v === "EXECUTED" ? "neon" : "dim");

function short(v: any) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Parts 27-30 (Part 27): every researched setup, its lifecycle and its decision trace, stage by stage. Read-only:
 *  nothing here can change a decision, a risk limit or the trading mode. */
export default function Opportunities() {
  const [state, setState] = useState("");
  const { data: summary } = useApi<any>("/api/opportunities/summary", 20000);
  const { data, error } = useApi<any[]>(`/api/opportunities?limit=200${state ? `&state=${state}` : ""}`, 20000);
  const { data: src } = useApi<any>("/api/data-sources", 30000);
  const [sel, setSel] = useState<any>(null);
  async function open(o: any) { setSel({ ...o, loading: true }); setSel(await api(`/api/opportunities/${o.id}`)); }
  const st = summary?.states || {};
  return (
    <div>
      <PageTitle sub="Every analysis in which a strategy produced a setup becomes an opportunity. Its decision trace shows what each agent saw and decided: data, regime, strategies, news and calendar, signal engine, consensus, probability, fusion, entry validation, exit plan, failure risk, AI review, then the Risk Engine at execution. The Risk Engine has the final say.">
        Opportunity Lifecycle</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        {STATES.map((s) => <Stat key={s} label={s.replaceAll("_", " ").toLowerCase()} value={st[s] ?? "—"} sub="last 24 h" />)}
      </div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="text-dim">Gate mode:</span>
        <Badge tone={summary?.gate_mode === "enforce" ? "warn" : "dim"}>{summary?.gate_mode ?? "—"}</Badge>
        <span className="text-dim">{summary?.gate_mode === "enforce"
          ? "entry-validation price/spread failures and HIGH failure risk downgrade BUY/SELL to WAIT"
          : "entry-validation price/spread checks and failure risk are recorded only; wrong-side stops or targets always make the setup NO_TRADE"}</span>
        <span className="ml-auto text-dim">Triggers (24 h): {Object.entries(summary?.triggers || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</span>
      </div>
      <div className="grid gap-3 xl:grid-cols-[1fr_520px]">
        <Panel title="Opportunities">
          <div className="mb-2 flex flex-wrap gap-1">
            {["", ...STATES].map((s) => (
              <button key={s || "all"} onClick={() => setState(s)}
                className={`rounded border px-2 py-0.5 text-[10px] ${state === s ? "border-cyan text-cyan" : "border-line text-dim"}`}>
                {s ? s.replaceAll("_", " ").toLowerCase() : "all"}</button>))}
          </div>
          <Table rows={data} onRow={open} rowKey={(o) => o.id}
            empty="No opportunities yet. They appear when the analysis service analyses a closed bar on which at least one strategy produced a setup."
            cols={[{ k: "created_at", h: "When", r: (o) => fmt.time(o.created_at) },
              { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
              { k: "direction", h: "Dir", r: (o) => o.direction ?? <span className="text-dim">—</span> },
              { k: "state", h: "State", r: (o) => <Badge tone={STATE[o.state]}>{o.state.replaceAll("_", " ")}</Badge> },
              { k: "score", h: "Engine score", sort: false, r: (o) => o.score_breakdown?.engine?.score ?? "—" },
              { k: "entry", h: "Entry check", sort: false, r: (o) => <Badge tone={RESULT_TONE(o.score_breakdown?.entry_validation)}>{o.score_breakdown?.entry_validation ?? "—"}</Badge> },
              { k: "risk", h: "Failure risk", sort: false, r: (o) => <Badge tone={RESULT_TONE(o.score_breakdown?.failure_risk)}>{o.score_breakdown?.failure_risk ?? "—"}</Badge> },
              { k: "ai", h: "AI review", sort: false, r: (o) => o.score_breakdown?.ai_review ?? <span className="text-dim">—</span> },
              { k: "trigger", h: "Trigger", r: (o) => <span className="text-[10px] text-dim">{o.trigger}</span> },
              { k: "state_reason", h: "Reason", sort: false, r: (o) => <span className="block max-w-[260px] truncate text-dim">{o.state_reason}</span> }]} />
        </Panel>
        {sel && (
          <Panel title={sel.loading ? "Loading…" : `#${sel.id} ${sel.symbol} ${sel.timeframe} ${sel.direction ?? ""}`}>
            {!sel.loading && <>
              <div className="mb-2 text-[11px]"><Badge tone={STATE[sel.state]}>{sel.state.replaceAll("_", " ")}</Badge> <span className="text-dim">{sel.state_reason}</span></div>
              <div className="text-[10px] uppercase text-dim">Lifecycle</div>
              {sel.transitions.map((t: any, i: number) => (
                <div key={i} className="text-[11px]">{fmt.time(t.at)} · {t.from ?? "∅"} → <b>{t.to}</b> <span className="text-dim">{t.reason}</span></div>))}
              <div className="mt-3 text-[10px] uppercase text-dim">Decision trace</div>
              {sel.trace.map((t: any, i: number) => (
                <details key={i} className="border-b border-line py-1 text-[11px]">
                  <summary className="cursor-pointer">
                    <span className="inline-block w-36 text-dim">{t.stage.replaceAll("_", " ")}</span>
                    <Badge tone={RESULT_TONE(t.result)}>{short(t.result)}</Badge>
                    <span className="ml-2 text-[10px] text-dim">{t.agent}</span>
                  </summary>
                  <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] text-dim">{JSON.stringify(t.evidence, null, 2)}</pre>
                </details>))}
              {sel.signal_id && <div className="mt-2 text-[11px] text-dim">Signal #{sel.signal_id} · analysis #{sel.analysis_id}</div>}
            </>}
          </Panel>)}
      </div>
      <Panel title="Data sources (observed health)" className="mt-3">
        <Table rows={[...(src?.sources || []), ...(src?.ai_providers || [])]} rowKey={(s) => s.key}
          cols={[{ k: "name", h: "Source" }, { k: "category", h: "Category" }, { k: "provider", h: "Provider" },
            { k: "status", h: "Status", r: (s) => <Badge tone={s.status === "ok" ? "neon" : s.status === "error" ? "bear" : s.status === "degraded" ? "warn" : "dim"}>{s.status.replaceAll("_", " ")}</Badge> },
            { k: "last_ok_at", h: "Last OK", r: (s) => s.last_ok_at ? fmt.time(s.last_ok_at) : "—" },
            { k: "counts", h: "OK / errors", sort: false, r: (s) => s.ok_24h !== undefined ? `${s.ok_24h} / ${s.errors_24h} (24 h)` : `${s.ok_count} / ${s.error_count}` },
            { k: "last_error", h: "Last error", sort: false, r: (s) => <span className="block max-w-[280px] truncate text-bear">{s.last_error || ""}</span> },
            { k: "notes", h: "Notes", sort: false, r: (s) => <span className="text-dim">{s.notes}</span> }]} />
      </Panel>
    </div>
  );
}
