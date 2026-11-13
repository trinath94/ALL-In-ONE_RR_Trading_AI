"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Panel, Table } from "@/components/ui";
import { api, fmt, put } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

export default function Notifications() {
  const me = useMe();
  const live = useLive(["notification"]);
  const { data, reload } = useApi<any[]>("/api/notifications", 0, live.tick);
  const { data: settings, reload: rs } = useApi<any[]>("/api/notifications/settings");
  const [err, setErr] = useState<string | null>(null);
  const unread = (data || []).filter((n) => !n.read).map((n) => n.id);
  async function toggle(row: any, ch: string) {
    try { await put("/api/notifications/settings", [{ ...row, [ch]: !row[ch] }]); rs(); } catch (e: any) { setErr(e.message); }
  }
  return (
    <div>
      <PageTitle right={!!unread.length && <Btn small onClick={async () => { await api("/api/notifications/read", { method: "POST", body: unread }); reload(); }}>Mark all read</Btn>}>Notifications</PageTitle>
      <ErrorLine error={err} />
      <div className="grid gap-3 xl:grid-cols-[1fr_420px]">
        <Panel><Table rows={data} empty="No notifications"
          cols={[{ k: "at", h: "Time", r: (n) => <span className={n.read ? "text-dim" : "text-text"}>{fmt.time(n.at)}</span> },
            { k: "severity", h: "Sev", r: (n) => <Badge tone={n.severity === "critical" ? "bear" : "cyan"}>{n.severity}</Badge> }, { k: "kind", h: "Kind" },
            { k: "title", h: "Title" }, { k: "body", h: "Detail", r: (n) => <span className="block max-w-[360px] truncate text-dim">{n.body}</span> },
            { k: "channels", h: "Delivered", r: (n) => n.channels.join(", ") }]} /></Panel>
        <Panel title="Channels per event">
          <Table rows={settings} cols={[{ k: "kind", h: "Event" }, ...["in_app", "telegram", "email"].map((ch) => ({ k: ch, h: ch.replace("_", "-"),
            r: (r: any) => <input type="checkbox" checked={r[ch]} disabled={!can(me, "system.manage")} onChange={() => toggle(r, ch)} /> }))]} />
          <p className="mt-2 text-[10px] text-dim">Telegram/e-mail require RR_TELEGRAM_* / RR_SMTP_* server configuration.</p>
        </Panel>
      </div>
    </div>
  );
}
