"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, Empty, ErrorLine, Field, inputCls, Panel, Table } from "@/components/ui";
import { fmt, patch, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function Users() {
  const me = useMe();
  const ok = can(me, "users.manage");
  const { data, reload } = useApi<any[]>(ok ? "/api/users" : null);
  const { data: hist } = useApi<any[]>(ok ? "/api/users/login-history?limit=100" : null);
  const [f, setF] = useState({ username: "", email: "", password: "", role: "trader" });
  const [err, setErr] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  if (!ok) return <><PageTitle>User Management</PageTitle><Empty>Administrator permission required</Empty></>;
  const run = async (fn: () => Promise<any>) => { setErr(null); try { await fn(); reload(); } catch (e: any) { setErr(e.message); } };
  return (
    <div>
      <PageTitle sub="Roles: admin (full), trader (manual trading & controls, no LIVE activation), readonly">User Management</PageTitle>
      <ErrorLine error={err} />
      <div className="grid gap-3 xl:grid-cols-[1fr_360px]">
        <Panel title="Users"><Table rows={data} cols={[{ k: "username", h: "User" }, { k: "email", h: "Email" },
          { k: "role", h: "Role", r: (u) => <select className={inputCls} value={u.role} onChange={(e) => run(() => patch(`/api/users/${u.id}`, { role: e.target.value }))}>
            {["admin", "trader", "readonly"].map((r) => <option key={r}>{r}</option>)}</select> },
          { k: "is_active", h: "Status", r: (u) => <Badge tone={u.is_active ? "neon" : "bear"}>{u.is_active ? "active" : "disabled"}</Badge> },
          { k: "a", h: "", sort: false, r: (u) => <div className="flex gap-1">
            <Btn small tone={u.is_active ? "warn" : "neon"} onClick={() => run(() => patch(`/api/users/${u.id}`, { is_active: !u.is_active }))}>{u.is_active ? "Disable" : "Enable"}</Btn>
            <Btn small onClick={() => run(async () => setToken((await post(`/api/users/${u.id}/reset-token`)).token))}>Reset token</Btn></div> }]} />
          {token && <div className="mt-2 rounded border border-warn/50 p-2 text-[11px]">One-time reset token (30 min, deliver out-of-band): <code className="text-warn">{token}</code></div>}
        </Panel>
        <Panel title="Create user"><div className="space-y-2">
          <Field label="Username"><input className={inputCls} value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} /></Field>
          <Field label="Email"><input className={inputCls} value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
          <Field label="Initial password (≥10 chars)"><input type="password" className={inputCls} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
          <Field label="Role"><select className={inputCls} value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{["admin", "trader", "readonly"].map((r) => <option key={r}>{r}</option>)}</select></Field>
          <Btn tone="neon" onClick={() => run(async () => { await post("/api/users", { ...f, email: f.email || null }); setF({ username: "", email: "", password: "", role: "trader" }); })}>Create</Btn>
        </div></Panel>
      </div>
      <Panel title="Login history" className="mt-3"><Table rows={hist} cols={[{ k: "at", h: "Time", r: (h) => fmt.time(h.at) }, { k: "username", h: "User" },
        { k: "success", h: "Result", r: (h) => <Badge tone={h.success ? "neon" : "bear"}>{h.success ? "success" : h.reason}</Badge> }, { k: "ip", h: "IP" },
        { k: "user_agent", h: "Agent", r: (h) => <span className="block max-w-[300px] truncate text-dim">{h.user_agent}</span> }]} /></Panel>
    </div>
  );
}
