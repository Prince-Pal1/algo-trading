"""Strategy graveyard — queryable registry of killed / retired strategies.

Invariant: every killed strategy lands here as one row. When a revival
attempt runs (today or in the future), it appends to that row's
`revival_attempts` list. This gives us a single-table view of every dead
strategy with its revival criteria.

Motivated by the 2026-04-14 Tier 5 M1 revival kill (task #100) — three
dead strategies, information scattered across obituary docstrings,
gitignored research JSON, task records, ROADMAP, and Known Gotchas.
Past ~5-10 dead strategies the scattered state becomes unusable.

Uses the same `data/trades.db` SQLite file as `ResultStore` — no new
.db file. Schema creation is lazy: first call to any public function
runs `CREATE TABLE IF NOT EXISTS` against the DB. Mirrors the
`ResultStore._init_tables` pattern.

Public API:
    - GraveyardEntry (dataclass mirror of the row)
    - record_kill(entry) -> row_id  (INSERT ... ON CONFLICT DO UPDATE)
    - append_revival_attempt(name, attempt_dict) -> None
    - list_graveyard(category=None, tag=None) -> list[GraveyardEntry]
    - get_graveyard_entry(name, version='v1') -> GraveyardEntry | None
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

KillCategory = Literal["TIMING", "NOISE", "PREMISE", "INFRA", "REGIME"]
_VALID_CATEGORIES: tuple[KillCategory, ...] = (
    "TIMING", "NOISE", "PREMISE", "INFRA", "REGIME",
)

_GRAVEYARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_graveyard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL DEFAULT 'v1',
    kill_date TEXT NOT NULL,
    kill_commit TEXT,
    kill_category TEXT NOT NULL,
    kill_reason_short TEXT NOT NULL,
    root_cause_long TEXT,
    revival_conditions TEXT,
    obituary_source_file TEXT,
    backtest_metrics_json TEXT,
    research_artifacts_json TEXT,
    revival_attempts_json TEXT DEFAULT '[]',
    task_ids TEXT,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (strategy_name, strategy_version)
);

CREATE INDEX IF NOT EXISTS idx_graveyard_category ON strategy_graveyard(kill_category);
CREATE INDEX IF NOT EXISTS idx_graveyard_name ON strategy_graveyard(strategy_name);
"""


@dataclass
class GraveyardEntry:
    """Row in the strategy_graveyard table.

    The JSON-encoded fields (backtest_metrics, research_artifacts,
    revival_attempts, task_ids, tags) are stored as native Python types
    on the dataclass and encoded at the DB boundary.
    """
    strategy_name: str
    kill_date: str
    kill_category: KillCategory
    kill_reason_short: str
    strategy_version: str = "v1"
    kill_commit: str | None = None
    root_cause_long: str | None = None
    revival_conditions: str | None = None
    obituary_source_file: str | None = None
    backtest_metrics: dict | None = None
    research_artifacts: list[dict] | None = None
    revival_attempts: list[dict] = field(default_factory=list)
    task_ids: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kill_category not in _VALID_CATEGORIES:
            raise ValueError(
                f"kill_category must be one of {_VALID_CATEGORIES}, "
                f"got {self.kill_category!r}"
            )


# ── Connection management ───────────────────────────────────────────────


