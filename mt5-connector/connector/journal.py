r"""Durable execution journal (SQLite, WAL, synchronous=FULL).

State machine per execution_id:
    RECEIVED -> SENDING -> DONE | REJECTED | FAILED
                       \-> UNKNOWN   (process died / MT5 call raised after SENDING)

The row is committed in state SENDING *before* order_send is called. If the process dies
between order_send and recording the result, the next request for the same execution_id
finds SENDING and reconciles against MT5 (orders/positions/deals tagged with the id)
instead of sending again. This is what guarantees no duplicate execution.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    state        TEXT NOT NULL,
    result       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, ts REAL NOT NULL);
"""


class Journal:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get(self, execution_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT execution_id, command, state, result, created_at, updated_at "
                                     "FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not row:
            return None
        return {"execution_id": row[0], "command": json.loads(row[1]), "state": row[2],
                "result": json.loads(row[3]) if row[3] else None, "created_at": row[4], "updated_at": row[5]}

    def reserve(self, execution_id: str, command: dict) -> tuple[bool, dict | None]:
        """Atomically insert RECEIVED. Returns (created, existing_row)."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT state FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
            if row:
                return False, None
            c.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)",
                      (execution_id, json.dumps(command, sort_keys=True), "RECEIVED", None, now, now))
        return True, None

    def set_state(self, execution_id: str, state: str, result: dict | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE executions SET state=?, result=COALESCE(?, result), updated_at=? WHERE execution_id=?",
                      (state, json.dumps(result, default=str) if result is not None else None, time.time(), execution_id))

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT execution_id, state, updated_at FROM executions "
                                      "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"execution_id": r[0], "state": r[1], "updated_at": r[2]} for r in rows]

    def unresolved(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT execution_id FROM executions WHERE state IN ('SENDING','UNKNOWN','RECEIVED')").fetchall()
        return [r[0] for r in rows]

    def use_nonce(self, nonce: str, ttl: float = 120) -> bool:
        """Returns False if the nonce was already seen (replay)."""
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM nonces WHERE ts < ?", (now - ttl,))
            if c.execute("SELECT 1 FROM nonces WHERE nonce=?", (nonce,)).fetchone():
                return False
            c.execute("INSERT INTO nonces VALUES (?,?)", (nonce, now))
        return True
