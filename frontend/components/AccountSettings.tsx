"use client";
import { useEffect, useState } from "react";
import { put } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { Badge, Btn, ErrorLine, Field, inputCls, Panel } from "./ui";

// The limits most often set per account. Blank = the account's mode template (DEMO or LIVE, Risk Centre).
const LIMITS: [string, string][] = [
  ["risk_per_trade_pct", "Risk per trade %"], ["max_daily_loss_pct", "Daily loss limit %"],
  ["max_weekly_loss_pct", "Weekly loss limit %"], ["max_drawdown_pct", "Max drawdown %"],
  ["max_open_positions", "Max open positions"], ["max_total_exposure_pct", "Max exposure %"],
  ["min_free_margin_pct", "Min free margin %"], ["max_lot_size", "Max lot size"],
];

/** Multi-account foundation: one account's name, trading switch, eligible symbols and strategies, and its own risk
 *  limits over its mode's template. Every save is validated and audited by the engine. */
export default function AccountSettings({ account, symbols, canManage, canRisk, onSaved }:
  { account: any; symbols: any[] | null | undefined; canManage: boolean; canRisk: boolean; onSaved: () => void }) {
  const { data: risk, reload } = useApi<any>(`/api/accounts/${account.id}/risk`);
  const [label, setLabel] = useState<string>(account.label || "");
  const [syms, setSyms] = useState<string[] | null>(account.allowed_symbols);
  const [strats, setStrats] = useState<string>((account.allowed_strategies || []).join(", "));
  const [lim, setLim] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    setLabel(account.label || ""); setSyms(account.allowed_symbols); setStrats((account.allowed_strategies || []).join(", "));
  }, [account]);
  useEffect(() => {
    if (risk) setLim(Object.fromEntries(Object.entries(risk.overrides).map(([k, v]) => [k, String(v)])));
  }, [risk]);

  async function save(fn: () => Promise<any>, ok: string) {
    setBusy(true); setErr(null); setMsg(null);
    try { await fn(); setMsg(ok); onSaved(); reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const saveSettings = () => {
    const codes = strats.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
    return save(() => put(`/api/accounts/${account.id}/settings`, {
      label, ...(syms === null ? { clear_allowed_symbols: true } : { allowed_symbols: syms }),
      ...(codes.length ? { allowed_strategies: codes } : { clear_allowed_strategies: true }),
    }), "Account settings saved");
  };
  const saveLimits = () => {
    const body: Record<string, number> = {};
    for (const [k] of LIMITS) if ((lim[k] ?? "").trim() !== "") body[k] = Number(lim[k]);
    for (const [k, v] of Object.entries(risk?.overrides || {})) if (!LIMITS.some(([x]) => x === k)) body[k] = v as number;
    return save(() => put(`/api/accounts/${account.id}/risk`, body), "Account risk limits saved");
  };
  const toggleSym = (s: string) => {
    const cur = syms ?? [];
    const next = cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s].sort();
    setSyms(next.length ? next : null);
  };
  return (
    <Panel title={`Account ${account.label || account.login} · ${account.mode}`}>
      <ErrorLine error={err} />{msg && <div className="mb-2 text-[11px] text-neon">{msg}</div>}
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="text-dim">Automatic trading on this account:</span>
        {account.trading_enabled ? <Badge tone="neon">execution account</Badge> : <Badge tone="dim">research only</Badge>}
        {canManage && <Btn small tone={account.trading_enabled ? "warn" : "neon"} busy={busy}
          onClick={() => save(() => put(`/api/accounts/${account.id}/settings`, { trading_enabled: !account.trading_enabled }),
            account.trading_enabled ? "Trading disabled for this account" : "Trading enabled for this account")}>
          {account.trading_enabled ? "Make research-only" : "Enable trading"}</Btn>}
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        <Field label="Name"><input className={inputCls} value={label} disabled={!canManage} maxLength={64}
          placeholder="e.g. DEMO Account 1" onChange={(e) => setLabel(e.target.value)} /></Field>
        <Field label="Strategies (codes, comma separated; blank = every enabled strategy)">
          <input className={inputCls} value={strats} disabled={!canManage} onChange={(e) => setStrats(e.target.value)} /></Field>
      </div>
      <div className="mt-2 text-[11px] text-dim">Symbols {syms === null ? "(all scanned symbols)" : `(${syms.length} selected)`}</div>
      <div className="mt-1 flex flex-wrap gap-1">
        {(symbols || []).filter((s: any) => s.tradable).map((s: any) => {
          const on = syms !== null && syms.includes(s.symbol);
          return <button key={s.id} type="button" disabled={!canManage} onClick={() => toggleSym(s.symbol)}
            className={`rounded border px-1.5 py-0.5 text-[10px] ${on ? "border-cyan bg-cyan/20 text-cyan" : "border-line text-dim"}`}>{s.symbol}</button>;
        })}
        {syms !== null && canManage && <button type="button" className="px-1.5 text-[10px] text-dim underline" onClick={() => setSyms(null)}>all</button>}
      </div>
      {canManage && <div className="mt-2"><Btn small busy={busy} onClick={saveSettings}>Save account settings</Btn></div>}
      <div className="mt-4 text-[11px] text-dim">Risk limits for this account. Blank uses the {account.mode} template from the Risk Centre.</div>
      <div className="mt-1 grid grid-cols-2 gap-2 md:grid-cols-4">
        {LIMITS.map(([k, h]) => (
          <Field key={k} label={h}><input className={inputCls} inputMode="decimal" disabled={!canRisk} value={lim[k] ?? ""}
            placeholder={risk ? String(risk.template[k]) : ""} onChange={(e) => setLim({ ...lim, [k]: e.target.value })} /></Field>
        ))}
      </div>
      {canRisk && <div className="mt-2"><Btn small busy={busy} onClick={saveLimits}>Save risk limits</Btn></div>}
    </Panel>
  );
}
