"""Application update mechanism.

The engine publishes the latest version, an HTTPS download URL and the installer's SHA-256
(``RR_AGENT_LATEST_VERSION`` / ``RR_AGENT_UPDATE_URL`` / ``RR_AGENT_UPDATE_SHA256``). The connector asks with a
signed request, downloads over HTTPS, refuses any file whose SHA-256 differs, and only then starts the installer
(silent upgrade in place; settings and credentials are kept).
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

import httpx

from rr_connector import __version__
from rr_connector.agent import Identity, signed_headers, tls_verify
from rr_connector.store import Store
from rr_connector.vault import Vault


def _v(s: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in s.split("."))


def check(store: Store, vault: Vault, identity: Identity, transport=None) -> dict:
    url, agent_id, key = store.get("engine_url"), store.get("agent_id"), vault.get("api-key")
    if not (url and agent_id and key):
        return {"available": False, "reason": "not registered"}
    path = "/api/v1/mt5/agent/update"
    with httpx.Client(verify=tls_verify(store), timeout=20, transport=transport) as c:
        r = c.get(url + path, headers=signed_headers(identity, agent_id, key, "GET", path, b""))
    r.raise_for_status()
    d = r.json()
    if not d.get("available") or _v(d["version"]) <= _v(__version__):
        return {"available": False, "current": __version__}
    return {**d, "current": __version__}


def download_and_verify(info: dict, store: Store, transport=None) -> Path:
    if not str(info.get("url", "")).startswith("https://"):
        raise ValueError("update URL must be HTTPS")
    dest = Path(tempfile.mkdtemp(prefix="rrmt5-update-")) / "RR_MT5_Connector_Setup.exe"
    h = hashlib.sha256()
    with httpx.Client(verify=tls_verify(store), timeout=120, follow_redirects=True, transport=transport) as c, \
            c.stream("GET", info["url"]) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                h.update(chunk)
                f.write(chunk)
    if h.hexdigest().lower() != str(info.get("sha256", "")).lower():
        dest.unlink(missing_ok=True)
        raise ValueError("downloaded installer failed the SHA-256 check; update refused")
    return dest


def run_installer(path: Path) -> None:
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], close_fds=True)
