"""Claude Code in the engine: development tasks through the official Claude Agent SDK (Python ``claude-agent-sdk``).

A request in the Engine Assistant such as "fix the bug in the symbol mapper and add a test" becomes an ``AgentTask``
of kind ``claude_code``. The ``claude-code`` role runs it:

1. **Workspace.** A git worktree on its own branch (``rr-claude/task-<id>``) of a *copy* of the engine source in the
   Claude Code workspace (``RR_CLAUDE_CODE_WORKSPACE``). Never the running engine's files, never the whole server.
2. **Claude Code** runs in that worktree with the Claude Code harness. Every tool call passes ``ToolPolicy`` (a
   PreToolUse hook and the permission callback): file paths must resolve inside the worktree, secrets (.env, keys,
   certificates, credential files) cannot be read or written, ``.git`` cannot be written, the web and sub-agents are
   off, and the shell only runs listed read-only and test commands (no pipes, redirection, substitution or chaining).
   Filesystem settings in the copy (``.claude/``) are not loaded, so a file in the repository cannot widen this.
3. **Progress** (phase, text, tools, files inspected and changed) is stored on the task as it happens; the chat polls
   it. Cancellation interrupts the session; a cancelled, failed or interrupted task resumes the same SDK session.
4. **Tests.** The engine itself runs the configured test command in the worktree after Claude finishes (Claude's own
   claim that tests passed is never trusted).
5. **Review.** The change is committed on the task branch; the patch, the changed files and the **approval gates** it
   touches (database migration, order execution, risk configuration, LIVE authorisation, credentials and security,
   destructive change) are shown. Nothing is merged without passing tests and an administrator who acknowledges each
   gate. Merging puts the change on the workspace's base branch only: there is no deployment path from here into the
   running engine (a change reaches it through the release pipeline, as a new installer).

The engine never depends on this: trading, risk, execution and learning run without Claude Code or any AI.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models_opportunity import AgentTask
from app.db.session import session_scope

log = logging.getLogger("ai.claude_code")
UTC = timezone.utc
KIND = "claude_code"
BRANCH_PREFIX = "rr-claude/task-"
MAX_PROGRESS = 400
MAX_PATCH = 200_000

# ---- What Claude Code may do ------------------------------------------------------------------------------------
TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]          # the built-in tools offered at all
PATH_KEYS = ("file_path", "path", "notebook_path")
WRITE_TOOLS = {"Edit", "Write"}
SECRET_PATTERNS = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                   "*secret*", "*credential*", "secrets.*", "*.kdbx", "*vault*", "dump.rdb")
SHELL_META = re.compile(r"[;&|<>`$\n\\]|\(|\)")
GIT_READ = {"status", "diff", "log", "show", "ls-files", "grep", "blame", "rev-parse", "branch"}
SIMPLE_READ = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo", "true", "sort", "uniq", "diff"}
PYTHON_MODULES = {"pytest", "ruff", "compileall", "py_compile", "mypy"}
FIND_DENY = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}

# ---- Approval gates (by changed path and by added content) ------------------------------------------------------
GATES = {
    "database_migration": "Database migration: applies to the production database only through the release pipeline",
    "order_execution": "Changes code that places, modifies or closes orders (execution, bridge, connectors)",
    "risk_configuration": "Changes risk limits, sizing or stop-loss handling",
    "live_authorisation": "Touches LIVE trading authorisation or the trading mode",
    "credentials_security": "Touches credentials, encryption, authentication or permissions",
    "destructive": "Deletes files",
}
GATE_PATHS = [
    ("database_migration", ("migrations/*", "*/migrations/*", "app/db/models*.py")),
    ("order_execution", ("app/execution/*", "*/execution/*", "mt5-connector/*", "windows-connector/*",
                         "app/services/trading.py", "app/workers/main.py", "app/workers/roles.py")),
    ("risk_configuration", ("app/risk/*", "*/risk/*", "app/portfolio/*")),
    ("credentials_security", ("app/core/security.py", "app/core/config.py", "app/api/deps.py", "app/api/routes/auth*",
                              "app/api/routes/mt5_agent.py", "app/services/secrets*")),
]
GATE_WORDS = [
    ("live_authorisation", re.compile(r"\bactive_mode\b|\bLIVE\b|live_enabled|enable_live|live_lock", re.I)),
    ("risk_configuration", re.compile(r"max_risk|risk_per_trade|daily_loss|max_drawdown|stop_loss|\bsl\b|lot_size|"
                                      r"max_lots|kill_switch|risk_lock", re.I)),
    ("order_execution", re.compile(r"order_send|place_order|close_position|modify_position|submit_order", re.I)),
]


def _now() -> datetime:
    return datetime.now(UTC)


# ---- Policy -----------------------------------------------------------------------------------------------------
@dataclass
class Decision:
    allow: bool
    reason: str = ""


class ToolPolicy:
    """Decides every tool call Claude Code makes in one task's worktree."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _inside(self, raw: str) -> tuple[Path | None, str]:
        if not raw:
            return self.root, ""
        p = Path(raw)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if p != self.root and self.root not in p.parents:
            return None, f"{raw} is outside the task workspace"
        return p, ""

    def _secret(self, p: Path) -> bool:
        rel = p.relative_to(self.root).as_posix() if p != self.root else ""
        names = [part.lower() for part in PurePosixPath(rel).parts]
        return any(fnmatch.fnmatch(n, pat) for n in names for pat in SECRET_PATTERNS)

    def check(self, tool: str, data: dict) -> Decision:
        if tool not in TOOLS:
            return Decision(False, f"the {tool} tool is not available in the engine workspace")
        if tool == "Bash":
            return self.check_command(str(data.get("command") or ""))
        if tool == "Glob" and (str(data.get("pattern", "")).startswith("/") or ".." in str(data.get("pattern", ""))):
            return Decision(False, "glob patterns must be relative to the task workspace")
        for key in PATH_KEYS:
            if key in data and data[key] is not None:
                p, why = self._inside(str(data[key]))
                if p is None:
                    return Decision(False, why)
                if self._secret(p):
                    return Decision(False, f"{data[key]} looks like a secret; secrets are never read or written")
                if tool in WRITE_TOOLS and ".git" in p.relative_to(self.root).parts:
                    return Decision(False, "the git metadata is managed by the engine")
        return Decision(True)

    def check_command(self, command: str) -> Decision:
        if not command.strip():
            return Decision(False, "empty command")
        if SHELL_META.search(command):
            return Decision(False, "pipes, redirection, substitution and chained commands are not allowed")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return Decision(False, f"unparsable command: {exc}")
        for a in argv[1:]:
            if a.startswith("-") and "=" in a:
                a = a.split("=", 1)[1]
            if a.startswith("/") or a.startswith("~") or ".." in PurePosixPath(a).parts:
                p, why = self._inside(os.path.expanduser(a))
                if p is None:
                    return Decision(False, why)
            if not a.startswith("-") and self._looks_secret(a):
                return Decision(False, f"{a} looks like a secret")
        prog = argv[0]
        if prog in ("python", "python3") and len(argv) >= 3 and argv[1] == "-m" and argv[2] in PYTHON_MODULES:
            return Decision(True)
        if prog in ("pytest", "ruff", "mypy"):
            return Decision(True)
        if prog == "git":
            sub = next((a for a in argv[1:] if not a.startswith("-")), "")
            return Decision(sub in GIT_READ, "" if sub in GIT_READ else f"git {sub} is not allowed (read-only git only)")
        if prog == "find" and FIND_DENY & set(argv):
            return Decision(False, "find with actions is not allowed")
        if prog in SIMPLE_READ:
            return Decision(True)
        return Decision(False, f"{prog} is not in the engine's command allow-list")

    def _looks_secret(self, arg: str) -> bool:
        return any(fnmatch.fnmatch(part.lower(), pat) for part in PurePosixPath(arg).parts for pat in SECRET_PATTERNS)


