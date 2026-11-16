"""CPU, memory and disk monitoring (RR Trading One Phase 20, specification Part 20).

Each role process (monitor, analysis, trading worker, learner) reports **its own container's** usage against its own
limits, read from the Linux control group the container runs in (cgroup v2 on Docker Desktop / WSL2, v1 elsewhere),
so the numbers are what the limits in infrastructure/compose.yml apply to. Reports live in Redis for two minutes:
a role that stopped reporting shows as "no report", never as healthy.

Thresholds (fixed, conservative):
* memory >= 85 % of the container limit: WARN; >= 95 %: CRITICAL (the container is about to be killed by the OOM
  killer). Memory excludes reclaimable file cache, as ``docker stats`` does.
* CPU is reported as % of the container's CPU cap over the last interval; a role pinned at its cap is WARN
  (it is being throttled), never critical by itself.
* disk free on the volume that holds the database < 5 GB: WARN; < 2 GB: CRITICAL (PostgreSQL stops accepting
  writes when the disk is full).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path("/sys/fs/cgroup")
KEY = "rr:resources:{role}"
TTL = 120
ROLES = ("monitor", "analysis", "trading", "learner")
MEM_WARN, MEM_CRIT = 85.0, 95.0
CPU_WARN = 95.0
DISK_WARN_GB, DISK_CRIT_GB = 5.0, 2.0
_last_cpu: dict[str, tuple[float, float]] = {}   # role -> (monotonic time, cpu seconds)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _stat(path: Path, key: str) -> int | None:
    text = _read(path) or ""
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k == key and v.strip().isdigit():
            return int(v)
    return None


def cgroup_sample(root: Path = ROOT) -> dict:
    """Memory (bytes, excluding inactive file cache), memory limit, CPU seconds used and CPU cap (cores) of the
    current container. Missing values are None (not in a container, or an unknown layout)."""
    if (root / "memory.current").exists():                      # cgroup v2
        cur = _read(root / "memory.current")
        lim = _read(root / "memory.max")
        inactive = _stat(root / "memory.stat", "inactive_file") or 0
        usage_usec = _stat(root / "cpu.stat", "usage_usec")
        cpu_max = (_read(root / "cpu.max") or "max").split()
        cap = (int(cpu_max[0]) / int(cpu_max[1])) if len(cpu_max) == 2 and cpu_max[0].isdigit() else None
        return {"cgroup": "v2", "mem_bytes": max(int(cur) - inactive, 0) if cur and cur.isdigit() else None,
                "mem_limit": int(lim) if lim and lim.isdigit() else None,
                "cpu_seconds": usage_usec / 1e6 if usage_usec is not None else None, "cpu_cap": cap}
    mem = root / "memory"
    if (mem / "memory.usage_in_bytes").exists():                 # cgroup v1
        cur = _read(mem / "memory.usage_in_bytes")
        lim = _read(mem / "memory.limit_in_bytes")
        inactive = _stat(mem / "memory.stat", "total_inactive_file") or 0
        usage_ns = _read(root / "cpuacct" / "cpuacct.usage")
        quota, period = _read(root / "cpu" / "cpu.cfs_quota_us"), _read(root / "cpu" / "cpu.cfs_period_us")
        limit = int(lim) if lim and lim.isdigit() else None
        return {"cgroup": "v1", "mem_bytes": max(int(cur) - inactive, 0) if cur and cur.isdigit() else None,
                # v1 reports "no limit" as a huge number
                "mem_limit": limit if limit is not None and limit < 1 << 60 else None,
                "cpu_seconds": int(usage_ns) / 1e9 if usage_ns and usage_ns.isdigit() else None,
                "cpu_cap": int(quota) / int(period) if quota and period and quota.lstrip("-").isdigit()
                and int(quota) > 0 and period.isdigit() else None}
    return {"cgroup": None, "mem_bytes": None, "mem_limit": None, "cpu_seconds": None, "cpu_cap": None}


def assess(s: dict) -> dict:
    """Add percentages and a status (OK / WARN / CRITICAL / UNKNOWN) to a sample."""
    out = dict(s)
    mem_pct = round(100 * s["mem_bytes"] / s["mem_limit"], 1) if s.get("mem_bytes") is not None and s.get("mem_limit") else None
    out["mem_pct"] = mem_pct
    status, reasons = "OK", []
    if mem_pct is None:
        status = "UNKNOWN"
    elif mem_pct >= MEM_CRIT:
        status, reasons = "CRITICAL", [f"memory at {mem_pct:.0f}% of its limit"]
    elif mem_pct >= MEM_WARN:
        status, reasons = "WARN", [f"memory at {mem_pct:.0f}% of its limit"]
    cpu = s.get("cpu_pct_of_cap")
    if cpu is not None and cpu >= CPU_WARN:
        reasons.append(f"CPU at its cap ({cpu:.0f}%): throttled")
        status = "WARN" if status in ("OK", "UNKNOWN") else status
    out["status"], out["reasons"] = status, reasons
    return out


def sample(role: str, root: Path = ROOT, clock=time.monotonic) -> dict:
    s = cgroup_sample(root)
    now = clock()
    prev = _last_cpu.get(role)
    if s["cpu_seconds"] is not None:
        _last_cpu[role] = (now, s["cpu_seconds"])
    if prev and s["cpu_seconds"] is not None and now > prev[0]:
        cores = (s["cpu_seconds"] - prev[1]) / (now - prev[0])
        s["cpu_cores"] = round(max(cores, 0.0), 3)
        s["cpu_pct_of_cap"] = round(100 * cores / s["cpu_cap"], 1) if s["cpu_cap"] else None
    else:
        s["cpu_cores"], s["cpu_pct_of_cap"] = None, None
    s["pid"], s["at"] = os.getpid(), time.time()
    return assess(s)


def publish(role: str, r=None, root: Path = ROOT) -> dict:
    from app.services.events import get_redis
    s = sample(role, root)
    (r or get_redis()).set(KEY.format(role=role), json.dumps(s), ex=TTL)
    return s


class Reporter:
    """Publish at most every ``every`` seconds from a role's loop (cheap: a few small file reads)."""

    def __init__(self, role: str, every: float = 15.0):
        self.role, self.every, self.last = role, every, 0.0

    def tick(self, r=None) -> None:
        if time.monotonic() - self.last >= self.every:
            self.last = time.monotonic()
            try:
                publish(self.role, r)
            except Exception:  # noqa: BLE001 - monitoring must never break a role
                pass


