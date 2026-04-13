"""M3S SQLite state store.

Two tables (see docs/planning/m3s_plan_v1.md § 6):

    m3s_state   — key/value scalars per namespace (compound, allocation, mode)
    m3s_events  — append-only event log (allocations, compound updates, DD events)

Design rules:
- Single connection, single-writer from the M3S scheduler task.
- Boot-safe: creates the parent directory and both tables IF NOT EXISTS.
- Values are stored as msgspec-JSON strings so complex dicts/lists survive
  round-tripping without losing type fidelity.
- Crash recovery: a missing key returns None. The caller decides the fallback
  (typically: fixed-weight 40/30/30 and a STARTUP_DEGRADED event — but that
  lives in a later sub-phase's hooks.py).
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

import msgspec

from src.utils.logger import get_logger

log = get_logger("m3s.state")


# ── Schema ──────────────────────────────────────────────────────────────


_SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS m3s_state (
    namespace     TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    updated_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (namespace, key)
)
"""

_SCHEMA_EVENTS = """
CREATE TABLE IF NOT EXISTS m3s_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    inputs_hash TEXT
)
"""

_INDEX_EVENTS_TS = (
    "CREATE INDEX IF NOT EXISTS idx_m3s_events_ts ON m3s_events(ts_ms)"
)
_INDEX_EVENTS_TYPE = (
    "CREATE INDEX IF NOT EXISTS idx_m3s_events_type ON m3s_events(event_type)"
)


# ── Store ───────────────────────────────────────────────────────────────


class M3SStore:
    """Thin wrapper over sqlite3 for the two M3S tables.

    Usage:
        store = M3SStore("data/m3s.sqlite")
        store.put("compound", "hwm", 12345.67)
        hwm = store.get("compound", "hwm")           # -> 12345.67
        store.append_event("allocation", {"weights": {...}})
        events = store.query_events(event_type="allocation")
        store.close()
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        # check_same_thread=False — the scheduler is the single writer but
        # async tasks may dispatch callbacks from other threads in future
        # sub-phases. Access is still serialized via the store instance.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(_SCHEMA_STATE)
            self._conn.execute(_SCHEMA_EVENTS)
            self._conn.execute(_INDEX_EVENTS_TS)
            self._conn.execute(_INDEX_EVENTS_TYPE)

    # ── Scalar/JSON KV ───────────────────────────────────────────────

    def put(self, namespace: str, key: str, value: Any) -> None:
        """Upsert a value under (namespace, key). Value is JSON-encoded.

        Any msgspec-serializable value is accepted (scalars, lists, dicts,
        msgspec.Structs). Last write wins.
        """
        encoded = msgspec.json.encode(value).decode("utf-8")
        now_ms = int(time.time() * 1000)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO m3s_state (namespace, key, value, updated_ts_ms)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value = excluded.value,
                    updated_ts_ms = excluded.updated_ts_ms
                """,
                (namespace, key, encoded, now_ms),
            )

    def get(self, namespace: str, key: str) -> Any | None:
        """Return the decoded value, or None if missing."""
        cur = self._conn.execute(
            "SELECT value FROM m3s_state WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return msgspec.json.decode(row[0])

    def delete(self, namespace: str, key: str) -> bool:
        """Delete a key. Returns True if something was deleted."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM m3s_state WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            return cur.rowcount > 0

    def list_keys(self, namespace: str) -> list[str]:
        """List all keys in a namespace (ordered)."""
        cur = self._conn.execute(
            "SELECT key FROM m3s_state WHERE namespace = ? ORDER BY key",
            (namespace,),
        )
        return [r[0] for r in cur.fetchall()]

    # ── Event log (append-only) ─────────────────────────────────────

    def append_event(
        self,
        event_type: str,
        payload: Any,
        *,
        ts_ms: int | None = None,
        inputs_hash: str | None = None,
    ) -> int:
        """Append one event. Returns the auto-assigned row id.

        event_type is a free-form string; for M3S built-in events use the
        M3SEventType enum from src.m3s.types. The caller is free to record
        custom types too (e.g., CUSTOM_CONFIG_LOADED).
        """
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        encoded = msgspec.json.encode(payload).decode("utf-8")
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO m3s_events (ts_ms, event_type, payload, inputs_hash)
                VALUES (?, ?, ?, ?)
                """,
                (ts_ms, event_type, encoded, inputs_hash),
            )
            return int(cur.lastrowid or 0)

    def query_events(
        self,
        *,
        event_type: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return events in chronological (ts_ms, id) order.

        Each row is a dict: {id, ts_ms, event_type, payload, inputs_hash}.
        payload is already decoded from JSON.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("ts_ms <= ?")
            params.append(until_ms)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""
        sql = (
            "SELECT id, ts_ms, event_type, payload, inputs_hash "
            f"FROM m3s_events{where} ORDER BY ts_ms ASC, id ASC{limit_clause}"
        )
        cur = self._conn.execute(sql, params)
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            out.append({
                "id": row[0],
                "ts_ms": row[1],
                "event_type": row[2],
                "payload": msgspec.json.decode(row[3]),
                "inputs_hash": row[4],
            })
        return out

    def event_count(self, event_type: str | None = None) -> int:
        """Total event count, optionally filtered by type."""
        if event_type is None:
            cur = self._conn.execute("SELECT COUNT(*) FROM m3s_events")
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM m3s_events WHERE event_type = ?",
                (event_type,),
            )
        return int(cur.fetchone()[0])

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as e:
            log.warning("m3s.state.close_failed", error=str(e))

    def __enter__(self) -> M3SStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