def classify(files: list[dict], added_text: str) -> list[str]:
    """Approval gates a patch touches: by changed path, deleted files, and words in the added lines. Paths are relative
    to the workspace root (backend/..., mt5-connector/...)."""
    gates = set()
    for f in files:
        path = f["path"].removeprefix("backend/")
        if f["status"].startswith("D"):
            gates.add("destructive")
        for gate, pats in GATE_PATHS:
            if any(fnmatch.fnmatch(path, p) for p in pats):
                gates.add(gate)
    for gate, rx in GATE_WORDS:
        if rx.search(added_text):
            gates.add(gate)
    return [g for g in GATES if g in gates]


# ---- Configuration and status -----------------------------------------------------------------------------------
def control(db):
    from app.db.models_ai import AIControl
    c = db.get(AIControl, 1)
    if c is None:
        c = AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="")
        db.add(c)
        db.flush()
    return c


def model_for(c) -> str:
    return (c.claude_code_model or "").strip() or get_settings().claude_model


def api_key(db) -> str | None:
    """The Anthropic key for the Claude Code process: the engine's env key, else an enabled Anthropic provider's
    stored key. Backend only: it is passed in the subprocess environment and never returned by any API."""
    s = get_settings()
    if s.anthropic_api_key:
        return s.anthropic_api_key
    from app.ai.gateway import _decrypt
    from app.db.models_ai import AIProvider
    for p in db.scalars(select(AIProvider).where(AIProvider.kind == "anthropic", AIProvider.enabled.is_(True))
                        .order_by(AIProvider.id)):
        try:
            k = _decrypt(p.api_key_enc)
        except Exception:  # noqa: BLE001
            continue
        if k:
            return k
    return None


