"""Local configuration (SQLite, every value encrypted) and the connection/audit event log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from rr_connector.vault import Vault

DEFAULTS: dict[str, Any] = {
    "engine_url": "", "agent_id": None, "agent_name": "", "device_id": "", "ca_file": "",
    "mt5_address": "", "mt5_port": 443, "terminal_path": "", "auto_connect": False,
    "autostart": True, "start_minimized": False,
    "live_authorization": None,       # {"login", "server", "at"} — bound to one account
    "guard": {"max_lot": 1.0, "max_open_positions": 10, "max_daily_loss_pct": 5.0},
    "day_start": None,                # {"date", "equity"}
}


class Store:
    def __init__(self, path: Path, vault: Vault):
        self.path, self.vault = Path(path), vault
        self.lock = threading.RLock()
        key = vault.get("config-key")
        if not key:
            key = Fernet.generate_key().decode()
            vault.set("config-key", key)
        self.f = Fernet(key.encode())
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
                        " level TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL)")

    # -- settings ------------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULTS.get(key, default) if default is None else default
        try:
            return json.loads(self.f.decrypt(row[0]))
        except InvalidToken:
            return DEFAULTS.get(key, default)       # key rotated / foreign file: treat as unset

    def set(self, key: str, value: Any) -> None:
        blob = self.f.encrypt(json.dumps(value).encode())
        with self.lock:
            self.db.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, blob))

    def update(self, **kv: Any) -> None:
        for k, v in kv.items():
            self.set(k, v)

    # -- events --------------------------------------------------------------------------------
    def event(self, level: str, category: str, message: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO events(ts, level, category, message) VALUES(?,?,?,?)",
                            (time.time(), level, category, message[:2000]))
            self.db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 20000")

    def events(self, limit: int = 500, category: str | None = None) -> list[dict]:
        q, args = "SELECT ts, level, category, message FROM events", []
        if category:
            q, args = q + " WHERE category=?", [category]
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"ts": r[0], "level": r[1], "category": r[2], "message": r[3]} for r in rows]

    def close(self) -> None:
        with self.lock:
            self.db.close()
