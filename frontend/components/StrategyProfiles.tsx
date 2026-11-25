"use client";
import { useState } from "react";
import { post, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { Badge, Btn, Confirm, ErrorLine, Field, inputCls } from "./ui";

const TONE: Record<string, any> = { PROPOSED: "dim", VALIDATING: "warn", VALIDATED: "cyan", REJECTED: "bear",
  DEMO_ACTIVE: "neon", LIVE_ACTIVE: "violet", RETIRED: "dim" };

/** Adaptive configuration: versioned parameter profiles for one strategy (per regime, symbols, timeframes). The
 *  strategy itself never changes; a profile is used only after validation and activation, and can be rolled back. */
export default function StrategyProfiles({ code, canManage, canPolicy, canLive }:
  { code: string; canManage: boolean; canPolicy: boolean; canLive: boolean }) {
  const { data, reload } = useApi<any>(`/api/strategies/${code}/profiles`, 30000);
  const { data: pol, reload: reloadPol } = useApi<any>("/api/automation/policy");
  const [params, setParams] = useState("{}");
  const [regime, setRegime] = useState("");
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [live, setLive] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(fn: () => Promise<any>, ok: string) {
    setBusy(true); setErr(null); setMsg(null);
    try { await fn(); setMsg(ok); reload(); reloadPol(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const act = (id: number, action: string, confirm_text = "") => run(() => post(`/api/profiles/${id}/action`, { action, confirm_text }), `${action.replace("_", " ")}: done`);
  return (
    <div className="mt-3">
      <div className="text-[10px] uppercase text-dim">Configuration profiles (versioned; the strategy itself never changes)</div>
      <ErrorLine error={err} />{msg && <div className="text-[11px] text-neon">{msg}</div>}
      {pol && <div className="mt-1 flex flex-wrap gap-3 text-[11px]">
        <label className="flex items-center gap-1"><input type="checkbox" disabled={!canPolicy || busy} checked={pol.auto_promote_profiles_demo}
          onChange={(e) => run(() => put("/api/automation/policy", { auto_promote_profiles_demo: e.target.checked }), "Policy saved")} />
          Activate validated profiles on DEMO automatically</label>
        <label className="flex items-center gap-1"><input type="checkbox" disabled={!canPolicy || busy} checked={pol.auto_promote_experiments_demo}
          onChange={(e) => run(() => put("/api/automation/policy", { auto_promote_experiments_demo: e.target.checked }), "Policy saved")} />
          Start the DEMO shadow test of validated experiments automatically</label>
        <span className="text-dim">LIVE is never automatic.</span></div>}
      {(data?.profiles || []).length === 0 ? <div className="text-[11px] text-dim">No profiles: the strategy's configuration applies.</div> :
        (data?.profiles || []).map((p: any) => (
          <div key={p.id} className="mt-1 rounded border border-line p-1.5 text-[11px]">
            <div className="flex flex-wrap items-center gap-2">
              <b>v{p.version}</b><Badge tone={TONE[p.status]}>{p.status.replace("_", " ")}</Badge>
              <span className="text-dim">{p.regime || "any regime"}{p.symbols ? ` · ${p.symbols.join(", ")}` : ""}{p.timeframes ? ` · ${p.timeframes.join(", ")}` : ""}</span>
              <span className="font-mono text-[10px]">{JSON.stringify(p.params)}</span>
              <span className="text-dim">{p.source}{p.ai_provider ? ` (${p.ai_provider})` : ""}</span>
              {p.forward_demo && <span className="text-dim">DEMO: {p.forward_demo.trades} trades, net {p.forward_demo.net}</span>}
              <button type="button" className="text-dim underline" onClick={() => setOpen(open === p.id ? null : p.id)}>{open === p.id ? "hide" : "evidence"}</button>
              {canManage && <span className="ml-auto flex gap-1">
                {["PROPOSED", "REJECTED", "VALIDATED"].includes(p.status) && <Btn small tone="dim" busy={busy} onClick={() => act(p.id, "validate")}>Validate</Btn>}
                {p.status === "VALIDATED" && <Btn small tone="neon" busy={busy} onClick={() => act(p.id, "activate_demo")}>Activate on DEMO</Btn>}
                {p.status === "DEMO_ACTIVE" && canLive && <Btn small tone="bear" busy={busy} onClick={() => setLive(p)}>Activate on LIVE…</Btn>}
                {["DEMO_ACTIVE", "LIVE_ACTIVE"].includes(p.status) && <Btn small tone="warn" busy={busy} onClick={() => act(p.id, "rollback")}>Roll back</Btn>}
                {!["RETIRED", "DEMO_ACTIVE", "LIVE_ACTIVE"].includes(p.status) && <Btn small tone="dim" busy={busy} onClick={() => act(p.id, "retire")}>Retire</Btn>}
              </span>}
            </div>
            {p.reason && <div className="text-dim">{p.reason}</div>}
            {open === p.id && <div className="mt-1">
              {p.validation?.gates ? Object.entries(p.validation.gates).map(([k, g]: any) => (
                <div key={k}><Badge tone={g.passed ? "neon" : "bear"}>{g.passed ? "pass" : "fail"}</Badge> <span>{k.replaceAll("_", " ")}</span> <span className="text-dim">{g.detail}</span></div>))
                : <div className="text-dim">Not validated yet (the learner validates queued profiles every hour).</div>}
              <div className="mt-1 text-[10px] text-dim">{(p.history || []).map((h: any) => `${h.to}${h.note ? ` (${h.note})` : ""}`).join(" → ")}</div>
            </div>}
          </div>))}
      {canManage && <div className="mt-2 grid gap-2 md:grid-cols-4">
        <Field label={`Parameters (JSON; defaults ${data ? JSON.stringify(data.defaults) : ""})`}><input className={inputCls + " font-mono"} value={params} onChange={(e) => setParams(e.target.value)} /></Field>
        <Field label="Regime"><select className={inputCls} value={regime} onChange={(e) => setRegime(e.target.value)}>
          <option value="">any regime</option>{(data?.regimes || []).map((r: string) => <option key={r} value={r}>{r}</option>)}</select></Field>
        <Field label="Reason"><input className={inputCls} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <div className="flex items-end gap-1">
          <Btn small busy={busy} onClick={() => run(async () => { let p; try { p = JSON.parse(params); } catch { throw new Error("parameters: invalid JSON"); }
            await post(`/api/strategies/${code}/profiles`, { params: p, regime: regime || null, reason }); }, "Profile created and queued for validation")}>New profile</Btn>
          <Btn small tone="violet" busy={busy} onClick={() => run(() => post(`/api/strategies/${code}/profiles/propose`), "AI proposal recorded and queued for validation")}>Ask AI</Btn>
        </div></div>}
      <Confirm open={!!live} title={live ? `Activate ${code} v${live.version} on LIVE` : ""} phrase="ACTIVATE LIVE PROFILE" busy={busy}
        body="LIVE accounts will use these parameters. Requires DEMO forward evidence; roll back at any time." onCancel={() => setLive(null)}
        onConfirm={(t) => { act(live.id, "activate_live", t); setLive(null); }} />
    </div>
  );
}