def workspace() -> Path | None:
    w = get_settings().claude_code_workspace
    return Path(w) if w else None


def repo_dir() -> Path | None:
    w = workspace()
    return w / "repo" if w else None


def sdk_info() -> dict:
    try:
        import claude_agent_sdk
        from claude_agent_sdk._cli_version import __cli_version__
        return {"installed": True, "version": getattr(claude_agent_sdk, "__version__", ""), "cli": __cli_version__}
    except Exception as exc:  # noqa: BLE001
        return {"installed": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _git(args: list[str], cwd: Path, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "RR Claude Code", "GIT_AUTHOR_EMAIL": "claude-code@rr-trading-one.local",
           "GIT_COMMITTER_NAME": "RR Claude Code", "GIT_COMMITTER_EMAIL": "claude-code@rr-trading-one.local"}
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def base_branch(repo: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()


def status(db) -> dict:
    c = control(db)
    repo = repo_dir()
    ws = {"configured": repo is not None, "ready": False}
    if repo is not None and (repo / ".git").exists() and shutil.which("git"):
        try:
            ws.update(ready=True, base_branch=base_branch(repo),
                      head=_git(["rev-parse", "--short", "HEAD"], repo).stdout.strip())
        except Exception as exc:  # noqa: BLE001
            ws["error"] = str(exc)[:200]
    counts = {}
    for st in ("QUEUED", "RUNNING", "AWAITING_APPROVAL"):
        counts[st.lower()] = db.query(AgentTask).filter(AgentTask.kind == KIND, AgentTask.status == st).count()
    sdk = sdk_info()
    key = bool(api_key(db))
    ready = bool(c.claude_code_enabled and sdk["installed"] and ws["ready"] and key)
    why = [m for ok, m in ((c.claude_code_enabled, "Claude Code is switched off"),
                           (sdk["installed"], "the Claude Agent SDK is not installed in the engine"),
                           (ws["configured"], "no Claude Code workspace is configured (RR_CLAUDE_CODE_WORKSPACE)"),
                           (ws["ready"] or not ws["configured"], "the workspace has no git copy of the engine yet "
                                                                 "(the claude-code service creates it at start)"),
                           (key, "no Anthropic API key (AI Studio provider or RR_ANTHROPIC_API_KEY)")) if not ok]
    return {"enabled": c.claude_code_enabled, "ready": ready, "not_ready": why, "sdk": sdk, "workspace": ws,
            "api_key_configured": key, "model": model_for(c), "model_setting": c.claude_code_model,
            "max_budget_usd": c.claude_code_max_budget_usd, "max_turns": c.claude_code_max_turns,
            "test_command": c.claude_code_test_command, "tasks": counts, "gates": GATES,
            "deployment": "No deployment path: an approved patch is merged into the workspace's base branch only."}


def init_workspace(source: Path | None = None) -> Path:
    """Create repo/ as a git copy of the engine source in the repository layout (first start of the claude-code
    service): backend/ (this engine) and mt5-connector/ (the simulated connector the engine's tests run against).
    Idempotent."""
    repo = repo_dir()
    if repo is None:
        raise RuntimeError("RR_CLAUDE_CODE_WORKSPACE is not set")
    if (repo / ".git").exists():
        return repo
    s = get_settings()
    src = Path(source or s.claude_code_source or Path(__file__).resolve().parents[2])
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*", "dump.rdb", ".pytest_cache", "*.key",
                                    "*.pem", "node_modules", ".venv", "venv*")
    shutil.copytree(src, repo / "backend", ignore=ignore, dirs_exist_ok=True)
    for base in (Path(s.claude_code_extra_sources or "/nonexistent"), src.parent):
        conn = base / "mt5-connector"
        if (conn / "connector").is_dir():
            for part in ("connector", "tests"):
                if (conn / part).is_dir():
                    shutil.copytree(conn / part, repo / "mt5-connector" / part, ignore=ignore, dirs_exist_ok=True)
            if (conn / "windows" / "gen_cert.py").is_file():                 # test certificates for the TLS tests
                (repo / "mt5-connector" / "windows").mkdir(parents=True, exist_ok=True)
                shutil.copy2(conn / "windows" / "gen_cert.py", repo / "mt5-connector" / "windows" / "gen_cert.py")
            break
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.env\n.env.*\n")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "Engine source baseline"], repo)
    (repo.parent / "tasks").mkdir(exist_ok=True)
    return repo


def worktree(task: AgentTask) -> Path:
    return repo_dir().parent / "tasks" / f"task-{task.id}"


def prepare_worktree(task: AgentTask) -> Path:
    repo = repo_dir()
    wt = worktree(task)
    if wt.exists():
        return wt                                              # resume: same worktree, same branch
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = f"{BRANCH_PREFIX}{task.id}"
    _git(["worktree", "add", "-q", "-b", branch, str(wt), base_branch(repo)], repo)
    task.branch = branch
    return wt


def remove_worktree(task: AgentTask) -> None:
    repo = repo_dir()
    if repo is None:
        return
    wt = worktree(task)
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], repo, check=False)
    if task.branch:
        _git(["branch", "-D", task.branch], repo, check=False)


