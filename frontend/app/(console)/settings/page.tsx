"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, Confirm, ErrorLine, Field, inputCls, Panel, Table } from "@/components/ui";
import { api, fmt, patch, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function Settings() {
  const me = useMe();
  const admin = can(me, "system.manage");
  const { data: accts, reload } = useApi<any[]>("/api/accounts");
  const { data: syms, reload: rsym } = useApi<any[]>("/api/symbols");
  const [a, setA] = useState({ login: "", server: "", mode: "DEMO", password: "", broker_name: "", terminal_name: "mt5-windows-1", timezone: "Etc/GMT-2" });
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const [approve, setApprove] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null); const [err, setErr] = useState<string | null>(null);
  const run = async (fn: () => Promise<any>, ok = "Done") => { setErr(null); setMsg(null); try { await fn(); setMsg(ok); reload(); rsym(); } catch (e: any) { setErr(e.message); } };
  return (
    <div>
      <PageTitle sub="Broker accounts, symbol discovery and personal security">Settings</PageTitle>
      <ErrorLine error={err} />{msg && <div className="mb-2 text-[11px] text-neon">{msg}</div>}
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Trading accounts">
          <Table rows={accts} empty="No accounts configured" cols={[{ k: "login", h: "Login" }, { k: "server", h: "Server" },
            { k: "mode", h: "Mode", r: (x) => <Badge tone={x.mode === "LIVE" ? "bear" : "cyan"}>{x.mode}</Badge> },
            { k: "verified", h: "Verified", r: (x) => x.verified ? <Badge tone="neon">{fmt.time(x.verified_at)}</Badge> : <Badge tone="warn">no</Badge> },
            { k: "live_approved", h: "Live approved", r: (x) => x.mode === "LIVE" ? (x.live_approved ? "yes" : "no") : "n/a" },
            { k: "is_active", h: "Active", r: (x) => x.is_active ? <Badge tone="neon">active</Badge> : "" },
            { k: "act", h: "", sort: false, r: (x) => admin && <div className="flex gap-1">
              <Btn small onClick={() => run(() => post(`/api/accounts/${x.id}/verify`), "Account verified against MT5")}>Verify</Btn>
              <Btn small onClick={() => run(() => post(`/api/accounts/${x.id}/activate`), "Activated (auto trading OFF, reconciliation required)")}>Activate</Btn>
              {x.mode === "LIVE" && !x.live_approved && can(me, "trade.live") && <Btn small tone="bear" onClick={() => setApprove(x)}>Approve live</Btn>}</div> }]} />
          {admin && <div className="mt-3 grid grid-cols-2 gap-2">
            <Field label="MT5 login"><input className={inputCls} value={a.login} onChange={(e) => setA({ ...a, login: e.target.value })} /></Field>
            <Field label="Server"><input className={inputCls} value={a.server} onChange={(e) => setA({ ...a, server: e.target.value })} /></Field>
            <Field label="Mode"><select className={inputCls} value={a.mode} onChange={(e) => setA({ ...a, mode: e.target.value })}><option>DEMO</option><option>LIVE</option></select></Field>
            <Field label="Broker name"><input className={inputCls} value={a.broker_name} onChange={(e) => setA({ ...a, broker_name: e.target.value })} /></Field>
            <Field label="Password (encrypted at rest; optional)"><input type="password" className={inputCls} value={a.password} onChange={(e) => setA({ ...a, password: e.target.value })} /></Field>
            <Field label="Broker timezone (IANA)"><input className={inputCls} value={a.timezone} onChange={(e) => setA({ ...a, timezone: e.target.value })} /></Field>
            <div><Btn tone="neon" onClick={() => run(() => post("/api/accounts", { ...a, login: Number(a.login), password: a.password || null }), "Account added — verify it next")}>Add account</Btn></div>
          </div>}
        </Panel>
        <Panel title="Symbols" right={admin && <Btn small onClick={() => run(async () => { const r = await post("/api/symbols/discover"); setMsg(`Discovered ${r.discovered} symbols from MT5`); })}>Discover from MT5</Btn>}>
          <Table rows={syms} empty="Run discovery after configuring an account" cols={[{ k: "symbol", h: "Symbol" }, { k: "category", h: "Class" },
            { k: "tradable", h: "Tradable", r: (s) => s.tradable ? "yes" : "no" },
            { k: "scan_enabled", h: "Scan", r: (s) => <input type="checkbox" disabled={!can(me, "strategy.manage")} checked={s.scan_enabled} onChange={() => run(() => patch(`/api/symbols/${s.id}`, { scan_enabled: !s.scan_enabled }))} /> },
            { k: "priority", h: "Priority", r: (s) => <input className={inputCls + " w-16"} defaultValue={s.priority} disabled={!can(me, "strategy.manage")}
              onBlur={(e) => +e.target.value !== s.priority && run(() => patch(`/api/symbols/${s.id}`, { priority: +e.target.value }))} /> }]} />
        </Panel>
        <Panel title="Change password"><div className="grid grid-cols-2 gap-2">
          <Field label="Current"><input type="password" className={inputCls} value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} /></Field>
          <Field label="New (≥10 chars)"><input type="password" className={inputCls} value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} /></Field>
          <div><Btn onClick={() => run(() => api("/api/auth/password", { method: "POST", body: pw }), "Password changed; other sessions revoked")}>Update</Btn></div></div></Panel>
      </div>
      <Confirm open={!!approve} title="Approve LIVE account" phrase={approve ? `APPROVE ${approve.login}` : undefined}
        body="This authorises real-money trading on this account. It is recorded in the audit trail." onCancel={() => setApprove(null)}
        onConfirm={(t) => run(async () => { await post(`/api/accounts/${approve.id}/approve-live`, { confirm_text: t }); setApprove(null); }, "LIVE account approved")} />
    </div>
  );
}
