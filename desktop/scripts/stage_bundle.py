"""Stage the files the RR Trading One installer carries next to the app (desktop/target/bundle-src).

The installed folder must let the app run everything without a source checkout:

    <install>\\rr-trading-one.exe          the desktop app (built by Tauri)
    <install>\\infrastructure\\             compose.yml, nginx config, backend Dockerfile, uninstall helper
    <install>\\backend\\  frontend\\  docker\\frontend.Dockerfile
                                           the engine sources Start All builds the local images from
    <install>\\.dockerignore               keeps the app and connector out of the Docker build context
    <install>\\mt5-connector\\              the packaged MT5 connector (PyInstaller folder, RR_MT5_Connector.exe)

Only files tracked by git are staged (never node_modules, build output, local secrets or caches), so a developer
build and a CI build carry the same engine. The staged tree is listed in BUNDLE_MANIFEST.txt with SHA-256 hashes.

    python desktop/scripts/stage_bundle.py --connector windows-connector/dist/RR_MT5_Connector
    python desktop/scripts/stage_bundle.py --no-connector        (layout checks on machines that cannot build it)
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "desktop" / "target" / "bundle-src"
ENGINE_PATHS = ["backend", "frontend", "infrastructure", "docker/frontend.Dockerfile"]
REQUIRED = [
    "infrastructure/compose.yml", "infrastructure/nginx.local.conf", "infrastructure/backend.Dockerfile",
    "infrastructure/windows/uninstall-stop.ps1", "docker/frontend.Dockerfile", "backend/requirements.lock",
    "backend/alembic.ini", "backend/app/main.py", "backend/tests/baseline/strategy_inventory.json",
    "frontend/package.json", "frontend/package-lock.json", ".dockerignore",
]
# Never shipped, even if tracked by mistake.
FORBIDDEN_PARTS = {"node_modules", ".next", "__pycache__", ".venv", ".env"}


def tracked(paths: list[str]) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *paths], cwd=ROOT, check=True, capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--connector", type=Path, help="PyInstaller output folder containing RR_MT5_Connector.exe")
    g.add_argument("--no-connector", action="store_true")
    args = ap.parse_args()

    if OUT.exists():
        shutil.rmtree(OUT)
    files = tracked(ENGINE_PATHS)
    bad = [f for f in files if FORBIDDEN_PARTS & set(Path(f).parts)]
    if bad:
        print(f"refusing to ship: {bad[:5]}", file=sys.stderr)
        return 1
    for rel in files:
        src = ROOT / rel
        if not src.is_file():          # deleted in the working tree but still in the index
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(ROOT / "infrastructure" / "install.dockerignore", OUT / ".dockerignore")

    if args.connector:
        exe = args.connector / "RR_MT5_Connector.exe"
        if not exe.is_file():
            print(f"{exe} not found: build the connector first (windows-connector/packaging)", file=sys.stderr)
            return 1
        shutil.copytree(args.connector, OUT / "mt5-connector")
        REQUIRED.append("mt5-connector/RR_MT5_Connector.exe")

    missing = [r for r in REQUIRED if not (OUT / r).is_file()]
    if missing:
        print(f"staged bundle is missing {missing}", file=sys.stderr)
        return 1

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *ENGINE_PATHS], cwd=ROOT, capture_output=True,
                           text=True).stdout.strip()
    lines = [f"# RR Trading One installer contents. source commit {commit}{' (with uncommitted changes)' if dirty else ''}"]
    count = 0
    for p in sorted(x for x in OUT.rglob("*") if x.is_file()):
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(OUT).as_posix()}")
        count += 1
    (OUT / "BUNDLE_MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"staged {count} files in {OUT} ({'with' if args.connector else 'without'} the MT5 connector)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
