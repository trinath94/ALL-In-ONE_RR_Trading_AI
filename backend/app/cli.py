"""Administrative CLI.

    python -m app.cli seed                       # roles, permissions, strategies, risk configs (idempotent)
    RR_ADMIN_PASSWORD=... python -m app.cli create-admin --username admin
    python -m app.cli gen-keys                   # print fresh JWT secret / Fernet key / bridge secret
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.analysis.sessions import DEFAULT_SESSIONS
from app.api.deps import PERMISSIONS, ROLE_PERMS
from app.core.security import hash_password
from app.db.models import (MarketSession, Permission, RiskConfig, Role, Strategy, StrategyParameter, StrategyVersion,
                           TradingState, User)
from app.db.session import session_scope
from app.risk.engine import RiskParams
from app.strategies import registry
from app.strategies.base import CAP_TICK_VOLUME

# Data/infrastructure that a standard MT5 broker connection provides
BASELINE_CAPS = {CAP_TICK_VOLUME, "peer_symbols"}


def seed() -> dict:
    out = {"strategies_new": 0, "versions_new": 0}
    with session_scope() as db:
        perms = {}
        for code, desc in PERMISSIONS.items():
            p = db.scalar(select(Permission).where(Permission.code == code)) or Permission(code=code, description=desc)
            db.add(p)
            perms[code] = p
        db.flush()
        for name, codes in ROLE_PERMS.items():
            r = db.scalar(select(Role).where(Role.name == name)) or Role(name=name, description=name.title())
            r.permissions = [perms[c] for c in codes]
            db.add(r)
        for mode in ("DEMO", "LIVE"):
            if not db.scalar(select(RiskConfig).where(RiskConfig.account_mode == mode)):
                params = RiskParams()
                if mode == "LIVE":  # stricter defaults for live
                    params.risk_per_trade_pct, params.max_daily_loss_pct, params.max_open_positions = 0.25, 1.0, 3
                db.add(RiskConfig(account_mode=mode, params=params.__dict__))
        if not db.get(TradingState, 1):
            db.add(TradingState(id=1, active_mode="DEMO", auto_trading=False))
        for s in DEFAULT_SESSIONS.values():
            if not db.scalar(select(MarketSession).where(MarketSession.name == s.name)):
                db.add(MarketSession(name=s.name, timezone=s.tz, start_local=s.start.strftime("%H:%M"),
                                     end_local=s.end.strftime("%H:%M"), kind=s.kind))
        for m in registry.metas():
            st = db.scalar(select(Strategy).where(Strategy.code == m.code))
            missing = [r for r in m.requires if r not in BASELINE_CAPS]
            availability = "available" if not missing else "unavailable: requires " + ", ".join(missing)
            if st is None:
                st = Strategy(code=m.code, name=m.name, category=m.category, description=m.description,
                              stage="PRODUCTION", enabled=m.default_enabled and not missing, risky=m.risky,
                              availability=availability)
                db.add(st)
                db.flush()
                out["strategies_new"] += 1
            else:
                st.availability = availability
                st.name, st.description = m.name, m.description
                if missing:
                    st.enabled = False
            cur = db.get(StrategyVersion, st.current_version_id) if st.current_version_id else None
            if cur is None or cur.code_hash != m.code_hash:
                # Never overwrite history: every code change creates a new version row.
                v = StrategyVersion(strategy_id=st.id, version=m.version if cur is None else f"{m.version}+{m.code_hash[:6]}",
                                    code_hash=m.code_hash, notes="registered from code", created_by="seed")
                db.add(v)
                db.flush()
                for k, val in m.params.items():
                    db.add(StrategyParameter(strategy_version_id=v.id, name=k, default_value={"value": val}))
                st.current_version_id = v.id
                out["versions_new"] += 1
    return out


def create_admin(username: str, password: str) -> None:
    with session_scope() as db:
        role = db.scalar(select(Role).where(Role.name == "admin"))
        if role is None:
            raise SystemExit("run seed first")
        if db.scalar(select(User).where(User.username == username)):
            raise SystemExit("user exists")
        db.add(User(username=username, password_hash=hash_password(password), role_id=role.id))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    ca = sub.add_parser("create-admin")
    ca.add_argument("--username", required=True)
    sub.add_parser("gen-keys")
    a = ap.parse_args(argv)
    if a.cmd == "seed":
        print(seed())
    elif a.cmd == "create-admin":
        pw = os.environ.get("RR_ADMIN_PASSWORD")
        if not pw:
            print("set RR_ADMIN_PASSWORD", file=sys.stderr)
            raise SystemExit(2)
        create_admin(a.username, pw)
        print("admin created")
    elif a.cmd == "gen-keys":
        print(f"RR_JWT_SECRET={secrets.token_urlsafe(48)}")
        print(f"RR_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
        print(f"RR_BRIDGE_SHARED_SECRET={secrets.token_urlsafe(48)}")


if __name__ == "__main__":
    main()