# ---- Task lifecycle (called from the chat / API) ----------------------------------------------------------------
CODE_RE = re.compile(r"\b(code|source|bug|patch|refactor|implement|unit tests?|write (a )?tests?|add (a )?tests?|"
                     r"migration|architecture|function|module|class|endpoint|exception|stack ?trace|traceback|"
                     r"compile|lint|type ?check|repository|repo|indicator (library|engine)|strategy engine)\b", re.I)


def is_code_request(text: str) -> bool:
    return bool(CODE_RE.search(text or "")) or (text or "").strip().lower().startswith("/code")


def submit(db, request: str, user_id: int | None) -> AgentTask:
    st = status(db)
    if not st["ready"]:
        raise ValueError("Claude Code is not available: " + "; ".join(st["not_ready"]))
    text = re.sub(r"^/code\s*", "", request.strip(), flags=re.I)
    t = AgentTask(command=text[:4000], workflow="claude_code", kind=KIND, status="QUEUED", findings=[], plan=[],
                  report={"title": "Claude Code task", "model": st["model"]}, progress=[], usage={},
                  created_by=user_id)
    db.add(t)
    db.flush()
    add_progress(t, "queued", "info", "Queued for the claude-code service.")
    return t


def cancel(db, task: AgentTask) -> AgentTask:
    if task.status == "QUEUED":
        task.status, task.finished_at = "CANCELLED", _now()
    elif task.status == "RUNNING":
        task.cancel_requested = True
    elif task.status == "AWAITING_APPROVAL":
        remove_worktree(task)
        task.status, task.finished_at = "REJECTED", _now()
        add_progress(task, "review", "info", "Patch rejected; branch removed.")
    return task


def resume(db, task: AgentTask, instruction: str = "") -> AgentTask:
    if task.status not in ("CANCELLED", "FAILED", "INTERRUPTED") or not task.session_id:
        raise ValueError(f"only a cancelled, failed or interrupted Claude Code task with a session can resume "
                         f"(this one is {task.status})")
    task.status, task.cancel_requested, task.finished_at = "QUEUED", False, None
    task.report = {**(task.report or {}), "resume_instruction": instruction[:2000] or None}
    add_progress(task, "queued", "info", "Resume queued (same Claude Code session).")
    return task