def disk(path: str = "/") -> dict:
    import psutil
    d = psutil.disk_usage(path)
    free_gb = d.free / 1e9
    status = "CRITICAL" if free_gb < DISK_CRIT_GB else "WARN" if free_gb < DISK_WARN_GB else "OK"
    return {"free_gb": round(free_gb, 2), "total_gb": round(d.total / 1e9, 2), "used_pct": d.percent, "status": status}


def overview(db, r=None) -> dict:
    """Every role's last report, the disk that holds the data, the database size, and the active alerts."""
    from sqlalchemy import text

    from app.services.events import get_redis
    r = r or get_redis()
    roles = {}
    for role in ROLES:
        raw = r.get(KEY.format(role=role))
        roles[role] = json.loads(raw) if raw else {"status": "NO_REPORT"}
    dk = disk()
    db_bytes = db.scalar(text("SELECT pg_database_size(current_database())"))
    alerts = [{"kind": f"RESOURCES_{role.upper()}", "severity": v["status"].lower(), "detail": f"{role}: " + "; ".join(v["reasons"])}
              for role, v in roles.items() if v.get("status") in ("WARN", "CRITICAL")]
    if dk["status"] != "OK":
        alerts.append({"kind": "DISK_SPACE", "severity": dk["status"].lower(),
                       "detail": f"{dk['free_gb']:.1f} GB free on the volume that holds the database"})
    return {"roles": roles, "disk": dk, "database_bytes": db_bytes, "alerts": alerts,
            "thresholds": {"memory_warn_pct": MEM_WARN, "memory_critical_pct": MEM_CRIT, "cpu_warn_pct_of_cap": CPU_WARN,
                           "disk_warn_gb": DISK_WARN_GB, "disk_critical_gb": DISK_CRIT_GB}}
