"""Central orchestration record (RR Trading One Phase 9).

The engine's loops already run the agents' work (scan cycle, monitor, learner, trading worker). The orchestrator gives
each cycle an ID, collects what every agent actually did inside it (timings measured where the work happens, the
analyses/signals it covered, its output summary, errors), and writes one typed run record per agent per cycle into
`agent_runs`, plus a master record for the cycle. Cycles in which no agent did any work write nothing (the scanner's
own status already says "nothing due").

Recording never affects trading: every recording path is best effort and isolated from the work it describes.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import delete

from app.db.models_ai import AgentRun
from app.db.session import session_scope
from app.orchestration.agents import BY_ID

log = logging.getLogger("orchestrator")
STATUSES = ("ok", "partial", "error", "skipped")


class AgentRunRecord(BaseModel):
    """The typed agent contract (specification Part 9): every field the specification lists."""
    cycle_id: str
    agent_id: str
    agent_version: str
    task: str
    input_refs: dict = Field(default_factory=dict)
    output_schema: str
    output: dict = Field(default_factory=dict)
    status: str
    started_at: datetime
    completed_at: datetime
    duration_ms: float | None
    provider: str | None = None
    model: str | None = None
    evidence: dict = Field(default_factory=dict)       # confidence / evidence quality, stated, never invented
    errors: list[str] = Field(default_factory=list)


def write(records: list[AgentRunRecord]) -> None:
    if not records:
        return
    try:
        with session_scope() as db:
            for r in records:
                if r.agent_id not in BY_ID or r.status not in STATUSES:
                    raise ValueError(f"invalid agent run {r.agent_id}/{r.status}")
                db.add(AgentRun(**r.model_dump()))
    except Exception:  # noqa: BLE001 - the audit trail must not break the work it records
        log.exception("agent run record failed")


def record(agent_id: str, task: str, *, started_at: datetime, duration_ms: float | None, status: str = "ok",
           input_refs: dict | None = None, output: dict | None = None, evidence: dict | None = None,
           errors: list[str] | None = None, provider: str | None = None, model: str | None = None,
           cycle_id: str | None = None) -> None:
    """One standalone run (monitor, learner, trading worker, API)."""
    spec = BY_ID[agent_id]
    write([AgentRunRecord(cycle_id=cycle_id or str(uuid.uuid4()), agent_id=agent_id, agent_version=spec.version,
                          task=task, input_refs=input_refs or {}, output_schema=spec.output_schema, output=output or {},
                          status=status, started_at=started_at, completed_at=datetime.now(UTC),
                          duration_ms=None if duration_ms is None else round(duration_ms, 3), provider=provider,
                          model=model, evidence=evidence or {}, errors=(errors or [])[:20])])


class _Acc:
    def __init__(self):
        self.n = 0
        self.ms = 0.0
        self.measured = True
        self.refs: list = []
        self.out: dict = defaultdict(int)
        self.errors: list[str] = []
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.provider = None
        self.model = None
        self.evidence: dict = {}


class Cycle:
    """Collects agent work for one scan cycle; thread-safe (jobs run on a pool)."""

    def __init__(self, task: str = "scan_cycle"):
        self.id = str(uuid.uuid4())
        self.task = task
        self.started_at = datetime.now(UTC)
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self._acc: dict[str, _Acc] = defaultdict(_Acc)

    @contextmanager
    def step(self, agent_id: str, ref=None):
        """Time one unit of an agent's work. Exceptions are recorded and re-raised (the caller decides)."""
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        err = None
        try:
            yield
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise
        finally:
            self.add(agent_id, ref=ref, ms=(time.perf_counter() - t0) * 1000, started=started,
                     error=err)

    def add(self, agent_id: str, *, ref=None, ms: float | None = None, started: datetime | None = None,
            out: dict | None = None, error: str | None = None, provider: str | None = None, model: str | None = None,
            evidence: dict | None = None) -> None:
        now = datetime.now(UTC)
        with self._lock:
            a = self._acc[agent_id]
            a.n += 1
            if ms is None:
                a.measured = False
            else:
                a.ms += ms
            if ref is not None and len(a.refs) < 200:
                a.refs.append(ref)
            for k, v in (out or {}).items():
                if isinstance(v, (int, float)):
                    a.out[k] += v
            if error and len(a.errors) < 10:
                a.errors.append(error)
            a.first = min(a.first or (started or now), started or now)
            a.last = now
            if provider:
                a.provider, a.model = provider, model
            if evidence:
                a.evidence.update(evidence)

    def note(self, agent_id: str, out: dict | None = None, evidence: dict | None = None) -> None:
        """Add output counts / evidence to an agent's record without counting another unit of work."""
        with self._lock:
            a = self._acc[agent_id]
            for k, v in (out or {}).items():
                if isinstance(v, (int, float)):
                    a.out[k] += v
            if evidence:
                a.evidence.update(evidence)

    def finish(self, summary: dict) -> None:
        recs = []
        with self._lock:
            for agent_id, a in self._acc.items():
                spec = BY_ID[agent_id]
                status = "ok" if not a.errors else ("error" if len(a.errors) >= a.n else "partial")
                recs.append(AgentRunRecord(
                    cycle_id=self.id, agent_id=agent_id, agent_version=spec.version, task=self.task,
                    input_refs={"units": a.n, "refs": a.refs}, output_schema=spec.output_schema,
                    output=dict(a.out), status=status, started_at=a.first or self.started_at,
                    completed_at=a.last or datetime.now(UTC),
                    duration_ms=round(a.ms, 3) if a.measured else None,
                    provider=a.provider, model=a.model,
                    evidence=a.evidence or ({"basis": "deterministic computation"} if spec.kind == "deterministic" else {}),
                    errors=a.errors))
        if not recs:
            return                                                     # nothing ran: nothing to record
        m = BY_ID["master_orchestrator"]
        recs.append(AgentRunRecord(
            cycle_id=self.id, agent_id=m.id, agent_version=m.version, task=self.task,
            input_refs={"agents": sorted(a.agent_id for a in recs)}, output_schema=m.output_schema, output=summary,
            status="ok" if not any(r.status in ("error", "partial") for r in recs) else "partial",
            started_at=self.started_at, completed_at=datetime.now(UTC),
            duration_ms=round((time.perf_counter() - self._t0) * 1000, 3),
            evidence={"basis": "records of the agents that ran in this cycle"},
            errors=[f"{r.agent_id}: {e}" for r in recs for e in r.errors][:10]))
        write(recs)


class NullCycle(Cycle):
    """Used when no cycle is being recorded (tests, direct calls): same interface, records nothing."""

    def add(self, *a, **k):  # noqa: D401
        return None

    def note(self, *a, **k):
        return None

    def finish(self, summary: dict) -> None:
        return None


def prune(days: int) -> int:
    try:
        with session_scope() as db:
            res = db.execute(delete(AgentRun).where(AgentRun.created_at < datetime.now(UTC) - timedelta(days=days)))
            return res.rowcount or 0
    except Exception:  # noqa: BLE001
        return 0