def approve(db, task: AgentTask, user_id: int | None, acknowledged: list[str]) -> AgentTask:
    """Merge the reviewed patch into the workspace's base branch. Requires passing tests and an acknowledgement of
    every approval gate the patch touches. Never deploys."""
    if task.status != "AWAITING_APPROVAL":
        raise ValueError(f"the task is {task.status}")
    rep = task.report or {}
    if not (rep.get("tests") or {}).get("passed"):
        raise ValueError("the engine's test run did not pass; the patch cannot be merged")
    missing = [g for g in rep.get("gates", []) if g not in set(acknowledged or [])]
    if missing:
        raise ValueError("acknowledge each approval gate first: " + ", ".join(missing))
    repo = repo_dir()
    base = base_branch(repo)
    _git(["merge", "--no-ff", "-q", "-m", f"Merge Claude Code task #{task.id} (approved)", task.branch], repo)
    head = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
    remove_worktree(task)
    task.status, task.approved_by, task.finished_at = "MERGED", user_id, _now()
    task.report = {**rep, "merged": {"into": base, "commit": head, "at": _now().isoformat(),
                                     "acknowledged_gates": list(acknowledged or [])},
                   "deployment": "Not deployed. The change is on the workspace's base branch; it reaches the running "
                                 "engine only through the release pipeline (tests, build, new installer)."}
    add_progress(task, "review", "info", f"Merged into {base} at {head}. Not deployed.")
    return task


def add_progress(task: AgentTask, phase: str, kind: str, text: str, file: str | None = None) -> None:
    ev = {"at": _now().isoformat(), "phase": phase, "kind": kind, "text": text[:600]}
    if file:
        ev["file"] = file
    task.progress = ((task.progress or []) + [ev])[-MAX_PROGRESS:]


def recover_interrupted() -> int:
    """At service start: tasks that were RUNNING when the process stopped become INTERRUPTED (resumable)."""
    with session_scope() as db:
        rows = db.scalars(select(AgentTask).where(AgentTask.kind == KIND, AgentTask.status == "RUNNING")).all()
        for t in rows:
            t.status, t.finished_at = "INTERRUPTED", _now()
            add_progress(t, "claude", "error", "The claude-code service stopped during this task; it can be resumed.")
        return len(rows)


# ---- Runner (claude-code role) ----------------------------------------------------------------------------------
SYSTEM_APPEND = """You are working inside RR Trading One's isolated development workspace: a git worktree with a copy
of the trading engine's source, on its own branch. Rules that the engine enforces and you must respect:
- Work only inside the current directory. Never read or write secrets, credentials or .env files.
- Never enable LIVE trading, raise risk limits, remove stop-loss requirements, bypass account permissions or change
  broker credentials. Never fabricate backtest or test results, and never claim profitability.
- Do not commit, merge or push: the engine commits your changes, runs the tests itself and shows the patch to an
  administrator, who decides. Database migrations must be additive and reversible.
- Keep the existing 215 strategies, their parameters and the existing risk controls unchanged unless the request is
  explicitly about them; prefer the smallest change that solves the request and add or update tests for it.
- Finish with a short report: what you found, what you changed and why, what you tested, and anything left open."""


class Cancelled(Exception):
    pass


