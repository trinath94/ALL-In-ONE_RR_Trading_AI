"""Static analysis of experimental strategy source (runs in the parent; never executes the code)."""
from __future__ import annotations

import ast
import hashlib
import re

ALLOWED_IMPORTS = {"math", "numpy", "pandas", "statistics"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                   "delattr", "input", "breakpoint", "exit", "quit", "memoryview", "help", "type", "object", "super"}
FORBIDDEN_MODULE_HINTS = {"os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib", "ctypes", "pickle",
                          "requests", "httpx", "urllib", "asyncio", "threading", "multiprocessing", "builtins",
                          "app", "sqlalchemy", "redis", "anthropic"}
MAX_SOURCE = 40_000
CODE_RE = re.compile(r"^exp_[a-z0-9_]{3,60}$")


def source_hash(src: str) -> str:
    return hashlib.sha256(src.encode()).hexdigest()


def validate(src: str) -> dict:
    """Returns {ok, errors, warnings, class_name, code, imports, complexity}."""
    errors, warnings = [], []
    if len(src) > MAX_SOURCE:
        return {"ok": False, "errors": [f"source exceeds {MAX_SOURCE} bytes"], "warnings": []}
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {"ok": False, "errors": [f"syntax error: {exc}"], "warnings": []}
    imports = set()
    classes = []
    branches = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                root = m.split(".")[0]
                imports.add(root)
                if root not in ALLOWED_IMPORTS:
                    errors.append(f"import of '{m}' is not allowed (allowed: {sorted(ALLOWED_IMPORTS)})")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(f"use of '{node.id}' is forbidden")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_MODULE_HINTS:
            errors.append(f"reference to '{node.id}' is forbidden")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr.startswith("_sa_")):
            errors.append(f"dunder/private attribute access '{node.attr}' is forbidden")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            errors.append("global/nonlocal statements are forbidden")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)):
            errors.append("async/generator constructs are not allowed")
        elif isinstance(node, ast.ClassDef):
            classes.append(node)
        elif isinstance(node, (ast.If, ast.For, ast.While, ast.IfExp, ast.BoolOp)):
            branches += 1
        if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value:
            warnings.append("unbounded 'while True' loop (CPU limit applies)")
    strat = [c for c in classes if any(isinstance(b, ast.Name) and b.id == "Strategy" for b in c.bases)]
    if len(strat) != 1:
        errors.append("source must define exactly one class deriving from Strategy")
        return {"ok": False, "errors": errors, "warnings": warnings, "imports": sorted(imports)}
    cls = strat[0]
    attrs = {}
    has_detect = False
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name) and isinstance(item.value, ast.Constant):
                    attrs[t.id] = item.value.value
                elif isinstance(t, ast.Tuple) and isinstance(item.value, ast.Tuple):
                    for n, v in zip(t.elts, item.value.elts):
                        if isinstance(n, ast.Name) and isinstance(v, ast.Constant):
                            attrs[n.id] = v.value
        if isinstance(item, ast.FunctionDef) and item.name == "detect":
            has_detect = True
    code = attrs.get("code")
    if not isinstance(code, str) or not CODE_RE.match(code):
        errors.append("class must declare code = 'exp_<snake_case>'")
    for req in ("name", "category"):
        if req not in attrs:
            errors.append(f"class must declare '{req}'")
    if not has_detect:
        errors.append("class must implement detect(self, ctx)")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "class_name": cls.name, "code": code,
            "name": attrs.get("name"), "category": attrs.get("category"), "imports": sorted(imports),
            "complexity": branches, "source_hash": source_hash(src)}
