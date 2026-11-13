"use client";
import { useEffect, useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Field, inputCls, Panel, Table } from "@/components/ui";
import { fmt, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const GROUPS: [string, string[]][] = [
  ["Capital & sizing", ["initial_capital", "risk_per_trade_pct", "max_lot_size", "max_size_multiplier", "min_rr"]],
  ["Loss limits (lock trading)", ["max_daily_loss_pct", "max_weekly_loss_pct", "max_monthly_loss_pct", "max_drawdown_pct", "max_consecutive_losses", "daily_profit_target_pct", "stop_after_profit_target"]],
  ["Exposure", ["max_open_positions", "max_trades_per_symbol", "max_total_exposure_pct", "max_correlated_exposure_pct", "correlation_threshold", "max_leverage_exposure", "min_free_margin_pct"]],
  ["Execution quality", ["max_spread_points", "max_slippage_points", "require_take_profit", "news_unknown_blocks"]],
  ["Trade management", ["break_even_at_r", "partial_close_at_r", "partial_close_fraction", "trailing_start_r", "trailing_atr_mult"]],
  ["Emergency", ["existing_position_policy", "reduce_fraction"]],
];

export default function Risk() {
  const me = useMe();
  const [mode, setMode] = useState("DEMO");
  const { data, reload } = useApi<any>(`/api/risk/config/${mode}`);
  const { data: evs } = useApi<any[]>("/api/risk/evaluations?limit=100", 15000);
  const [form, setForm] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null); const [ok, setOk] = useState(false); const [busy, setBusy] = useState(false);
  useEffect(() => { if (data) setForm({ ...data }); }, [data]);
  const edit = can(me, "risk.manage");
  async function save() {
    setBusy(true); setErr(null); setOk(false);
    try { await put(`/api/risk/config/${mode}`, form); setOk(true); reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  return (
    <div>
      <PageTitle sub="Every order — automated, manual or AI-recommended — passes these checks. DEMO and LIVE are configured independently."
        right={<div className="flex gap-1">{["DEMO", "LIVE"].map((m) => <Btn key={m} small tone={m === mode ? (m === "LIVE" ? "bear" : "cyan") : "dim"} onClick={() => setMode(m)}>{m}</Btn>)}</div>}>
        Risk Management</PageTitle>
      <Panel accent={mode === "LIVE" ? "bear" : "cyan"}>
        {form && <div className="grid gap-4 lg:grid-cols-3">
          {GROUPS.map(([g, keys]) => (
            <div key={g}><div className="mb-2 text-[10px] uppercase tracking-widest text-cyan">{g}</div>
              <div className="space-y-2">{keys.map((k) => (
                <Field key={k} label={k.replace(/_/g, " ")}>
                  {typeof form[k] === "boolean" ? <input type="checkbox" disabled={!edit} checked={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.checked })} />
                    : k === "existing_position_policy" ? <select disabled={!edit} className={inputCls} value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })}>
                      {["PROTECT", "REDUCE", "CLOSE_ALL"].map((p) => <option key={p}>{p}</option>)}</select>
                    : <input disabled={!edit} className={inputCls} value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value === "" ? "" : Number(e.target.value) })} />}
                </Field>))}</div>
            </div>))}
        </div>}
        <div className="mt-3 flex items-center gap-3">{edit && <Btn tone={mode === "LIVE" ? "bear" : "neon"} busy={busy} onClick={save}>Save {mode} risk config</Btn>}
          {ok && <span className="text-[11px] text-neon">Saved and audited</span>}<ErrorLine error={err} /></div>
      </Panel>
      <Panel title="Recent Risk Engine decisions" className="mt-3">
        <Table rows={evs} empty="No evaluations yet"
          cols={[{ k: "at", h: "Time", r: (e) => fmt.time(e.at) }, { k: "signal_id", h: "Signal" },
            { k: "approved", h: "Decision", r: (e) => <Badge tone={e.approved ? "neon" : "bear"}>{e.approved ? "approved" : "rejected"}</Badge> },
            { k: "volume", h: "Lots" }, { k: "risk_percent", h: "Risk %", r: (e) => fmt.num(e.risk_percent, 3) },
            { k: "reasons", h: "Reasons", sort: false, r: (e) => <span className="block max-w-[520px] truncate text-bear">{e.reasons.join("; ")}</span> }]} />
      </Panel>
    </div>
  );
}
