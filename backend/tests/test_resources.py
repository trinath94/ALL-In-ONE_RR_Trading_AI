"""Performance and resource management (RR Trading One Phase 20, specification Part 20).

* Isolated learner jobs: a run is its own process, so a timeout, an operator's cancel or the learner stopping really
  stops it (before, a timed-out job thread kept running and the next loop started another copy).
* Hot-path indexes and the two queries rewritten to use them return exactly what the old queries returned.
"""
import os
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.db.models import AnalysisResult, Symbol
from app.db.models_ai import AgentRun
from app.db.models_intel import LearningJob
from app.db.session import session_scope
from app.learning_engine import scheduler
from app.services.events import get_redis
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_trade_intelligence import accounts

NOW = datetime.now(UTC)


# ---- module-level job functions: an isolated run imports them in its own process ------------------------------
def job_ok():
    return {"value": 42, "pid": os.getpid()}


def job_boom():
    raise RuntimeError("isolated failure")


def job_forever():
    with open(os.environ["RR_TEST_PIDFILE"], "w") as f:
        f.write(str(os.getpid()))
    while True:
        time.sleep(0.1)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a terminated child the parent has already reaped is gone; a zombie would still be signalable
    with open(f"/proc/{pid}/stat") as f:
        return f.read().split()[2] != "Z"


def row(name: str) -> LearningJob:
    with session_scope() as db:
        return db.scalar(select(LearningJob).where(LearningJob.job_type == name).order_by(LearningJob.id.desc()))


def wait_pid(path) -> int:
    for _ in range(1200):                      # up to 60 s: process start-up on a slow CI runner
        if os.path.exists(path) and open(path).read():
            return int(open(path).read())
        time.sleep(0.05)
    raise AssertionError("the isolated job never started")


def test_every_production_learner_job_is_isolated():
    assert scheduler.JOBS and all(j.isolated for j in scheduler.JOBS)


def test_isolated_job_runs_in_its_own_process_and_records_result_or_failure(db_clean):
    out = scheduler.run_job(scheduler.Job("t_iso_ok", 3600, 60, job_ok, isolated=True))
    assert out["status"] == "DONE" and out["result"]["value"] == 42
    assert out["result"]["pid"] != os.getpid(), "ran in a separate process"
    assert row("t_iso_ok").result == {"value": 42, "pid": out["result"]["pid"]}
    assert scheduler.run_job(scheduler.Job("t_iso_ok", 3600, 60, job_ok, isolated=True))["skipped"] is True
    bad = scheduler.run_job(scheduler.Job("t_iso_bad", 3600, 60, job_boom, isolated=True))
    assert bad["status"] == "FAILED" and "isolated failure" in row("t_iso_bad").error