def _connect(db_path: str) -> sqlite3.Connection:
    """Open an SQLite connection, create parent dir, install schema."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_GRAVEYARD_SCHEMA)
    conn.commit()
    return conn


# ── Row serialization ───────────────────────────────────────────────────


def _entry_to_row(entry: GraveyardEntry) -> dict:
    return {
        "strategy_name": entry.strategy_name,
        "strategy_version": entry.strategy_version,
        "kill_date": entry.kill_date,
        "kill_commit": entry.kill_commit,
        "kill_category": entry.kill_category,
        "kill_reason_short": entry.kill_reason_short,
        "root_cause_long": entry.root_cause_long,
        "revival_conditions": entry.revival_conditions,
        "obituary_source_file": entry.obituary_source_file,
        "backtest_metrics_json": json.dumps(entry.backtest_metrics) if entry.backtest_metrics else None,
        "research_artifacts_json": json.dumps(entry.research_artifacts) if entry.research_artifacts else None,
        "revival_attempts_json": json.dumps(entry.revival_attempts),
        "task_ids": ",".join(str(t) for t in entry.task_ids) if entry.task_ids else None,
        "tags": ",".join(entry.tags) if entry.tags else None,
    }


def _row_to_entry(row: sqlite3.Row) -> GraveyardEntry:
    def _parse_json(s: str | None, default):
        if s is None or s == "":
            return default
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return default

    def _parse_csv_ints(s: str | None) -> list[int]:
        if not s:
            return []
        return [int(x) for x in s.split(",") if x.strip()]

    def _parse_csv_strs(s: str | None) -> list[str]:
        if not s:
            return []
        return [x.strip() for x in s.split(",") if x.strip()]

    return GraveyardEntry(
        strategy_name=row["strategy_name"],
        strategy_version=row["strategy_version"],
        kill_date=row["kill_date"],
        kill_commit=row["kill_commit"],
        kill_category=row["kill_category"],
        kill_reason_short=row["kill_reason_short"],
        root_cause_long=row["root_cause_long"],
        revival_conditions=row["revival_conditions"],
        obituary_source_file=row["obituary_source_file"],
        backtest_metrics=_parse_json(row["backtest_metrics_json"], None),
        research_artifacts=_parse_json(row["research_artifacts_json"], None),
        revival_attempts=_parse_json(row["revival_attempts_json"], []),
        task_ids=_parse_csv_ints(row["task_ids"]),
        tags=_parse_csv_strs(row["tags"]),
    )


# ── Public API ──────────────────────────────────────────────────────────


def record_kill(
    entry: GraveyardEntry,
    db_path: str = "data/trades.db",
) -> int:
    """Insert or update a graveyard row. Idempotent on (strategy_name, strategy_version).

    If the row already exists, updates all mutable fields AND refreshes
    `updated_at`. `created_at` is preserved. Returns the row id.
    """
    row = _entry_to_row(entry)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO strategy_graveyard (
                strategy_name, strategy_version, kill_date, kill_commit,
                kill_category, kill_reason_short, root_cause_long,
                revival_conditions, obituary_source_file,
                backtest_metrics_json, research_artifacts_json,
                revival_attempts_json, task_ids, tags
            ) VALUES (
                :strategy_name, :strategy_version, :kill_date, :kill_commit,
                :kill_category, :kill_reason_short, :root_cause_long,
                :revival_conditions, :obituary_source_file,
                :backtest_metrics_json, :research_artifacts_json,
                :revival_attempts_json, :task_ids, :tags
            )
            ON CONFLICT (strategy_name, strategy_version) DO UPDATE SET
                kill_date = excluded.kill_date,
                kill_commit = excluded.kill_commit,
                kill_category = excluded.kill_category,
                kill_reason_short = excluded.kill_reason_short,
                root_cause_long = excluded.root_cause_long,
                revival_conditions = excluded.revival_conditions,
                obituary_source_file = excluded.obituary_source_file,
                backtest_metrics_json = excluded.backtest_metrics_json,
                research_artifacts_json = excluded.research_artifacts_json,
                revival_attempts_json = excluded.revival_attempts_json,
                task_ids = excluded.task_ids,
                tags = excluded.tags,
                updated_at = datetime('now')
            """,
            row,
        )
        conn.commit()
        # cur.lastrowid is 0 on UPDATE — re-query to get the real id
        if cur.lastrowid and cur.lastrowid > 0:
            row_id = cur.lastrowid
        else:
            row_id_cur = conn.execute(
                "SELECT id FROM strategy_graveyard WHERE strategy_name = ? AND strategy_version = ?",
                (entry.strategy_name, entry.strategy_version),
            )
            fetched = row_id_cur.fetchone()
            row_id = fetched["id"] if fetched else 0
        return row_id
    finally:
        conn.close()


def append_revival_attempt(
    strategy_name: str,
    attempt: dict,
    strategy_version: str = "v1",
    db_path: str = "data/trades.db",
) -> None:
    """Append one entry to the revival_attempts_json array.

    `attempt` must be a dict. Expected keys are at least:
      - date: ISO date string
      - method: short description of the approach
      - result: "REVIVE" | "KILL" | "PARTIAL"
    Extra keys are allowed (e.g., commit, backtest_metrics).

    Raises ValueError if the strategy row doesn't exist. Call record_kill()
    first to create the row.
    """
    if not isinstance(attempt, dict):
        raise TypeError(f"attempt must be a dict, got {type(attempt).__name__}")

    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT revival_attempts_json FROM strategy_graveyard "
            "WHERE strategy_name = ? AND strategy_version = ?",
            (strategy_name, strategy_version),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"no graveyard row for {strategy_name} v{strategy_version}; "
                f"call record_kill() first"
            )
        current = json.loads(row["revival_attempts_json"] or "[]")
        current.append(attempt)
        conn.execute(
            "UPDATE strategy_graveyard SET revival_attempts_json = ?, "
            "updated_at = datetime('now') "
            "WHERE strategy_name = ? AND strategy_version = ?",
            (json.dumps(current), strategy_name, strategy_version),
        )
        conn.commit()
    finally:
        conn.close()


def list_graveyard(
    category: str | None = None,
    tag: str | None = None,
    db_path: str = "data/trades.db",
) -> list[GraveyardEntry]:
    """Query the graveyard. Optional filters: category and tag.

    `tag` matches via substring on the comma-separated tags column, so
    `list_graveyard(tag="aggressive")` returns any row whose tags include
    `aggressive`.
    """
    conn = _connect(db_path)
    try:
        where_clauses = []
        params: list = []
        if category is not None:
            where_clauses.append("kill_category = ?")
            params.append(category)
        if tag is not None:
            where_clauses.append("(',' || tags || ',') LIKE ?")
            params.append(f"%,{tag},%")
        sql = "SELECT * FROM strategy_graveyard"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        sql += " ORDER BY kill_date DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]
    finally:
        conn.close()


def get_graveyard_entry(
    strategy_name: str,
    strategy_version: str = "v1",
    db_path: str = "data/trades.db",
) -> GraveyardEntry | None:
    """Single entry lookup by (name, version). Returns None if missing."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategy_graveyard WHERE strategy_name = ? AND strategy_version = ?",
            (strategy_name, strategy_version),
        ).fetchone()
        return _row_to_entry(row) if row else None
    finally:
        conn.close()
