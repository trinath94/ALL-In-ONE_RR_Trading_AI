"""Parent-side sandbox launcher: static validation first, then a resource-limited subprocess."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pandas as pd

from app.strategies.experimental.validator import validate as static_validate

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class SandboxError(Exception):
    pass


def run_job(task: str, source: str, bars: pd.DataFrame | None = None, timeout: int = 300, **extra) -> dict:
    check = static_validate(source)
    if not check["ok"]:
        raise SandboxError("static validation failed: " + "; ".join(check["errors"]))
    with tempfile.TemporaryDirectory(prefix="rr-sandbox-") as tmp:
        job = {"task": task, "source": source, **extra}
        if bars is not None:
            path = os.path.join(tmp, "bars.pkl")
            bars.to_pickle(path)
            job["bars"] = path
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": BACKEND_ROOT, "PYTHONHASHSEED": "0", "HOME": tmp,
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        try:
            proc = subprocess.run([sys.executable, "-m", "app.strategies.experimental.runner"], input=json.dumps(job),
                                  capture_output=True, text=True, timeout=timeout, cwd=tmp, env=env)
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"sandbox timed out after {timeout}s") from exc
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            raise SandboxError(f"sandbox produced no result (exit {proc.returncode}): {proc.stderr[-500:]}")
        out = json.loads(lines[-1], parse_constant=lambda c: None)
        if not out.get("ok"):
            raise SandboxError(out.get("error", "unknown sandbox error"))
        return _finite(out["result"])


def _finite(x):
    """JSON/JSONB cannot hold Infinity/NaN (e.g. profit factor with no losing trades) -> None."""
    import math
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_finite(v) for v in x]
    return x