def _record_message(task: AgentTask, msg, inspected: set, changed: set) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock
    if isinstance(msg, SystemMessage):
        if msg.subtype == "init" and msg.data.get("session_id"):
            task.session_id = str(msg.data["session_id"])[:64]
            add_progress(task, "claude", "info", f"Claude Code session started ({msg.data.get('model', '')}).")
    elif isinstance(msg, AssistantMessage):
        for b in msg.content:
            if isinstance(b, TextBlock) and b.text.strip():
                add_progress(task, "claude", "text", b.text.strip())
            elif isinstance(b, ToolUseBlock):
                f = next((str(b.input[k]) for k in PATH_KEYS if b.input.get(k)), None)
                if b.name in ("Read", "Glob", "Grep") and f:
                    inspected.add(f)
                if b.name in WRITE_TOOLS and f:
                    changed.add(f)
                detail = b.input.get("command") if b.name == "Bash" else (f or b.input.get("pattern") or "")
                add_progress(task, "claude", "tool", f"{b.name} {str(detail)[:200]}".strip(), file=f)
    elif isinstance(msg, ResultMessage):
        task.session_id = (msg.session_id or task.session_id or "")[:64] or None
        u = msg.usage or {}
        prev = task.usage or {}
        task.usage = {"cost_usd": round((prev.get("cost_usd") or 0) + (msg.total_cost_usd or 0), 6),
                      "input_tokens": (prev.get("input_tokens") or 0) + int(u.get("input_tokens") or 0),
                      "output_tokens": (prev.get("output_tokens") or 0) + int(u.get("output_tokens") or 0),
                      "cache_read_tokens": (prev.get("cache_read_tokens") or 0)
                      + int(u.get("cache_read_input_tokens") or 0),
                      "cache_write_tokens": (prev.get("cache_write_tokens") or 0)
                      + int(u.get("cache_creation_input_tokens") or 0),
                      "turns": (prev.get("turns") or 0) + int(msg.num_turns or 0),
                      "duration_ms": (prev.get("duration_ms") or 0) + int(msg.duration_ms or 0)}
        task.report = {**(task.report or {}), "claude_result": (msg.result or "")[:20000], "claude_error": msg.is_error,
                       "claude_subtype": msg.subtype, "stop": msg.terminal_reason or msg.stop_reason}


def _options(task: AgentTask, c, cwd: Path, key: str, policy: ToolPolicy, denials: list):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, PermissionResultAllow, PermissionResultDeny

    async def pre_tool(inp, tool_use_id, ctx):
        d = policy.check(inp.get("tool_name", ""), inp.get("tool_input") or {})
        if d.allow:
            return {}
        denials.append(f"{inp.get('tool_name')}: {d.reason}")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": d.reason}}

    async def can_use(tool, data, ctx):
        d = policy.check(tool, data)
        return PermissionResultAllow() if d.allow else PermissionResultDeny(message=d.reason)

    env = {"ANTHROPIC_API_KEY": key, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    return ClaudeAgentOptions(
        cwd=str(cwd), tools=list(TOOLS), allowed_tools=[], disallowed_tools=["WebFetch", "WebSearch", "Task", "Agent"],
        permission_mode="default", can_use_tool=can_use, hooks={"PreToolUse": [HookMatcher(hooks=[pre_tool])]},
        setting_sources=[], system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_APPEND},
        model=model_for(c), max_turns=int(c.claude_code_max_turns), max_budget_usd=float(c.claude_code_max_budget_usd),
        resume=task.session_id or None, env=env, stderr=lambda line: log.debug("claude-code: %s", line[:300]))


def _default_client(options):
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options=options)


async def _drive(task_id: int, prompt: str, options, client_factory) -> None:
    inspected: set = set()
    changed: set = set()
    async with client_factory(options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            with session_scope() as db:
                t = db.get(AgentTask, task_id)
                _record_message(t, msg, inspected, changed)
                rep = t.report or {}
                t.report = {**rep, "files_inspected": sorted(set(rep.get("files_inspected", [])) | inspected),
                            "files_touched": sorted(set(rep.get("files_touched", [])) | changed)}
                cancel_now = t.cancel_requested
            if cancel_now:
                await client.interrupt()
                raise Cancelled()


def test_dir(wt: Path) -> Path:
    return wt / "backend" if (wt / "backend").is_dir() else wt


def ensure_test_database() -> str | None:
    """Create the dedicated test database (RR_CLAUDE_CODE_TEST_DATABASE_URL) if it does not exist. Refuses when it is
    the trading database. Returns a problem, or None."""
    url = os.environ.get("RR_CLAUDE_CODE_TEST_DATABASE_URL")
    if not url:
        return "no test database configured (RR_CLAUDE_CODE_TEST_DATABASE_URL)"
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    test, prod = make_url(url), make_url(get_settings().database_url)
    if (test.host, test.port, test.database) == (prod.host, prod.port, prod.database):
        return "the test database is the trading database: refused"
    eng = create_engine(prod.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            if not c.scalar(text("select 1 from pg_database where datname = :d"), {"d": test.database}):
                if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", test.database or ""):
                    return f"invalid test database name {test.database!r}"
                c.execute(text(f'CREATE DATABASE "{test.database}"'))
    finally:
        eng.dispose()
    return None


def run_tests(cwd: Path, command: str) -> dict:
    argv = shlex.split(command or "")
    if not argv:
        return {"ran": False, "passed": False, "output": "no test command configured"}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "PYTHONDONTWRITEBYTECODE": "1",
           "RR_ENVIRONMENT": "test"}
    # The engine's test fixtures truncate tables and flush Redis at RR_DATABASE_URL / RR_REDIS_URL: the production
    # values are never passed. Only dedicated test databases (RR_CLAUDE_CODE_TEST_*) are; without them the database
    # tests fail and the patch cannot be merged (honest, never silently skipped).
    for src, dst in (("RR_CLAUDE_CODE_TEST_DATABASE_URL", "RR_DATABASE_URL"),
                     ("RR_CLAUDE_CODE_TEST_REDIS_URL", "RR_REDIS_URL")):
        if os.environ.get(src):
            env[dst] = os.environ[src]
    if env.get("RR_DATABASE_URL") and env["RR_DATABASE_URL"] == os.environ.get("RR_DATABASE_URL"):
        return {"ran": False, "passed": False, "command": command,
                "output": "refused: the test database is the production database"}
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env,
                           timeout=get_settings().claude_code_test_timeout_s)
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": False, "exit_code": None, "command": command, "output": "timed out"}
    except FileNotFoundError as exc:
        return {"ran": False, "passed": False, "command": command, "output": str(exc)}
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    return {"ran": True, "passed": r.returncode == 0, "exit_code": r.returncode, "command": command,
            "output": out[-6000:]}


