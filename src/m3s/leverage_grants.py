"""SQLite append store for M3S leverage grants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.utils.logger import get_logger
from src.utils.types import LeverageGrant

log = get_logger("m3s.leverage_grants")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS leverage_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    requested REAL NOT NULL,
    granted REAL NOT NULL,
    reason TEXT NOT NULL,
    conviction REAL NOT NULL,
    declared_range_min REAL NOT NULL,
    declared_range_max REAL NOT NULL,
    regime_target REAL NOT NULL,
    conviction_target REAL NOT NULL,
    aggregate_before REAL NOT NULL,
    aggregate_cap REAL NOT NULL,
    m3s_regime TEXT NOT NULL,
    user_reason TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_leverage_grants_ts
    ON leverage_grants(ts_ms);
CREATE INDEX IF NOT EXISTS idx_leverage_grants_strategy
    ON leverage_grants(strategy_name);
"""


class LeverageGrantStore:
    def __init__(self, db_path: str = "data/trades.db") -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def append(self, grant: LeverageGrant) -> int:
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            INSERT INTO leverage_grants (
                ts_ms, strategy_name, requested, granted, reason,
                conviction, declared_range_min, declared_range_max,
                regime_target, conviction_target, aggregate_before,
                aggregate_cap, m3s_regime, user_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(grant.ts_ms),
                str(grant.strategy_name),
                float(grant.requested),
                float(grant.granted),
                grant.reason.value,
                float(grant.conviction),
                float(grant.declared_range_min),
                float(grant.declared_range_max),
                float(grant.regime_target),
                float(grant.conviction_target),
                float(grant.aggregate_before),
                float(grant.aggregate_cap),
                str(grant.m3s_regime),
                str(grant.user_reason),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

    def count(self) -> int:
        conn = self._ensure_conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM leverage_grants").fetchone()
        return int(row["c"] if row else 0)

    def recent(self, limit: int = 50) -> list[dict]:
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT * FROM leverage_grants ORDER BY ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
