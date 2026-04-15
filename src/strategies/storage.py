"""Strategy storage — versioned registry for living strategies + their variants.

Complements `src/strategies/graveyard.py` (dead strategies) with a live-strategy
registry. Two tables in `data/trades.db`:

    strategies           — parent-level metadata (name, family, tier, markets, status)
    strategy_versions    — per-variant data (leverage mode, params, max-return cell,
                           report_dir path, verdict) — one row per {strategy × variant}

Design motivated by the 2026-04-15 Day 2 sprint (tasks #109-#116 + #79). We
shipped 9 discovered tasks producing ~16 deep_backtest run directories with
different leverage modes, baselines, windows, timeframes — zero index on disk,
no way to answer "show me all variants of donchian_gold + their max return +
HTML report link" without `ls reports/ | grep` + eyeballing.

This module is the data backbone. The auto-capture hook in
`src/backtest/deep_backtest.py::run_deep_backtest()` calls
`record_deep_backtest_result()` after every run. Future Streamlit page can
render the tables directly via `pd.DataFrame([asdict(v) for v in list_versions()])`.

Principal-engineer details:
- WAL mode + FK enforcement + retry-on-busy from day 1 (the graveyard doesn't
  have these; don't repeat the mistake)
- Lazy imports of BaseStrategy to dodge circular dependency risk
- Relative paths for report_dir so a repo move doesn't orphan the rows
- Do-not-clobber upsert semantics: None input fields preserve existing values
- Schema version stub (`_SCHEMA_VERSION = 1`) for future ALTER TABLE migrations

Public API:
    - StoredStrategy, StoredVersion (dataclass mirrors of the rows)
    - upsert_strategy(stored, *, db_path) -> int
    - upsert_version(stored, *, db_path) -> int
    - get_strategy(name, *, db_path) -> StoredStrategy | None
    - get_version(strategy_name, version_slug, *, db_path) -> StoredVersion | None
    - list_strategies(*, status=None, family=None, db_path) -> list[StoredStrategy]
    - list_versions(*, strategy_name=None, verdict=None, db_path) -> list[StoredVersion]
    - record_deep_backtest_result(strategy, result, *, db_path) -> tuple[int, int]
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _resolve_default_db() -> str:
    """Read the default DB path with env-var override.

    Tests set `ALGO_STRATEGY_DB` (typically via conftest.py autouse fixture)
    to route test writes to an isolated tmp DB, preventing pollution of the
    real `data/trades.db` when the deep_backtest auto-capture hook fires
    inside a test run.
    """
    return os.environ.get("ALGO_STRATEGY_DB", "data/trades.db")


# Default db path resolved at call time so tests that set ALGO_STRATEGY_DB
# in a pytest fixture are honored without needing to pass db_path to every
# helper explicitly. Functions that accept db_path as a kwarg default to this
# function's return value via `or _resolve_default_db()`.
_DEFAULT_DB_PATH = "data/trades.db"
# Bump this constant when adding any ALTER TABLE migration. The `_apply_migrations`
# runner checks `PRAGMA user_version` and runs the delta from the current stored
# version up to _SCHEMA_VERSION, committing after each step.
#
# Schema history:
#   v1 (task #117, 2026-04-15): initial strategies + strategy_versions tables
#   v2 (task #119, 2026-04-15): add wf_* walk-forward denormalized columns
#   v3 (task #121, 2026-04-15): add strategies.killed_graveyard_id FK
#   v4 (task #124, 2026-04-15): add strategy_versions.backtest_run_id FK
#   v5 (task #123, 2026-04-15): add strategy_version_runs append-only history table
_SCHEMA_VERSION = 5

# Sanity thresholds for the max-return cell — a cell failing these flags is a
# vanity trap (thin trade count, huge DD, or Calmar too weak). The future UI
# renders an amber badge on !sane rows without hiding the user's requested max.
_SANE_MIN_TRADES = 30
_SANE_MAX_DD_PCT = 60.0
_SANE_MIN_CALMAR = 0.2


_STORAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT UNIQUE NOT NULL,
    display_name        TEXT,
    description         TEXT,
    family              TEXT,
    category            TEXT,
    tier                TEXT,
    base_class          TEXT,
    markets_json        TEXT,
    default_timeframe   TEXT,
    leverage_range_json TEXT,
    status              TEXT NOT NULL DEFAULT 'researching',
    tags_json           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id              INTEGER NOT NULL REFERENCES strategies(id),
    version_slug             TEXT NOT NULL,
    description              TEXT,
    params_json              TEXT,
    leverage_mode            TEXT,
    baseline_leverage        REAL,
    timeframe                TEXT,
    tags_json                TEXT,
    last_backtested_at       TEXT,
    report_dir               TEXT,
    report_html_path         TEXT,
    summary_json_path        TEXT,
    verdict                  TEXT,
    verdict_reason           TEXT,
    matrix_n_cells           INTEGER,
    max_return_pct           REAL,
    max_return_cell_json     TEXT,
    max_return_sane          INTEGER,
    max_return_warning       TEXT,
    best_calmar              REAL,
    best_calmar_return_pct   REAL,
    best_calmar_cell_json    TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (strategy_id, version_slug)
);

CREATE INDEX IF NOT EXISTS idx_versions_strategy  ON strategy_versions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_versions_verdict   ON strategy_versions(verdict);
CREATE INDEX IF NOT EXISTS idx_strategies_status  ON strategies(status);
"""


