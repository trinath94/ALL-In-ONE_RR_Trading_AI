"""Claude Code (Claude Agent SDK) and Anthropic API integration in the Engine Assistant.

The Claude Code session is scripted with the SDK's own message types (no network, no model); the workspace, git
worktree, the engine's test run, the patch review, approval gates, cancellation and resume are real."""
import asyncio
import subprocess
import sys

import pytest
from claude_agent_sdk import (AssistantMessage, CLINotFoundError, ResultMessage, SystemMessage, TextBlock,
                              ToolUseBlock)
from sqlalchemy import select

from app.ai import claude_code as cc
from app.ai import gateway
from app.core.config import get_settings
from app.db.models import AuditLog, TradeOrder
from app.db.models_ai import AIUsage
from app.db.models_opportunity import AgentTask, Incident
from app.db.session import session_scope
from app.services import assistant, orchestrator
from app.services import trading as tr
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)


# ---- Tool policy -------------------------------------------------------------------------------------------------
@pytest.fixture
def pol(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("x = 1\n")
    return cc.ToolPolicy(tmp_path)


@pytest.mark.parametrize("tool,data,ok", [
    ("Read", {"file_path": "app/x.py"}, True),
    ("Edit", {"file_path": "app/x.py", "old_string": "1", "new_string": "2"}, True),
    ("Read", {"file_path": "/etc/passwd"}, False),
    ("Read", {"file_path": "../../outside.py"}, False),
    ("Read", {"file_path": ".env"}, False),
    ("Write", {"file_path": "config/prod.env"}, False),
    ("Read", {"file_path": "certs/server.key"}, False),
    ("Write", {"file_path": ".git/config"}, False),
    ("Glob", {"pattern": "/etc/*"}, False),
    ("Grep", {"pattern": "def", "path": "app"}, True),
    ("WebFetch", {"url": "https://example.com"}, False),
    ("Task", {"prompt": "x"}, False),
    ("mcp__x__y", {}, False),
])
def test_tool_policy_paths_secrets_and_tools(pol, tool, data, ok):
    assert pol.check(tool, data).allow is ok


@pytest.mark.parametrize("cmd,ok", [
    ("python -m pytest -x -q", True), ("pytest tests/test_x.py", True), ("ruff check app", True),
    ("git status", True), ("git diff HEAD", True), ("ls app", True), ("grep -rn def app", True),
    ("git push origin main", False), ("git commit -m x", False), ("git reset --hard", False),
    ("rm -rf app", False), ("curl https://example.com", False), ("pip install x", False),
    ("python -c 'import os'", False), ("python script.py", False), ("bash -c ls", False),
    ("ls; rm -rf /", False), ("cat app/x.py | sh", False), ("echo $(whoami)", False), ("cat > app/x.py", False),
    ("cat /etc/passwd", False), ("cat ../secrets.txt", False), ("cat .env", False),
    ("find . -name '*.py' -delete", False), ("find . -name '*.py'", True), ("sudo ls", False),
])
def test_shell_allow_list(pol, cmd, ok):
    assert pol.check("Bash", {"command": cmd}).allow is ok, cmd


def test_gates_by_path_deletion_and_content():
    files = [{"status": "M", "path": "migrations/versions/0099_x.py"}, {"status": "M", "path": "app/execution/manager.py"},
             {"status": "D", "path": "app/old.py"}, {"status": "M", "path": "app/risk/engine.py"},
             {"status": "M", "path": "app/core/security.py"}]
    assert cc.classify(files, "") == ["database_migration", "order_execution", "risk_configuration",
                                      "credentials_security", "destructive"]
    assert cc.classify([{"status": "M", "path": "app/ui.py"}], "state.active_mode = 'LIVE'") == ["live_authorisation"]
    assert cc.classify([{"status": "M", "path": "app/ui.py"}], "label = 'x'") == []
    assert cc.classify([{"status": "M", "path": "backend/app/execution/manager.py"},
                        {"status": "M", "path": "mt5-connector/connector/service.py"}], "") == ["order_execution"]


# ---- Workspace and runner ----------------------------------------------------------------------------------------
@pytest.fixture
def ws(keys, tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "app" / "risk").mkdir(parents=True)
    (src / "app" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (src / "app" / "__init__.py").write_text("")
    (src / "app" / "risk" / "__init__.py").write_text("")
    (src / "app" / "risk" / "limits.py").write_text("MAX_RISK = 1.0\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_calc.py").write_text("from app.calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    (src / ".env").write_text("RR_SECRET=nope\n")
    monkeypatch.setenv("RR_CLAUDE_CODE_WORKSPACE", str(tmp_path / "ccws"))
    monkeypatch.setenv("RR_CLAUDE_CODE_SOURCE", str(src))
    monkeypatch.setenv("RR_CLAUDE_CODE_EXTRA_SOURCES", str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("RR_ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    repo = cc.init_workspace()
    with session_scope() as db:
        c = cc.control(db)
        c.claude_code_enabled = True
        c.claude_code_test_command = f"{sys.executable} -m pytest -q -p no:cacheprovider tests"
    yield repo
    get_settings.cache_clear()


class FakeClaude:
    """Stands in for ClaudeSDKClient: same async interface, scripted SDK messages, edits files like Claude Code."""

    def __init__(self, options, script, on_message=None):
        self.options, self.script, self.on_message = options, script, on_message
        self.prompt, self.interrupted = None, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.prompt = prompt

    async def interrupt(self):
        self.interrupted = True

    async def receive_response(self):
        for i, item in enumerate(self.script):
            if callable(item):
                item = await item(self)
                if item is None:
                    continue
            if self.on_message:
                self.on_message(i)
            yield item


async def hook(fake, tool, data):
    """Run the engine's PreToolUse hook as the CLI would before a tool call."""
    cb = fake.options.hooks["PreToolUse"][0].hooks[0]
    return await cb({"tool_name": tool, "tool_input": data, "hook_event_name": "PreToolUse"}, "t1", None)


def edit(rel, text):
    async def step(fake):
        out = await hook(fake, "Edit", {"file_path": rel})
        assert out == {}, out
        (fake.options.cwd and __import__("pathlib").Path(fake.options.cwd) / rel).write_text(text)
        return AssistantMessage(content=[ToolUseBlock(id="e1", name="Edit", input={"file_path": rel})], model="m")
    return step


def denied(tool, data):
    async def step(fake):
        out = await hook(fake, tool, data)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        return None
    return step


def result(cost=0.12, sid="sess-1"):
    return ResultMessage(subtype="success", duration_ms=900, duration_api_ms=800, is_error=False, num_turns=3,
                         session_id=sid, total_cost_usd=cost, result="Fixed add(); the test passes.",
                         usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800})


def factory(script, captured, on_message=None):
    def make(options):
        f = FakeClaude(options, script, on_message)
        captured.append(f)
        return f
    return make


def submit(text="/code Fix the bug in app/calc.py add() and keep the test"):
    with session_scope() as db:
        return cc.submit(db, text, None).id


def task(tid):
    with session_scope() as db:
        t = db.get(AgentTask, tid)
        db.expunge(t)
        return t


def test_task_runs_in_an_isolated_worktree_tests_and_waits_for_review(ws):
    with session_scope() as db:
        before = (tr.get_state(db).active_mode, tr.get_state(db).auto_trading, db.query(TradeOrder).count())
    tid = submit()
    seen = []
    script = [SystemMessage(subtype="init", data={"session_id": "sess-1", "model": "m"}),
              AssistantMessage(content=[TextBlock(text="Looking at calc."),
                                        ToolUseBlock(id="r1", name="Read", input={"file_path": "backend/app/calc.py"})], model="m"),
              denied("Read", {"file_path": ".env"}), denied("Bash", {"command": "git push origin main"}),
              denied("Read", {"file_path": "/etc/passwd"}),
              edit("backend/app/calc.py", "def add(a, b):\n    return a + b\n"),
              result()]
    assert cc.run_task(tid, factory(script, seen)) == "AWAITING_APPROVAL"
    fake = seen[0]
    t = task(tid)
    wt = cc.worktree(t)
    # isolation: own worktree and branch; settings files ignored; web and sub-agents off; budget and turns bounded
    assert fake.options.cwd == str(wt) and str(wt) != str(ws) and t.branch == f"rr-claude/task-{tid}"
    assert fake.options.setting_sources == [] and fake.options.allowed_tools == []
    assert {"WebFetch", "WebSearch", "Task"} <= set(fake.options.disallowed_tools)
    assert fake.options.max_budget_usd == 2.0 and fake.options.max_turns == 40
    assert fake.options.env["ANTHROPIC_API_KEY"] == "sk-ant-test" and fake.prompt.startswith("Fix the bug")
    assert fake.options.model == get_settings().claude_model, "model comes from configuration"
    r = t.report
    assert r["tests"]["passed"] is True and r["tests"]["ran"]
    assert [f["path"] for f in r["files_changed"]] == ["backend/app/calc.py"] and "+    return a + b" in r["patch"]
    assert r["gates"] == [] and r["files_inspected"] == ["backend/app/calc.py"] and len(r["policy_denials"]) == 3
    assert t.session_id == "sess-1" and t.usage["cost_usd"] == 0.12 and t.usage["cache_read_tokens"] == 800
    phases = [e["phase"] for e in t.progress]
    assert phases[0] == "queued" and {"workspace", "claude", "tests", "review"} <= set(phases)
    assert ".env" not in subprocess.run(["git", "ls-files"], cwd=wt, capture_output=True, text=True).stdout
    # the base branch is untouched until approval; approval merges, never deploys
    assert "a - b" in (ws / "backend" / "app" / "calc.py").read_text()
    with session_scope() as db:
        t = db.get(AgentTask, tid)
        cc.approve(db, t, None, [])
        assert t.status == "MERGED" and "Not deployed" in t.report["deployment"]
    assert "a + b" in (ws / "backend" / "app" / "calc.py").read_text() and not wt.exists()
    with session_scope() as db:
        assert (tr.get_state(db).active_mode, tr.get_state(db).auto_trading, db.query(TradeOrder).count()) == before


def test_gated_patch_needs_each_acknowledgement_and_failing_tests_block_merge(ws):
    tid = submit("/code tighten the risk limit constant")
    script = [SystemMessage(subtype="init", data={"session_id": "s2"}), edit("backend/app/risk/limits.py", "MAX_RISK = 0.5\n"),
              edit("backend/app/calc.py", "def add(a, b):\n    return a + b\n"), result()]
    assert cc.run_task(tid, factory(script, [])) == "AWAITING_APPROVAL"
    with session_scope() as db:
        t = db.get(AgentTask, tid)
        assert t.report["gates"] == ["risk_configuration"]
        with pytest.raises(ValueError, match="acknowledge"):
            cc.approve(db, t, None, [])
        cc.approve(db, t, None, ["risk_configuration"])
        assert t.status == "MERGED"
    tid = submit("/code refactor add")
    script = [edit("backend/app/calc.py", "def add(a, b):\n    return a - b + 1\n"), result()]
    assert cc.run_task(tid, factory(script, [])) == "AWAITING_APPROVAL"
    with session_scope() as db:
        t = db.get(AgentTask, tid)
        assert t.report["tests"]["passed"] is False
        with pytest.raises(ValueError, match="test"):
            cc.approve(db, t, None, [])
        cc.cancel(db, t)                                         # reject: branch removed
        assert t.status == "REJECTED" and not cc.worktree(t).exists()


def test_cancel_interrupts_and_resume_continues_the_same_session(ws):
    tid = submit()

    def flag(i):
        if i == 1:
            with session_scope() as db:
                cc.cancel(db, db.get(AgentTask, tid))

    seen = []
    script = [SystemMessage(subtype="init", data={"session_id": "sess-9"}),
              AssistantMessage(content=[TextBlock(text="working")], model="m"), result()]
    assert cc.run_task(tid, factory(script, seen, flag)) == "CANCELLED"
    assert seen[0].interrupted is True
    with session_scope() as db:
        t = db.get(AgentTask, tid)
        assert t.session_id == "sess-9"
        cc.resume(db, t, "Now finish the fix.")
        assert t.status == "QUEUED"
    seen2 = []
    script = [edit("backend/app/calc.py", "def add(a, b):\n    return a + b\n"), result(sid="sess-9")]
    assert cc.run_task(tid, factory(script, seen2)) == "AWAITING_APPROVAL"
    assert seen2[0].options.resume == "sess-9" and seen2[0].prompt == "Now finish the fix."
    assert cc.worktree(task(tid)).exists()


def test_claude_unavailable_fails_the_task_and_the_engine_continues(ws):
    tid = submit()

    def broken(options):
        raise CLINotFoundError("Claude Code not found")
    assert cc.run_task(tid, broken) == "FAILED"
    t = task(tid)
    assert "not found" in t.progress[-1]["text"]
    with session_scope() as db:
        assert db.get(AgentTask, tid).session_id is None and tr.get_state(db).active_mode == "DEMO"
    # a restart while running leaves the task resumable once it has a session
    with session_scope() as db:
        t = db.get(AgentTask, tid)
        t.status, t.session_id = "RUNNING", "sess-x"
    assert cc.recover_interrupted() == 1 and task(tid).status == "INTERRUPTED"


def test_workspace_has_the_repository_layout_without_secrets(ws):
    assert (ws / "backend" / "app" / "calc.py").is_file() and not (ws / "backend" / ".env").exists()
    assert (ws / "mt5-connector" / "connector" / "service.py").is_file(), "the simulated connector the tests use"
    assert (ws / "mt5-connector" / "tests").is_dir()


def test_dedicated_test_database_is_created_and_the_trading_database_refused(ws, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    prod = make_url(get_settings().database_url)
    monkeypatch.setenv("RR_CLAUDE_CODE_TEST_DATABASE_URL", prod.render_as_string(hide_password=False))
    assert "refused" in cc.ensure_test_database()
    name = "rr_claude_test_pytest"
    monkeypatch.setenv("RR_CLAUDE_CODE_TEST_DATABASE_URL", prod.set(database=name).render_as_string(hide_password=False))
    eng = create_engine(prod.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        assert cc.ensure_test_database() is None and cc.ensure_test_database() is None     # idempotent
        with eng.connect() as c:
            assert c.scalar(text("select 1 from pg_database where datname = :d"), {"d": name})
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        eng.dispose()


def test_test_run_never_uses_the_production_database(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("RR_DATABASE_URL", "postgresql+psycopg://rr:x@prod:5432/rr_trading")
    monkeypatch.setenv("RR_CLAUDE_CODE_TEST_DATABASE_URL", "postgresql+psycopg://rr:x@prod:5432/rr_trading")
    out = cc.run_tests(tmp_path, f"{sys.executable} -c pass")
    assert out["passed"] is False and "production" in out["output"]


def test_status_and_disabled_claude_code(ws):
    with session_scope() as db:
        st = cc.status(db)
        assert st["ready"] and st["sdk"]["installed"] and st["workspace"]["ready"] and st["api_key_configured"]
        assert "sk-ant" not in str(st)
        cc.control(db).claude_code_enabled = False
    with session_scope() as db:
        with pytest.raises(ValueError, match="switched off"):
            cc.submit(db, "/code x", None)
        out = assistant.answer(db, "/code fix the bug in app/calc.py", user_id=None)
        assert out["intent"] == "claude_code" and "switched off" in out["answer"] and "task" not in out


# ---- Orchestrator --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("q,target,task_", [
    ("Fix the bug in the symbol mapper code and add a unit test", "claude_code", None),
    ("/code refactor the scanner", "claude_code", None),
    ("Write tests for the risk module", "claude_code", None),
    ("Give me a market summary", "anthropic_api", "market_summary"),
    ("Review my recent trades", "anthropic_api", "trade_review"),
    ("How is the win rate and drawdown this month?", "anthropic_api", "performance_analysis"),
    ("Which strategies should I research next?", "anthropic_api", "strategy_research"),
    ("Explain this error: MT5 disconnected", "anthropic_api", "error_explanation"),
    ("why was signal #3 not executed?", "runtime", None),
    ("Audit my entire trading engine.", "runtime", None),
    ("Find and repair all errors.", "runtime", None),
    ("what should I do next?", "runtime", None),
    ("/engine review my trades", "runtime", None),
])
def test_routing(q, target, task_):
    assert orchestrator.route(q) == (target, task_)


def test_anthropic_api_analysis_uses_the_route_and_engine_facts(keys):
    pid = provider("claude", kind="anthropic")
    route("trade_review", (pid, "configured-model"))
    fake = Scripted({"claude": [{"answer": "No closed trades in 30 days.", "next_steps": ["Run the DEMO test"]}]})
    with session_scope() as db:
        out = assistant.answer(db, "Review my recent trades", provider_factory=fake.factory)
    assert out["source"] == "ai" and out["analysis"] == "trade_review" and out["ai"]["model"] == "configured-model-2026"
    sent = fake.seen[0]
    assert sent["model"] == "configured-model" and "closed_trades_30d" in sent["user"]["facts"]
    with session_scope() as db:                                  # no provider for the task: engine facts only
        out = assistant.answer(db, "Give me a market summary")
        assert out["source"] == "engine" and "overview" in out["facts"]
        u = db.scalars(select(AIUsage)).all()
        assert [x.task for x in u] == ["trade_review"]


def test_budget_alert_raises_one_incident(keys):
    from app.db.models_ai import AIControl
    pid = provider("p", price=1000.0)
    route("market_summary", (pid, "m"))
    with session_scope() as db:
        db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                           daily_budget_usd=5.0, budget_alert_pct=40))
    for i in range(3):
        gateway.run("market_summary", "s", {"i": i}, orchestrator.ANSWER_SCHEMA, orchestrator.Answer,
                    provider_factory=Scripted({"p": [{"answer": "a", "next_steps": []}]}).factory, sleep=lambda s: None)
    b = gateway.budget_status()
    assert b["alert"] and not b["exhausted"]
    with session_scope() as db:
        inc = db.scalars(select(Incident).where(Incident.category == "ai_budget")).all()
        assert len(inc) == 1 and inc[0].occurrences >= 2


# ---- API -----------------------------------------------------------------------------------------------------------
def test_api_permissions_status_config_and_merge(client, ws):
    login(client, "trader")
    assert client.get("/api/claude-code/status").json()["enabled"] is True
    assert client.put("/api/claude-code/config", json={"enabled": False}, headers=h(client)).status_code == 403
    r = client.post("/api/assistant/ask", json={"question": "/code fix the bug in app/calc.py"}, headers=h(client))
    assert r.status_code == 403
    login(client, "admin")
    bad = client.put("/api/claude-code/config", json={"test_command": "rm -rf /"}, headers=h(client))
    assert bad.status_code == 422
    st = client.put("/api/claude-code/config", json={"max_budget_usd": 1.5, "max_turns": 20}, headers=h(client)).json()
    assert st["max_budget_usd"] == 1.5 and st["max_turns"] == 20 and "sk-ant" not in str(st)
    r = client.post("/api/assistant/ask", json={"question": "/code fix the bug in app/calc.py add()"},
                    headers=h(client)).json()
    assert r["route"] == "claude_code" and r["task"]["status"] == "QUEUED" and r["task"]["kind"] == "claude_code"
    tid = r["task"]["id"]
    script = [edit("backend/app/risk/limits.py", "MAX_RISK = 0.5\n"), edit("backend/app/calc.py", "def add(a, b):\n    return a + b\n"),
              result()]
    assert cc.run_task(tid, factory(script, [])) == "AWAITING_APPROVAL"
    t = client.get(f"/api/agent/tasks/{tid}").json()
    assert t["report"]["gates"] == ["risk_configuration"] and t["progress"] and t["usage"]["cost_usd"] == 0.12
    assert client.post(f"/api/agent/tasks/{tid}/approve", json={}, headers=h(client)).status_code == 409
    out = client.post(f"/api/agent/tasks/{tid}/approve", json={"acknowledged_gates": ["risk_configuration"]},
                      headers=h(client)).json()
    assert out["status"] == "MERGED"
    assert client.get("/api/agent/tasks?kind=claude_code").json()[0]["id"] == tid
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "CLAUDE_CODE_PATCH_MERGED"))
        assert db.scalar(select(AuditLog).where(AuditLog.action == "CLAUDE_CODE_CONFIG"))


def test_real_sdk_client_is_constructed_with_the_policy(ws):
    """The default factory builds the SDK's own ClaudeSDKClient (not connected here: no network in tests)."""
    from claude_agent_sdk import ClaudeSDKClient
    with session_scope() as db:
        t = db.get(AgentTask, submit())
        opts = cc._options(t, cc.control(db), ws, "k", cc.ToolPolicy(ws), [])
    client_ = cc._default_client(opts)
    assert isinstance(client_, ClaudeSDKClient) and client_.options.cwd == str(ws)
    out = asyncio.run(opts.can_use_tool("Read", {"file_path": "/etc/passwd"}, None))
    assert out.behavior == "deny"
