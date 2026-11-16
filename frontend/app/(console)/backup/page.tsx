"use client";
import { PageTitle } from "@/components/Shell";
import { Panel, Stat, Table } from "@/components/ui";
import { useApi } from "@/lib/hooks";

/** Part 17 item 26. Backups are files on this computer, so they are taken, listed and restored by the desktop
 *  application (which runs the database container). This page shows what is being protected. */
export default function BackupRecovery() {
  const { data: d } = useApi<any>("/api/system/data", 60000);
  return (
    <div>
      <PageTitle sub="Backups are PostgreSQL dumps on this computer, managed by the RR Trading One desktop window.">Backup and Recovery</PageTitle>
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-3">
        <Stat label="Database schema" value={d?.schema_version ?? "—"} sub="migration revision" />
        <Stat label="Database size" value={d ? `${(d.database_bytes / 1e6).toFixed(1)} MB` : "—"} />
        <Stat label="Backups" value="desktop window" sub="Backup and Recovery section" />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="How backups work">
          <ul className="list-disc space-y-1 pl-4 text-[12px]">
            <li><b>Automatic:</b> a backup is taken before every database migration (each upgrade). The newest 10 are kept.</li>
            <li><b>On request:</b> desktop window → Backup and Recovery → <i>Back up now</i>. The newest 30 are kept.</li>
            <li><b>Restore:</b> choose a backup and type <code>RESTORE DATABASE</code>. A safety backup of the current database is taken first,
              the engine is stopped (trading worker first), the backup is restored in one transaction (all or nothing), newer migrations are
              applied, auto trading is set to OFF and the engine must reconcile with the broker before any entry. The trading worker is not restarted.</li>
            <li>Backups never leave this computer. Copy the backups folder (shown under Locations in the desktop window) to external storage to protect against disk loss.</li>
          </ul>
        </Panel>
        <Panel title="What a backup contains (current record counts)">
          <Table rows={d ? Object.entries(d.records).map(([k, v]) => ({ table: k, rows: v })) : []} rowKey={(r) => r.table}
            cols={[{ k: "table", h: "Table" }, { k: "rows", h: "Records" }]} />
          <div className="mt-1 text-[10px] text-dim">Every table of the engine database is included; these are the main ones. Secrets (keys, passwords) are not in the database: they stay in this computer's protected storage.</div>
        </Panel>
      </div>
    </div>
  );
}