# ── Migration registry ───────────────────────────────────────────────────
#
# Each migration takes an open connection and runs the delta from the prior
# version. Keyed by TARGET version (e.g., `2` means "take us from v1 to v2").
# Migrations are idempotent-safe by design — they use `ALTER TABLE ... ADD
# COLUMN` which SQLite tolerates across repeat runs via _column_exists checks.
#
# Rule: new migrations NEVER modify or delete existing columns in-place. SQLite
# ALTER TABLE has strict limitations (no DROP COLUMN until 3.35, no TYPE
# change). For destructive changes, create a new-named column + backfill +
# leave the old column for a release before dropping. Forward-compat first.


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _migration_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: Walk-forward denormalization (task #119).

    Denormalizes the walk_forward.continuous_* metrics onto strategy_versions
    so the future Streamlit page can sort/filter by WF Calmar without re-reading
    each summary.json off disk. Three new columns.
    """
    if not _column_exists(conn, "strategy_versions", "wf_continuous_return_pct"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN wf_continuous_return_pct REAL")
    if not _column_exists(conn, "strategy_versions", "wf_continuous_dd_pct"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN wf_continuous_dd_pct REAL")
    if not _column_exists(conn, "strategy_versions", "wf_continuous_calmar"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN wf_continuous_calmar REAL")
    if not _column_exists(conn, "strategy_versions", "wf_gate_passed"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN wf_gate_passed TEXT")
    if not _column_exists(conn, "strategy_versions", "wf_n_folds"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN wf_n_folds INTEGER")
    if not _column_exists(conn, "strategy_versions", "wf_profitable_folds"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN wf_profitable_folds INTEGER")


def _migration_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: Graveyard linkage (task #121).

    Adds a nullable FK from strategies to strategy_graveyard. When a strategy
    is killed, `kill_strategy()` flips status to 'killed' and sets this FK so
    the Streamlit page can cross-link parent rows to their obituary without
    a second query.
    """
    if not _column_exists(conn, "strategies", "killed_graveyard_id"):
        conn.execute("ALTER TABLE strategies ADD COLUMN killed_graveyard_id INTEGER")


def _migration_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: Link strategy_versions to backtest_runs (task #124).

    Adds a nullable FK from strategy_versions to the existing backtest_runs
    table. Populated by Stage 2 code that has strategies.py register also
    capture into ResultStore. MVP leaves this NULL; the column exists so the
    future Streamlit page can deep-link a version to the individual-run
    equity curve + trade ledger in the existing dashboard.
    """
    if not _column_exists(conn, "strategy_versions", "backtest_run_id"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN backtest_run_id INTEGER")


def _migration_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5: Append-only strategy_version_runs history table (task #123).

    One row per `run_deep_backtest()` call. Lets the Streamlit page show "3
    runs on this version over the past month" + detect regressions ("version
    X regressed from +140% to +12% between runs"). The `strategy_versions`
    row still holds the latest snapshot; this table holds the history.
    """
    if not _table_exists(conn, "strategy_version_runs"):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_version_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id        INTEGER NOT NULL REFERENCES strategy_versions(id),
                run_timestamp     TEXT NOT NULL,
                report_dir        TEXT,
                report_html_path  TEXT,
                summary_json_path TEXT,
                verdict           TEXT,
                max_return_pct    REAL,
                max_return_sane   INTEGER,
                best_calmar       REAL,
                matrix_n_cells    INTEGER,
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_runs_version ON strategy_version_runs(version_id);
            CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON strategy_version_runs(run_timestamp);
        """)


_MIGRATIONS: dict[int, "callable"] = {
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run pending migrations up to `_SCHEMA_VERSION`.

    Reads `PRAGMA user_version` (SQLite's per-db integer), runs every
    migration whose key is > current, and bumps `user_version` after each.
    Idempotent: re-running after a full migration is a no-op.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(_MIGRATIONS.keys()):
        if version > current:
            _MIGRATIONS[version](conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            current = version
    # Even if no migrations ran, ensure user_version reflects the current
    # schema version so a v1 DB that was created before migrations existed
    # gets stamped.
    if current < _SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass
class StoredStrategy:
    """Parent-level strategy row. Mirrors `strategies` table."""
    name: str
    id: int | None = None
    display_name: str | None = None
    description: str | None = None
    family: str | None = None
    category: str | None = None
    tier: str | None = None
    base_class: str | None = None
    markets: list[str] = field(default_factory=list)
    default_timeframe: str | None = None
    leverage_range: list[float] | None = None
    status: str = "researching"
    tags: list[str] = field(default_factory=list)
    # Schema v3: graveyard linkage. Populated by `kill_strategy()` when a
    # strategy lands in the graveyard. None for living strategies.
    killed_graveyard_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class StoredVersion:
    """Per-variant row. Mirrors `strategy_versions` table.

    Performance fields (last_backtested_at, report_dir, verdict, max_return_*,
    best_calmar_*) are all None until the first deep_backtest run records
    into this row.
    """
    strategy_id: int
    version_slug: str
    id: int | None = None
    description: str | None = None
    params: dict | None = None
    leverage_mode: str | None = None
    baseline_leverage: float | None = None
    timeframe: str | None = None
    tags: list[str] = field(default_factory=list)
    last_backtested_at: str | None = None
    report_dir: str | None = None
    report_html_path: str | None = None
    summary_json_path: str | None = None
    verdict: str | None = None
    verdict_reason: str | None = None
    matrix_n_cells: int | None = None
    max_return_pct: float | None = None
    max_return_cell: dict | None = None
    max_return_sane: bool | None = None
    max_return_warning: str | None = None
    best_calmar: float | None = None
    best_calmar_return_pct: float | None = None
    best_calmar_cell: dict | None = None
    # Schema v2: walk-forward denormalization. Populated from
    # result.walk_forward.continuous_* when WF ran for this backtest.
    wf_continuous_return_pct: float | None = None
    wf_continuous_dd_pct: float | None = None
    wf_continuous_calmar: float | None = None
    wf_gate_passed: str | None = None
    wf_n_folds: int | None = None
    wf_profitable_folds: int | None = None
    # Schema v4: nullable link to backtest_runs. Populated by Stage 2 code
    # that has scripts/backtest.py run also capture into storage.
    backtest_run_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


# ── Connection management ───────────────────────────────────────────────


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open an SQLite connection with WAL + FK pragmas + schema install.

    If `db_path` is None, reads `ALGO_STRATEGY_DB` env var (tests set this via
    conftest.py to route writes to a tmp DB), falling back to the module
    default "data/trades.db".

    Mirrors `graveyard._connect` shape but layers in production-grade pragmas
    the graveyard punts on:
      - WAL mode avoids writer-reader lock contention when a long deep_backtest
        writes mid-run and the dashboard holds readers on the same file.
      - foreign_keys=ON enforces the strategy_versions.strategy_id FK.
    """
    if db_path is None:
        db_path = _resolve_default_db()
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL is a no-op on :memory: — harmless to request.
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(_STORAGE_SCHEMA)
    conn.commit()
    # Apply any pending ALTER TABLE migrations to bring the schema up to
    # _SCHEMA_VERSION. Idempotent — safe to call on every connect.
    _apply_migrations(conn)
    return conn


def _retry_on_busy(fn, *, attempts: int = 3, backoff_ms: int = 100):
    """Retry a DB callable on `database is locked` with linear backoff.

    Prince runs multi-mode deep_backtests (see scripts/deep_backtest.py's
    per-mode loop) so two writers can hit this DB within milliseconds. SQLite's
    default lock error surfaces as `OperationalError: database is locked` —
    catch it, wait, retry. 3 attempts × 100ms covers realistic contention.
    """
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == attempts - 1:
                raise
            time.sleep((backoff_ms / 1000.0) * (i + 1))
    return None  # unreachable


# ── Path helpers ────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """Return the absolute path to the repo root (2 levels up from this file)."""
    return Path(__file__).resolve().parents[2]


def _to_relative(p: str | Path | None) -> str | None:
    """Store paths relative to the repo root so a repo move doesn't orphan rows.

    Idempotent: already-relative paths pass through. Absolute paths inside the
    repo get stripped of the REPO_ROOT prefix. Paths outside the repo get
    stored as-is (as absolute) — this is a fallback for unusual cases; nothing
    the pipeline itself will emit.
    """
    if p is None:
        return None
    path = Path(p)
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.relative_to(_repo_root()))
    except ValueError:
        return str(path)


def _to_absolute(p: str | None) -> Path | None:
    """Resolve a stored relative path back to absolute at read time."""
    if p is None:
        return None
    path = Path(p)
    if path.is_absolute():
        return path
    return _repo_root() / path


# ── Row (de)serialization ───────────────────────────────────────────────


def _parse_json(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def _strategy_to_row(s: StoredStrategy) -> dict:
    return {
        "name": s.name,
        "display_name": s.display_name,
        "description": s.description,
        "family": s.family,
        "category": s.category,
        "tier": s.tier,
        "base_class": s.base_class,
        "markets_json": json.dumps(s.markets) if s.markets else None,
        "default_timeframe": s.default_timeframe,
        "leverage_range_json": json.dumps(s.leverage_range) if s.leverage_range else None,
        "status": s.status,
        "tags_json": json.dumps(s.tags) if s.tags else None,
        "killed_graveyard_id": s.killed_graveyard_id,
    }


def _row_to_strategy(row: sqlite3.Row) -> StoredStrategy:
    keys = set(row.keys())
    return StoredStrategy(
        id=row["id"],
        name=row["name"],
        display_name=row["display_name"],
        description=row["description"],
        family=row["family"],
        category=row["category"],
        tier=row["tier"],
        base_class=row["base_class"],
        markets=_parse_json(row["markets_json"], []),
        default_timeframe=row["default_timeframe"],
        leverage_range=_parse_json(row["leverage_range_json"], None),
        status=row["status"],
        tags=_parse_json(row["tags_json"], []),
        killed_graveyard_id=row["killed_graveyard_id"] if "killed_graveyard_id" in keys else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version_to_row(v: StoredVersion) -> dict:
    return {
        "strategy_id": v.strategy_id,
        "version_slug": v.version_slug,
        "description": v.description,
        "params_json": json.dumps(v.params) if v.params is not None else None,
        "leverage_mode": v.leverage_mode,
        "baseline_leverage": v.baseline_leverage,
        "timeframe": v.timeframe,
        "tags_json": json.dumps(v.tags) if v.tags else None,
        "last_backtested_at": v.last_backtested_at,
        "report_dir": v.report_dir,
        "report_html_path": v.report_html_path,
        "summary_json_path": v.summary_json_path,
        "verdict": v.verdict,
        "verdict_reason": v.verdict_reason,
        "matrix_n_cells": v.matrix_n_cells,
        "max_return_pct": v.max_return_pct,
        "max_return_cell_json": json.dumps(v.max_return_cell) if v.max_return_cell else None,
        "max_return_sane": int(v.max_return_sane) if v.max_return_sane is not None else None,
        "max_return_warning": v.max_return_warning,
        "best_calmar": v.best_calmar,
        "best_calmar_return_pct": v.best_calmar_return_pct,
        "best_calmar_cell_json": json.dumps(v.best_calmar_cell) if v.best_calmar_cell else None,
        # Schema v2 — WF denormalization
        "wf_continuous_return_pct": v.wf_continuous_return_pct,
        "wf_continuous_dd_pct": v.wf_continuous_dd_pct,
        "wf_continuous_calmar": v.wf_continuous_calmar,
        "wf_gate_passed": v.wf_gate_passed,
        "wf_n_folds": v.wf_n_folds,
        "wf_profitable_folds": v.wf_profitable_folds,
        # Schema v4 — optional FK to backtest_runs
        "backtest_run_id": v.backtest_run_id,
    }


def _row_to_version(row: sqlite3.Row) -> StoredVersion:
    keys = set(row.keys())

    def _opt(col: str, default: Any = None) -> Any:
        return row[col] if col in keys else default

    sane_int = _opt("max_return_sane")
    return StoredVersion(
        id=row["id"],
        strategy_id=row["strategy_id"],
        version_slug=row["version_slug"],
        description=row["description"],
        params=_parse_json(row["params_json"], None),
        leverage_mode=row["leverage_mode"],
        baseline_leverage=row["baseline_leverage"],
        timeframe=row["timeframe"],
        tags=_parse_json(row["tags_json"], []),
        last_backtested_at=row["last_backtested_at"],
        report_dir=row["report_dir"],
        report_html_path=row["report_html_path"],
        summary_json_path=row["summary_json_path"],
        verdict=row["verdict"],
        verdict_reason=row["verdict_reason"],
        matrix_n_cells=row["matrix_n_cells"],
        max_return_pct=row["max_return_pct"],
        max_return_cell=_parse_json(row["max_return_cell_json"], None),
        max_return_sane=bool(sane_int) if sane_int is not None else None,
        max_return_warning=row["max_return_warning"],
        best_calmar=row["best_calmar"],
        best_calmar_return_pct=row["best_calmar_return_pct"],
        best_calmar_cell=_parse_json(row["best_calmar_cell_json"], None),
        wf_continuous_return_pct=_opt("wf_continuous_return_pct"),
        wf_continuous_dd_pct=_opt("wf_continuous_dd_pct"),
        wf_continuous_calmar=_opt("wf_continuous_calmar"),
        wf_gate_passed=_opt("wf_gate_passed"),
        wf_n_folds=_opt("wf_n_folds"),
        wf_profitable_folds=_opt("wf_profitable_folds"),
        backtest_run_id=_opt("backtest_run_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Version slug generation ─────────────────────────────────────────────


def _params_hash(params: dict | None) -> str:
    """Canonical 6-char sha1 of a params dict. Order-independent."""
    return hashlib.sha1(
        json.dumps(params or {}, sort_keys=True).encode()
    ).hexdigest()[:6]


def _generate_version_slug(
    leverage_mode: str | None,
    baseline_leverage: float | None,
    timeframe: str | None,
    params: dict | None,
    existing_versions: list["StoredVersion"],
) -> str:
    """Deterministic slug with param-aware collision handling.

    Base slug: `{mode}_L{int(baseline)}_{tf}` (e.g., `risk_scaled_L10_1h`).

    Idempotency contract:
      - Same (mode, leverage, tf, params) → same slug → UPSERTs same row.
      - Same (mode, leverage, tf) but different params → appends a 6-char
        sha1(params) hash to the base slug to distinguish variants.
      - A previously-hashed version with matching params gets reused.

    This means re-running `deep_backtest donchian_gold --leverage-mode
    risk_scaled` with identical params hits the same row every time;
    re-tuning params creates a new row with a distinct hash suffix.
    """
    parts = []
    if leverage_mode:
        parts.append(leverage_mode)
    if baseline_leverage is not None:
        parts.append(f"L{int(baseline_leverage)}")
    if timeframe:
        parts.append(timeframe)
    base = "_".join(parts) if parts else "default"

    new_hash = _params_hash(params)

    # Build a map of existing {slug: stored_params} for lookup
    existing_by_slug: dict[str, dict | None] = {
        v.version_slug: v.params for v in existing_versions
    }

    # Case 1: base slug doesn't exist yet → use it directly
    if base not in existing_by_slug:
        return base

    # Case 2: base slug exists. Compare params.
    existing_params = existing_by_slug[base]
    if _params_hash(existing_params) == new_hash:
        # Same params as the existing base-slug row → reuse (idempotent re-run)
        return base

    # Case 3: base slug exists with different params. Try the hashed variant.
    hashed_slug = f"{base}_{new_hash}"
    # Idempotent re-check: if a hashed variant with matching params exists, reuse.
    if hashed_slug in existing_by_slug and _params_hash(existing_by_slug[hashed_slug]) == new_hash:
        return hashed_slug
    return hashed_slug


# ── Public API — upsert ─────────────────────────────────────────────────


def upsert_strategy(
    stored: StoredStrategy,
    *,
    db_path: str | None = None,
) -> int:
    """INSERT-or-UPDATE a parent strategy row. Idempotent on `name`.

    Do-not-clobber semantics: on conflict, user-set metadata fields
    (description, family, category, tags) are only updated if the input is
    non-None. This means auto-capture from `run_deep_backtest()` never wipes
    manual annotations the user typed earlier via `scripts/strategies.py
    register`.

    Returns the row id.
    """
    row = _strategy_to_row(stored)
    conn = _connect(db_path)
    try:
        def _do_upsert():
            cur = conn.execute(
                """
                INSERT INTO strategies (
                    name, display_name, description, family, category, tier,
                    base_class, markets_json, default_timeframe,
                    leverage_range_json, status, tags_json, killed_graveyard_id
                ) VALUES (
                    :name, :display_name, :description, :family, :category, :tier,
                    :base_class, :markets_json, :default_timeframe,
                    :leverage_range_json, :status, :tags_json, :killed_graveyard_id
                )
                ON CONFLICT (name) DO UPDATE SET
                    display_name        = COALESCE(excluded.display_name, display_name),
                    description         = COALESCE(excluded.description, description),
                    family              = COALESCE(excluded.family, family),
                    category            = COALESCE(excluded.category, category),
                    tier                = COALESCE(excluded.tier, tier),
                    base_class          = COALESCE(excluded.base_class, base_class),
                    markets_json        = COALESCE(excluded.markets_json, markets_json),
                    default_timeframe   = COALESCE(excluded.default_timeframe, default_timeframe),
                    leverage_range_json = COALESCE(excluded.leverage_range_json, leverage_range_json),
                    status              = COALESCE(excluded.status, status),
                    tags_json           = COALESCE(excluded.tags_json, tags_json),
                    killed_graveyard_id = COALESCE(excluded.killed_graveyard_id, killed_graveyard_id),
                    updated_at          = datetime('now')
                """,
                row,
            )
            conn.commit()
            # ALWAYS re-query. SQLite's AUTOINCREMENT counter gets bumped on
            # conflict even when ON CONFLICT DO UPDATE is taken, so
            # cur.lastrowid can return a stale "next-available" value instead
            # of the actual updated row id.
            fetched = conn.execute(
                "SELECT id FROM strategies WHERE name = ?", (stored.name,),
            ).fetchone()
            return fetched["id"] if fetched else 0

        return _retry_on_busy(_do_upsert)
    finally:
        conn.close()


def upsert_version(
    stored: StoredVersion,
    *,
    db_path: str | None = None,
) -> int:
    """INSERT-or-UPDATE a version row. Idempotent on (strategy_id, version_slug).

    Do-not-clobber semantics: on conflict, performance fields
    (last_backtested_at, report_dir, verdict, max_return_*, best_calmar_*)
    update ONLY when the input dataclass supplies a non-None value. A second
    call without performance data does not wipe the first call's performance.

    This matters for Stage 2 backfill that touches parent metadata without
    rerunning the matrix.

    Returns the row id.
    """
    row = _version_to_row(stored)
    conn = _connect(db_path)
    try:
        def _do_upsert():
            cur = conn.execute(
                """
                INSERT INTO strategy_versions (
                    strategy_id, version_slug, description, params_json,
                    leverage_mode, baseline_leverage, timeframe, tags_json,
                    last_backtested_at, report_dir, report_html_path,
                    summary_json_path, verdict, verdict_reason, matrix_n_cells,
                    max_return_pct, max_return_cell_json, max_return_sane,
                    max_return_warning, best_calmar, best_calmar_return_pct,
                    best_calmar_cell_json,
                    wf_continuous_return_pct, wf_continuous_dd_pct,
                    wf_continuous_calmar, wf_gate_passed, wf_n_folds,
                    wf_profitable_folds, backtest_run_id
                ) VALUES (
                    :strategy_id, :version_slug, :description, :params_json,
                    :leverage_mode, :baseline_leverage, :timeframe, :tags_json,
                    :last_backtested_at, :report_dir, :report_html_path,
                    :summary_json_path, :verdict, :verdict_reason, :matrix_n_cells,
                    :max_return_pct, :max_return_cell_json, :max_return_sane,
                    :max_return_warning, :best_calmar, :best_calmar_return_pct,
                    :best_calmar_cell_json,
                    :wf_continuous_return_pct, :wf_continuous_dd_pct,
                    :wf_continuous_calmar, :wf_gate_passed, :wf_n_folds,
                    :wf_profitable_folds, :backtest_run_id
                )
                ON CONFLICT (strategy_id, version_slug) DO UPDATE SET
                    description            = COALESCE(excluded.description, description),
                    params_json            = COALESCE(excluded.params_json, params_json),
                    leverage_mode          = COALESCE(excluded.leverage_mode, leverage_mode),
                    baseline_leverage      = COALESCE(excluded.baseline_leverage, baseline_leverage),
                    timeframe              = COALESCE(excluded.timeframe, timeframe),
                    tags_json              = COALESCE(excluded.tags_json, tags_json),
                    last_backtested_at     = COALESCE(excluded.last_backtested_at, last_backtested_at),
                    report_dir             = COALESCE(excluded.report_dir, report_dir),
                    report_html_path       = COALESCE(excluded.report_html_path, report_html_path),
                    summary_json_path      = COALESCE(excluded.summary_json_path, summary_json_path),
                    verdict                = COALESCE(excluded.verdict, verdict),
                    verdict_reason         = COALESCE(excluded.verdict_reason, verdict_reason),
                    matrix_n_cells         = COALESCE(excluded.matrix_n_cells, matrix_n_cells),
                    max_return_pct         = COALESCE(excluded.max_return_pct, max_return_pct),
                    max_return_cell_json   = COALESCE(excluded.max_return_cell_json, max_return_cell_json),
                    max_return_sane        = COALESCE(excluded.max_return_sane, max_return_sane),
                    max_return_warning     = COALESCE(excluded.max_return_warning, max_return_warning),
                    best_calmar            = COALESCE(excluded.best_calmar, best_calmar),
                    best_calmar_return_pct = COALESCE(excluded.best_calmar_return_pct, best_calmar_return_pct),
                    best_calmar_cell_json  = COALESCE(excluded.best_calmar_cell_json, best_calmar_cell_json),
                    wf_continuous_return_pct = COALESCE(excluded.wf_continuous_return_pct, wf_continuous_return_pct),
                    wf_continuous_dd_pct     = COALESCE(excluded.wf_continuous_dd_pct, wf_continuous_dd_pct),
                    wf_continuous_calmar     = COALESCE(excluded.wf_continuous_calmar, wf_continuous_calmar),
                    wf_gate_passed           = COALESCE(excluded.wf_gate_passed, wf_gate_passed),
                    wf_n_folds               = COALESCE(excluded.wf_n_folds, wf_n_folds),
                    wf_profitable_folds      = COALESCE(excluded.wf_profitable_folds, wf_profitable_folds),
                    backtest_run_id          = COALESCE(excluded.backtest_run_id, backtest_run_id),
                    updated_at             = datetime('now')
                """,
                row,
            )
            conn.commit()
            # ALWAYS re-query (see upsert_strategy note).
            fetched = conn.execute(
                "SELECT id FROM strategy_versions WHERE strategy_id = ? AND version_slug = ?",
                (stored.strategy_id, stored.version_slug),
            ).fetchone()
            return fetched["id"] if fetched else 0

        return _retry_on_busy(_do_upsert)
    finally:
        conn.close()


# ── Public API — read ───────────────────────────────────────────────────


def get_strategy(
    name: str,
    *,
    db_path: str | None = None,
) -> StoredStrategy | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE name = ?", (name,),
        ).fetchone()
        return _row_to_strategy(row) if row else None
    finally:
        conn.close()


def get_version(
    strategy_name: str,
    version_slug: str,
    *,
    db_path: str | None = None,
) -> StoredVersion | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT v.* FROM strategy_versions v
            JOIN strategies s ON s.id = v.strategy_id
            WHERE s.name = ? AND v.version_slug = ?
            """,
            (strategy_name, version_slug),
        ).fetchone()
        return _row_to_version(row) if row else None
    finally:
        conn.close()


def list_strategies(
    *,
    status: str | None = None,
    family: str | None = None,
    db_path: str | None = None,
) -> list[StoredStrategy]:
    conn = _connect(db_path)
    try:
        where = []
        params: list[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if family is not None:
            where.append("family = ?")
            params.append(family)
        sql = "SELECT * FROM strategies"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_strategy(r) for r in rows]
    finally:
        conn.close()


def list_version_runs(
    *,
    version_id: int | None = None,
    strategy_name: str | None = None,
    version_slug: str | None = None,
    limit: int | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """Read history rows from `strategy_version_runs` (schema v5).

    Filters: specific version_id, OR (strategy_name, version_slug) pair for
    lookup by name. Returns dicts (not a dataclass — history rows are
    append-only audit records, not mutable entities).
    """
    conn = _connect(db_path)
    try:
        where: list[str] = []
        params: list[Any] = []
        sql = (
            "SELECT r.*, s.name AS strategy_name, v.version_slug "
            "FROM strategy_version_runs r "
            "JOIN strategy_versions v ON v.id = r.version_id "
            "JOIN strategies s ON s.id = v.strategy_id"
        )
        if version_id is not None:
            where.append("r.version_id = ?")
            params.append(version_id)
        if strategy_name is not None:
            where.append("s.name = ?")
            params.append(strategy_name)
        if version_slug is not None:
            where.append("v.version_slug = ?")
            params.append(version_slug)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.run_timestamp DESC, r.id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_best_by_max_return(
    *,
    verdict: str | None = None,
    limit: int = 10,
    db_path: str | None = None,
) -> list[StoredVersion]:
    """JSON1 helper (task #129) — rank all versions by max_return_pct.

    Optional filter by verdict (e.g., verdict='DEPLOYABLE' for "best among
    deployable candidates"). Returns the top N sorted descending.
    """
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT v.* FROM strategy_versions v "
            "JOIN strategies s ON s.id = v.strategy_id "
            "WHERE v.max_return_pct IS NOT NULL"
        )
        params: list[Any] = []
        if verdict is not None:
            sql += " AND v.verdict = ?"
            params.append(verdict)
        sql += f" ORDER BY v.max_return_pct DESC LIMIT {int(limit)}"
        return [_row_to_version(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_deployable_by_calmar(
    *,
    use_wf_calmar: bool = True,
    limit: int = 10,
    db_path: str | None = None,
) -> list[StoredVersion]:
    """JSON1 helper (task #129) — DEPLOYABLE versions sorted by Calmar.

    If `use_wf_calmar` (default), sorts by `wf_continuous_calmar` (the OOS
    metric that matters for deployment). Falls back to `best_calmar` (matrix
    best) when no WF was run.
    """
    calmar_col = "wf_continuous_calmar" if use_wf_calmar else "best_calmar"
    conn = _connect(db_path)
    try:
        sql = (
            f"SELECT v.* FROM strategy_versions v "
            f"WHERE v.verdict = 'DEPLOYABLE' AND v.{calmar_col} IS NOT NULL "
            f"ORDER BY v.{calmar_col} DESC LIMIT {int(limit)}"
        )
        return [_row_to_version(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def query_vanity_traps(
    *,
    db_path: str | None = None,
) -> list[StoredVersion]:
    """JSON1 helper (task #129) — vanity-trap detector.

    Returns versions where the max-return cell fails the sanity flag but the
    verdict is still DEPLOYABLE. These are cells where the headline number
    (+340% return) is driven by a thin trade count, huge drawdown, or low
    Calmar — a misleading "win" that would embarrass us on live capital.

    Use to audit the strategy storage for suspicious DEPLOYABLE verdicts
    before promoting anything to paper trading.
    """
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT v.* FROM strategy_versions v "
            "WHERE v.max_return_sane = 0 "
            "  AND v.verdict = 'DEPLOYABLE' "
            "ORDER BY v.max_return_pct DESC"
        )
        return [_row_to_version(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def kill_strategy(
    name: str,
    graveyard_id: int,
    *,
    db_path: str | None = None,
) -> None:
    """Flip a strategy row to status='killed' + set `killed_graveyard_id` FK.

    Decoupled from `graveyard.record_kill()` so the two modules remain
    independent. Call this from a script or the strategies CLI after landing
    a row in the graveyard. No-op if the strategy doesn't exist (creates
    nothing — use `upsert_strategy` first if needed).
    """
    conn = _connect(db_path)
    try:
        def _do():
            conn.execute(
                """
                UPDATE strategies
                SET status = 'killed',
                    killed_graveyard_id = ?,
                    updated_at = datetime('now')
                WHERE name = ?
                """,
                (graveyard_id, name),
            )
            conn.commit()

        _retry_on_busy(_do)
    finally:
        conn.close()


def list_versions(
    *,
    strategy_name: str | None = None,
    verdict: str | None = None,
    db_path: str | None = None,
) -> list[StoredVersion]:
    conn = _connect(db_path)
    try:
        where = []
        params: list[Any] = []
        sql = "SELECT v.* FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id"
        if strategy_name is not None:
            where.append("s.name = ?")
            params.append(strategy_name)
        if verdict is not None:
            where.append("v.verdict = ?")
            params.append(verdict)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.name, v.version_slug"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_version(r) for r in rows]
    finally:
        conn.close()


# ── Strategy resolution for auto-capture ────────────────────────────────


def _resolve_strategy(spec: Any) -> tuple[str, Any | None]:
    """Resolve `config.strategy` to (name, instance-or-None).

    `spec` can be a string (registry key or dotted path), a class, or an
    instance. We lazy-import BaseStrategy + STRATEGY_REGISTRY to dodge
    circular import risk.
    """
    # Lazy-import to avoid any risk of circular dependency (router.py imports
    # strategy classes which may transitively import src.backtest.engine).
    try:
        from src.strategies.base import BaseStrategy
        from src.strategies.router import STRATEGY_REGISTRY
    except Exception:
        BaseStrategy = None
        STRATEGY_REGISTRY = {}

    # Instance — we already have everything.
    if BaseStrategy is not None and isinstance(spec, BaseStrategy):
        return spec.name, spec

    # Class — instantiate with defaults if possible; otherwise return name only.
    if isinstance(spec, type):
        name = spec.__name__.lower()
        try:
            inst = spec()
            return getattr(inst, "name", name), inst
        except Exception:
            return name, None

    # String — try registry lookup.
    if isinstance(spec, str):
        key = spec.split(":")[-1].split(".")[-1].lower()
        cls = STRATEGY_REGISTRY.get(key) or STRATEGY_REGISTRY.get(spec)
        if cls is not None:
            try:
                inst = cls()
                return getattr(inst, "name", key), inst
            except Exception:
                return key, None
        return key, None

    return str(spec), None


# ── Auto-capture entry point (called from run_deep_backtest) ────────────


def record_deep_backtest_result(
    strategy: Any,
    result: Any,
    *,
    db_path: str | None = None,
) -> tuple[int, int]:
    """Upsert a parent strategy row + a version row for one deep_backtest run.

    Called by `run_deep_backtest()` after `write_report()`. Wrapped in a
    try/except at the call site so storage failures never break the pipeline.

    The version row captures:
      - the max-return cell from the matrix (user's explicit G.8 ask)
      - the existing best-Calmar cell (for completeness)
      - the verdict + verdict_reason
      - relative paths to the report_dir, index.html, summary.json

    Returns (strategy_id, version_id).
    """
    # Lazy-import to break any potential circular reference.
    from src.backtest.deep_backtest import _compute_max_return_cell, _is_max_return_sane

    strategy_name, inst = _resolve_strategy(strategy)

    # Extract description from the first line of the strategy's docstring, if
    # any — prevents DB/docstring drift (Rule 1: single source of truth).
    description = None
    if inst is not None and getattr(inst, "__doc__", None):
        doc = (type(inst).__doc__ or "").strip()
        if doc:
            description = doc.split("\n", 1)[0].strip()

    base_class = type(inst).__name__ if inst is not None else None
    tier = None
    if inst is not None and hasattr(inst, "tier"):
        t = inst.tier
        tier = t.name if hasattr(t, "name") else str(t)
    markets = list(getattr(inst, "markets", []) or []) if inst is not None else []
    default_tf = getattr(inst, "timeframe", None) if inst is not None else None
    lev_range = None
    if inst is not None and hasattr(inst, "leverage_range"):
        try:
            lev_range = [float(inst.leverage_range[0]), float(inst.leverage_range[1])]
        except Exception:
            lev_range = None

    parent = StoredStrategy(
        name=strategy_name,
        display_name=strategy_name,
        description=description,
        base_class=base_class,
        tier=tier,
        markets=markets,
        default_timeframe=default_tf,
        leverage_range=lev_range,
        status="researching",
    )
    strategy_id = upsert_strategy(parent, db_path=db_path)

    # Max-return cell + sanity flag from the matrix DataFrame.
    max_cell = _compute_max_return_cell(getattr(result, "matrix_df", None))
    sane, warning = _is_max_return_sane(max_cell)

    # Best-Calmar cell — use the existing DeepBacktestResult.best_cell so we
    # keep feature parity with the verdict logic.
    best_calmar_cell = None
    best_calmar_val = None
    best_calmar_return_pct = None
    bc = getattr(result, "best_cell", None)
    if bc is not None:
        try:
            from dataclasses import asdict as _asdict
            best_calmar_cell = _asdict(bc)
        except Exception:
            best_calmar_cell = None
        best_calmar_val = getattr(bc, "calmar", None)
        best_calmar_return_pct = getattr(bc, "return_pct", None)

    # Config-level fields for slug generation + version metadata.
    config = getattr(result, "config", None)
    lev_mode_val = None
    baseline_leverage = None
    timeframe = None
    params = None
    if config is not None:
        lm = getattr(config, "leverage_mode", None)
        if lm is not None:
            lev_mode_val = lm.value if hasattr(lm, "value") else str(lm)
        baseline_leverage = getattr(config, "baseline_leverage", None)
        tfs = getattr(config, "timeframes", None)
        if tfs:
            timeframe = tfs[0]
        params = getattr(config, "strategy_params", None)

    # Determine the slug. Honor `config.version_slug` if set (task #120
    # `--version-slug` CLI override). Otherwise auto-generate from
    # (mode, leverage, tf, params) with collision fallback.
    existing = list_versions(strategy_name=strategy_name, db_path=db_path)
    explicit_slug = getattr(config, "version_slug", None) if config is not None else None
    if explicit_slug:
        slug = explicit_slug
    else:
        slug = _generate_version_slug(
            leverage_mode=lev_mode_val,
            baseline_leverage=baseline_leverage,
            timeframe=timeframe,
            params=params,
            existing_versions=existing,
        )

    # Paths — store relative to repo root.
    report_dir = getattr(result, "report_dir", None)
    report_dir_rel = _to_relative(report_dir) if report_dir is not None else None
    html_path_rel = None
    summary_path_rel = None
    if report_dir is not None:
        report_dir_path = Path(report_dir)
        html_path = report_dir_path / "index.html"
        summary_path = report_dir_path / "summary.json"
        if html_path.exists():
            html_path_rel = _to_relative(html_path)
        if summary_path.exists():
            summary_path_rel = _to_relative(summary_path)

    # Walk-forward denormalization (schema v2).
    wf = getattr(result, "walk_forward", None)
    wf_ret = wf_dd = wf_calmar = None
    wf_gate = wf_nf = wf_prof = None
    if wf is not None:
        wf_ret = getattr(wf, "continuous_return_pct", None)
        wf_dd = getattr(wf, "continuous_dd_pct", None)
        wf_calmar = getattr(wf, "continuous_calmar", None)
        gp = getattr(wf, "continuous_gate_passed", None)
        wf_gate = str(gp) if gp is not None else None
        wf_nf = getattr(wf, "n_folds", None)
        wf_prof = getattr(wf, "profitable_folds", None)

    version = StoredVersion(
        strategy_id=strategy_id,
        version_slug=slug,
        params=params,
        leverage_mode=lev_mode_val,
        baseline_leverage=baseline_leverage,
        timeframe=timeframe,
        last_backtested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        report_dir=report_dir_rel,
        report_html_path=html_path_rel,
        summary_json_path=summary_path_rel,
        verdict=getattr(result, "verdict", None),
        verdict_reason=getattr(result, "verdict_reason", None),
        matrix_n_cells=len(getattr(result, "matrix", []) or []),
        max_return_pct=max_cell.get("return_pct") if max_cell else None,
        max_return_cell=max_cell or None,
        max_return_sane=sane if max_cell else None,
        max_return_warning=warning or None,
        best_calmar=best_calmar_val,
        best_calmar_return_pct=best_calmar_return_pct,
        best_calmar_cell=best_calmar_cell,
        wf_continuous_return_pct=wf_ret,
        wf_continuous_dd_pct=wf_dd,
        wf_continuous_calmar=wf_calmar,
        wf_gate_passed=wf_gate,
        wf_n_folds=wf_nf,
        wf_profitable_folds=wf_prof,
    )
    version_id = upsert_version(version, db_path=db_path)

    # Schema v5: append-only run history
    _append_version_run(
        version_id=version_id,
        version=version,
        db_path=db_path,
    )

    return strategy_id, version_id


def _append_version_run(
    *,
    version_id: int,
    version: StoredVersion,
    db_path: str | None = None,
) -> int:
    """Append one row to `strategy_version_runs` — the history table (schema v5).

    Called inside `record_deep_backtest_result` after the version row UPSERT.
    Each call creates a new row (no dedup) so the history table is a true
    append-only audit log of "every deep_backtest run that ever completed for
    this version".

    Returns the new row id.
    """
    conn = _connect(db_path)
    try:
        def _do_insert():
            cur = conn.execute(
                """
                INSERT INTO strategy_version_runs (
                    version_id, run_timestamp, report_dir, report_html_path,
                    summary_json_path, verdict, max_return_pct, max_return_sane,
                    best_calmar, matrix_n_cells
                ) VALUES (
                    :version_id, :run_timestamp, :report_dir, :report_html_path,
                    :summary_json_path, :verdict, :max_return_pct, :max_return_sane,
                    :best_calmar, :matrix_n_cells
                )
                """,
                {
                    "version_id": version_id,
                    "run_timestamp": version.last_backtested_at,
                    "report_dir": version.report_dir,
                    "report_html_path": version.report_html_path,
                    "summary_json_path": version.summary_json_path,
                    "verdict": version.verdict,
                    "max_return_pct": version.max_return_pct,
                    "max_return_sane": int(version.max_return_sane) if version.max_return_sane is not None else None,
                    "best_calmar": version.best_calmar,
                    "matrix_n_cells": version.matrix_n_cells,
                },
            )
            conn.commit()
            return cur.lastrowid or 0

        return _retry_on_busy(_do_insert) or 0
    finally:
        conn.close()