def review(cwd: Path) -> dict:
    """Commit the worktree's changes on the task branch and describe the patch."""
    _git(["add", "-A"], cwd)
    if _git(["diff", "--cached", "--quiet"], cwd, check=False).returncode == 0:
        return {"files": [], "patch": "", "gates": [], "stat": "no changes"}
    _git(["commit", "-q", "-m", "Claude Code changes (pending review)"], cwd)
    files = []
    for line in _git(["diff", "--name-status", "HEAD~1", "HEAD"], cwd).stdout.splitlines():
        parts = line.split("\t")
        files.append({"status": parts[0], "path": parts[-1]})
    patch = _git(["diff", "HEAD~1", "HEAD"], cwd).stdout
    added = "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    return {"files": files, "patch": patch[:MAX_PATCH], "patch_truncated": len(patch) > MAX_PATCH,
            "stat": _git(["diff", "--shortstat", "HEAD~1", "HEAD"], cwd).stdout.strip(),
            "gates": classify(files, added), "commit": _git(["rev-parse", "--short", "HEAD"], cwd).stdout.strip()}


def run_task(task_id: int, client_factory=None) -> str:
    """Run one queued Claude Code task to its next stop (AWAITING_APPROVAL, DONE, CANCELLED or FAILED)."""
    client_factory = client_factory or _default_client
    with session_scope() as db:
        t = db.get(AgentTask, task_id)
        if t is None or t.status != "QUEUED":
            return t.status if t else "MISSING"
        c = control(db)
        key = api_key(db)
        resuming = bool(t.session_id)
        t.status, t.started_at, t.cancel_requested = "RUNNING", t.started_at or _now(), False
        try:
            wt = prepare_worktree(t)
        except Exception as exc:  # noqa: BLE001
            t.status, t.finished_at = "FAILED", _now()
            add_progress(t, "workspace", "error", f"Workspace could not be prepared: {exc}")
            return t.status
        add_progress(t, "workspace", "info", f"Isolated worktree on branch {t.branch or BRANCH_PREFIX + str(t.id)}.")
        if not key:
            t.status, t.finished_at = "FAILED", _now()
            add_progress(t, "claude", "error", "No Anthropic API key is configured.")
            return t.status
        prompt = ((t.report or {}).get("resume_instruction") or "Continue the task.") if resuming else t.command
        test_cmd = c.claude_code_test_command
        denials: list = []
        options = _options(t, c, wt, key, ToolPolicy(wt), denials)
        add_progress(t, "claude", "info", "Resuming the Claude Code session." if resuming else "Claude Code started.")
    outcome, error = "ok", None
    try:
        asyncio.run(_drive(task_id, prompt, options, client_factory))
    except Cancelled:
        outcome = "cancelled"
    except Exception as exc:  # noqa: BLE001 - Claude being unavailable never affects the engine
        outcome, error = "error", f"{type(exc).__name__}: {str(exc)[:400]}"
    with session_scope() as db:
        t = db.get(AgentTask, task_id)
        rep = dict(t.report or {})
        rep["policy_denials"] = (rep.get("policy_denials") or []) + denials
        t.report = rep
        if outcome == "cancelled":
            t.status, t.finished_at = "CANCELLED", _now()
            add_progress(t, "claude", "info", "Cancelled; the session can be resumed.")
            return t.status
        if outcome == "error":
            t.status, t.finished_at = "FAILED", _now()
            add_progress(t, "claude", "error", f"Claude Code failed: {error}")
            return t.status
        add_progress(t, "tests", "info", f"Running the engine's test command: {test_cmd}")
    tests = run_tests(test_dir(wt), test_cmd)
    try:
        rv = review(wt)
        rv_err = None
    except Exception as exc:  # noqa: BLE001
        rv, rv_err = {"files": [], "patch": "", "gates": []}, str(exc)[:300]
    with session_scope() as db:
        t = db.get(AgentTask, task_id)
        add_progress(t, "tests", "info" if tests["passed"] else "error",
                     f"Tests {'passed' if tests['passed'] else 'FAILED'}" + (f" (exit {tests.get('exit_code')})"
                                                                             if tests.get("ran") else ": not run"))
        t.report = {**(t.report or {}), "tests": tests, "files_changed": rv["files"], "patch": rv["patch"],
                    "patch_truncated": rv.get("patch_truncated", False), "stat": rv.get("stat"),
                    "gates": rv["gates"], "gate_labels": {g: GATES[g] for g in rv["gates"]},
                    "commit": rv.get("commit"), "review_error": rv_err}
        if rv["files"]:
            t.status = "AWAITING_APPROVAL"
            add_progress(t, "review", "approval", f"Patch ready for review: {rv.get('stat')}. "
                         + (f"Approval gates: {', '.join(rv['gates'])}." if rv["gates"] else "No approval gate.")
                         + ("" if tests["passed"] else " Tests failed: it cannot be merged."))
        else:
            t.status, t.finished_at = "DONE", _now()
            remove_worktree(t)
            add_progress(t, "review", "info", "No files changed. Done.")
        return t.status


