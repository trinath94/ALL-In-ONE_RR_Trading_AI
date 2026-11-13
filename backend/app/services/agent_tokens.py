"""One-time registration tokens for the RR MT5 Connector (shared by the API route and the local supervisor).

``python -m app.services.agent_tokens --label LABEL [--ttl-minutes N]`` issues a token and prints it as JSON on
stdout. RR Trading One's desktop supervisor runs it inside the local ``api`` container (fixed argv, no shell) to link
its own connector without an admin web login; the token is handed to the connector on stdin, used immediately,
and only its SHA-256 is stored. Every issue is audited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone

from app.db.models_agent import MT5AgentToken
from app.services.events import audit

UTC = timezone.utc
MAX_CLI_TTL_MINUTES = 60


def issue(db, label: str, created_by: int | None, ttl: timedelta, ip: str | None = None) -> tuple[str, datetime]:
    token = "rrmt5_" + secrets.token_urlsafe(32)
    exp = datetime.now(UTC) + ttl
    db.add(MT5AgentToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), label=label[:128],
                         created_by=created_by, expires_at=exp))
    audit(db, created_by, "MT5_AGENT_TOKEN_CREATED", label[:128], {"expires_at": exp.isoformat()}, ip)
    return token, exp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent_tokens")
    p.add_argument("--label", required=True)
    p.add_argument("--ttl-minutes", type=int, default=10)
    a = p.parse_args(argv)
    if not 1 <= a.ttl_minutes <= MAX_CLI_TTL_MINUTES:
        print(f"--ttl-minutes must be 1..{MAX_CLI_TTL_MINUTES}", file=sys.stderr)
        return 2
    from app.db.session import session_scope
    with session_scope() as db:
        token, exp = issue(db, f"local:{a.label}", None, timedelta(minutes=a.ttl_minutes), "local-supervisor")
    print(json.dumps({"token": token, "expires_at": exp.isoformat()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