def test_a_timed_out_job_is_terminated_not_left_running(db_clean, tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("RR_TEST_PIDFILE", str(pidfile))
    # Generous timeout: starting the job's process (a fresh interpreter importing the engine) can take seconds on a
    # slow machine; the test waits until the job is running, so it measures the timeout, not the start-up.
    job = scheduler.Job("t_iso_slow", 3600, 20, job_forever, isolated=True)
    res = {}
    th = threading.Thread(target=lambda: res.update(scheduler.run_job(job)))
    th.start()
    pid = wait_pid(pidfile)
    assert alive(pid)
    th.join(20 + scheduler.POLL_SECONDS + scheduler.KILL_GRACE_SECONDS + 30)
    assert not th.is_alive() and res["status"] == "TIMEOUT"
    assert not alive(pid), "the job's process was terminated"
    assert "process terminated" in row("t_iso_slow").error


def test_an_operator_cancel_terminates_the_run_and_it_is_not_retried_in_the_period(db_clean, tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("RR_TEST_PIDFILE", str(pidfile))
    job = scheduler.Job("t_iso_cancel", 3600, 120, job_forever, isolated=True)
    res = {}
    th = threading.Thread(target=lambda: res.update(scheduler.run_job(job)))
    th.start()
    pid = wait_pid(pidfile)
    get_redis().set(scheduler.cancel_key(job.name), "1")
    th.join(30)
    assert not th.is_alive() and res["status"] == "CANCELLED" and not alive(pid)
    assert scheduler.run_job(job)["skipped"] is True, "a cancelled run is final for its period"


def test_learner_stopping_interrupts_the_run_which_is_retried_later(db_clean, tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("RR_TEST_PIDFILE", str(pidfile))
    job = scheduler.Job("t_iso_stop", 3600, 120, job_forever, isolated=True)
    stop = {"flag": False}
    res = {}
    th = threading.Thread(target=lambda: res.update(scheduler.run_job(job, should_stop=lambda: stop["flag"])))
    th.start()
    pid = wait_pid(pidfile)
    stop["flag"] = True
    th.join(30)
    assert res["status"] == "INTERRUPTED" and not alive(pid)
    assert row("t_iso_stop").attempts == 1
    monkeypatch.setattr(job, "fn", job_ok)
    assert scheduler.run_job(job)["status"] == "DONE", "retried after the stop"
    assert row("t_iso_stop").attempts == 2


def test_cancel_endpoint(client, db_clean):
    login(client, "trader")
    assert client.post("/api/learning/jobs/drift/cancel", headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post("/api/learning/jobs/nope/cancel", headers=h(client)).status_code == 404
    r = client.post("/api/learning/jobs/drift/cancel", headers=h(client))
    assert r.status_code == 409 and "not running" in r.json()["detail"]
    assert not get_redis().get(scheduler.cancel_key("drift")), "nothing is left to cancel a future run"
    with session_scope() as db:
        db.add(LearningJob(job_type="drift", idempotency_key="drift:test", status="RUNNING", attempts=1, params={}))
    r = client.post("/api/learning/jobs/drift/cancel", headers=h(client))
    assert r.status_code == 200 and get_redis().get(scheduler.cancel_key("drift"))


# ---- indexes and the rewritten queries -----------------------------------------------------------------------
def test_hot_path_indexes_exist(db_clean):
    with session_scope() as db:
        names = set(db.scalars(text("select indexname from pg_indexes where schemaname = 'public'")))
        assert {"ix_trading_signals_new_expires", "ix_agent_runs_agent_id_id", "ix_analysis_results_symbol_tf_id"} <= names
        plan = "\n".join(db.scalars(text(
            "EXPLAIN SELECT id FROM trading_signals WHERE status = 'NEW' AND expires_at < now()")))
        db.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(db.scalars(text(
            "EXPLAIN SELECT id FROM trading_signals WHERE status = 'NEW' AND expires_at < now()")))
        assert "ix_trading_signals_new_expires" in plan, plan


def _analysis(sym_id, tf, minutes, score):
    return AnalysisResult(symbol_id=sym_id, timeframe=tf, bar_time=NOW - timedelta(minutes=minutes), regime="TRENDING",
                          structure={}, strategy_outputs=[], score={"decision": "WAIT", "score": score, "strategies": []},
                          created_at=NOW - timedelta(minutes=minutes))


def test_scanner_page_returns_the_latest_analysis_of_every_symbol_and_timeframe(client, db_clean):
    demo, _ = accounts()
    with session_scope() as db:
        conn = db.get(__import__("app.db.models", fromlist=["TradingAccount"]).TradingAccount, demo).broker_connection_id
        syms = []
        for name in ("EURUSD.r", "XAUUSD.r", "NEVER.r"):
            s = Symbol(broker_connection_id=conn, broker_symbol=name, canonical=name[:6], category="forex", point=0.00001,
                       tick_size=0.00001, tick_value=1, contract_size=100000, volume_min=0.01, volume_max=100,
                       volume_step=0.01)
            db.add(s)
            db.flush()
            syms.append(s.id)
        # several analyses per pair; the latest (highest id) is added last, with a distinctive score
        for sid in syms[:2]:
            for tf in ("M15", "H1"):
                for k, minutes in enumerate((300, 200, 100)):
                    db.add(_analysis(sid, tf, minutes, score=sid * 100 + (10 if tf == "H1" else 0) + k))
    login(client, "viewer")
    rows = client.get("/api/scanner").json()
    got = {(r["symbol"], r["timeframe"]): r["score"] for r in rows}
    want = {(n, tf): sid * 100 + (10 if tf == "H1" else 0) + 2 for n, sid in zip(("EURUSD.r", "XAUUSD.r"), syms)
            for tf in ("M15", "H1")}
    assert got == want, "exactly one row per analysed pair, the latest; a never-analysed symbol has none"
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    only_h1 = client.get("/api/scanner", params={"timeframe": "H1"}).json()
    assert {(r["symbol"], r["timeframe"]) for r in only_h1} == {(n, "H1") for n in ("EURUSD.r", "XAUUSD.r")}


def test_control_room_shows_each_agents_latest_run(client, db_clean):
    with session_scope() as db:
        for i, st in enumerate(("ok", "error", "ok")):
            db.add(AgentRun(cycle_id=f"c{i}", agent_id="market_regime", agent_version="1.0", task=f"t{i}", input_refs={},
                            output_schema="x", output={}, status=st, started_at=NOW, completed_at=NOW, duration_ms=1,
                            evidence={}, errors=[]))
        db.add(AgentRun(cycle_id="x", agent_id="not_registered", agent_version="1.0", task="x", input_refs={},
                        output_schema="x", output={}, status="ok", started_at=NOW, completed_at=NOW, duration_ms=1,
                        evidence={}, errors=[]))
    login(client, "viewer")
    agents = {a["id"]: a for a in client.get("/api/agents").json()}
    assert "not_registered" not in agents
    mr = agents["market_regime"]
    last = mr.get("last_run") or {}
    assert last.get("task") == "t2", mr


# ---- CPU / memory / disk monitoring --------------------------------------------------------------------------
def _v2(root, mem, limit, inactive, usage_usec, cpu_max):
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.current").write_text(f"{mem}\n")
    (root / "memory.max").write_text(f"{limit}\n")
    (root / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\nactive_file 2\n")
    (root / "cpu.stat").write_text(f"usage_usec {usage_usec}\nuser_usec 1\n")
    (root / "cpu.max").write_text(f"{cpu_max}\n")


def test_cgroup_v2_and_v1_are_read_like_docker_stats(tmp_path):
    from app.services import resources as res
    v2 = tmp_path / "v2"
    _v2(v2, mem=600 * 2**20, limit=768 * 2**20, inactive=100 * 2**20, usage_usec=5_000_000, cpu_max="50000 100000")
    s = res.cgroup_sample(v2)
    assert s["cgroup"] == "v2" and s["mem_bytes"] == 500 * 2**20 and s["mem_limit"] == 768 * 2**20
    assert s["cpu_seconds"] == 5.0 and s["cpu_cap"] == 0.5
    _v2(tmp_path / "nolimit", 10, "max", 0, 1, "max 100000")
    s = res.cgroup_sample(tmp_path / "nolimit")
    assert s["mem_limit"] is None and s["cpu_cap"] is None
    v1 = tmp_path / "v1"
    for d in ("memory", "cpu", "cpuacct"):
        (v1 / d).mkdir(parents=True)
    (v1 / "memory/memory.usage_in_bytes").write_text("2000\n")
    (v1 / "memory/memory.limit_in_bytes").write_text(f"{2**63 - 4096}\n")
    (v1 / "memory/memory.stat").write_text("total_inactive_file 500\n")
    (v1 / "cpuacct/cpuacct.usage").write_text("3000000000\n")
    (v1 / "cpu/cpu.cfs_quota_us").write_text("-1\n")
    (v1 / "cpu/cpu.cfs_period_us").write_text("100000\n")
    s = res.cgroup_sample(v1)
    assert s == {"cgroup": "v1", "mem_bytes": 1500, "mem_limit": None, "cpu_seconds": 3.0, "cpu_cap": None}
    assert res.cgroup_sample(tmp_path / "none")["cgroup"] is None


def test_thresholds_and_cpu_rate(tmp_path):
    from app.services import resources as res
    root = tmp_path / "cg"
    _v2(root, mem=700 * 2**20, limit=1000 * 2**20, inactive=0, usage_usec=10_000_000, cpu_max="200000 100000")
    t = iter([100.0, 110.0])
    first = res.sample("t_role", root, clock=lambda: next(t))
    assert first["cpu_pct_of_cap"] is None and first["status"] == "OK" and first["mem_pct"] == 70.0
    _v2(root, mem=960 * 2**20, limit=1000 * 2**20, inactive=0, usage_usec=29_500_000, cpu_max="200000 100000")
    second = res.sample("t_role", root, clock=lambda: next(t))
    assert second["cpu_cores"] == 1.95 and second["cpu_pct_of_cap"] == 97.5
    assert second["status"] == "CRITICAL" and any("memory at 96%" in x for x in second["reasons"])
    assert any("throttled" in x for x in second["reasons"])
    assert res.assess({"mem_bytes": 86, "mem_limit": 100})["status"] == "WARN"
    assert res.assess({"mem_bytes": None, "mem_limit": None})["status"] == "UNKNOWN"


def test_resources_endpoint_and_critical_alerts(client, db_clean, monkeypatch):
    import json as _json

    from app.services import resources as res
    r = get_redis()
    r.set(res.KEY.format(role="monitor"), _json.dumps({"status": "OK", "reasons": [], "mem_pct": 20.0}))
    r.set(res.KEY.format(role="learner"), _json.dumps({"status": "CRITICAL", "reasons": ["memory at 97% of its limit"],
                                                        "mem_pct": 97.0}))
    login(client, "viewer")
    o = client.get("/api/system/resources").json()
    assert o["roles"]["monitor"]["status"] == "OK" and o["roles"]["analysis"]["status"] == "NO_REPORT"
    assert o["database_bytes"] > 0 and o["disk"]["free_gb"] > 0
    assert {"kind": "RESOURCES_LEARNER", "severity": "critical", "detail": "learner: memory at 97% of its limit"} in o["alerts"]
    ov = client.get("/api/dashboard/overview").json()
    assert any(a["kind"] == "RESOURCES_LEARNER" for a in ov["critical_alerts"]), "an imminent out-of-memory is active"
    monkeypatch.setattr(res, "disk", lambda path="/": {"free_gb": 1.2, "total_gb": 100, "used_pct": 98.8, "status": "CRITICAL"})
    ov = client.get("/api/dashboard/overview").json()
    assert any(a["kind"] == "DISK_SPACE" for a in ov["critical_alerts"])