def next_queued() -> int | None:
    with session_scope() as db:
        row = db.execute(select(AgentTask.id).where(AgentTask.kind == KIND, AgentTask.status == "QUEUED")
                         .order_by(AgentTask.id).limit(1).with_for_update(skip_locked=True)).first()
        return row[0] if row else None


def describe(task: AgentTask) -> str:
    r = task.report or {}
    u = task.usage or {}
    lines = [f"Claude Code — task #{task.id} ({task.status.replace('_', ' ').lower()})"
             + (f", branch {task.branch}" if task.branch else "")]
    last = (task.progress or [])[-1:] or [{}]
    if task.status in ("QUEUED", "RUNNING"):
        lines.append(f"Phase: {last[0].get('phase', 'queued')} — {last[0].get('text', '')}")
    if r.get("files_inspected"):
        lines.append(f"Files inspected: {len(r['files_inspected'])}")
    if r.get("files_changed"):
        lines.append("Changed: " + ", ".join(f"{f['status']} {f['path']}" for f in r["files_changed"][:20]))
    if r.get("tests"):
        lines.append("Tests: " + ("passed" if r["tests"]["passed"] else "FAILED") + f" ({r['tests'].get('command')})")
    if r.get("gates"):
        lines.append("Approval gates: " + "; ".join(GATES[g] for g in r["gates"]))
    if r.get("claude_result"):
        lines.append("Claude's report: " + r["claude_result"][:1500])
    if r.get("policy_denials"):
        lines.append(f"Blocked by the workspace policy: {len(r['policy_denials'])} tool call(s).")
    if u:
        lines.append(f"Usage: {u.get('cost_usd', 0)} USD, {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} "
                     f"out tokens, {u.get('turns', 0)} turns.")
    if task.status == "AWAITING_APPROVAL":
        lines.append("Review the patch and approve to merge it into the workspace (never deployed from here), or "
                     "reject it.")
    if r.get("deployment") and task.status == "MERGED":
        lines.append(r["deployment"])
    return "\n".join(lines)
