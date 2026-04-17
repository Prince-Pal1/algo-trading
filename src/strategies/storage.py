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
#   v6 (task #133, 2026-04-15): add strategy_versions.facets_json — per-(window,fee)
#                               best-cell cache keyed by "{window_label}::{fee_profile}".
#                               Unlocks the hierarchical Strategies page (Layer 1 fee
#                               filter → Layer 2 window hero → Layer 3 mode hero → Layer 4
#                               leverage×tf leaf) without N-way joins on every page load.
_SCHEMA_VERSION = 6

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


def _migration_v6(conn: sqlite3.Connection) -> None:
    """v5 → v6: Per-(window,fee) facets cache (task #133).

    Adds a `facets_json` TEXT column to `strategy_versions`. The column stores
    a flat dict keyed by `"{window_label}::{fee_profile}"` where each value is
    the best-cell payload (return_pct, trades, maxdd_pct, calmar, sharpe,
    sane, warning, updated_at) selected from the matrix_df for that cell
    group. Enables the hierarchical Strategies page to render:

        Layer 1 (filter)  — fee profile dropdown
        Layer 2 (🏆 hero)  — window time-frame, picked across all strategies
        Layer 3 (⭐ hero)  — strategy + leverage_mode, picked across leverage×tf
        Layer 4 (leaf)    — (leverage, candle_tf) rows with vanity warnings

    Design rationale:
        - Flat key structure so adding a new window or fee profile is a plain
          dict insert — zero schema churn.
        - Values embed `sane` + `warning` so the UI renders vanity badges
          without re-computing from matrix_df on every page load.
        - `updated_at` per-facet tracks keep-best provenance across re-runs.

    Backwards-compat: existing rows get NULL, which `_parse_json(s, {})`
    turns into an empty dict on read. Old code paths are unaffected.
    """
    if not _column_exists(conn, "strategy_versions", "facets_json"):
        conn.execute("ALTER TABLE strategy_versions ADD COLUMN facets_json TEXT")


_MIGRATIONS: dict[int, "callable"] = {
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
    6: _migration_v6,
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
    # Schema v6 (task #133): per-(window,fee) facets cache for the
    # hierarchical Strategies page. Flat dict keyed by
    # `"{window_label}::{fee_profile}"`. Empty dict when the row has never
    # seen a deep_backtest run (parent metadata-only rows from CLI register).
    # Each value is a dict with {return_pct, maxdd_pct, calmar, trades, sane,
    # warning, updated_at, ...}.
    facets: dict[str, dict] = field(default_factory=dict)
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
        # Schema v6 — per-(window,fee) facets cache
        "facets_json": json.dumps(v.facets) if v.facets else None,
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
        facets=_parse_json(_opt("facets_json"), {}) or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Version slug generation ─────────────────────────────────────────────


def _generate_version_slug(
    leverage_mode: str | None,
    baseline_leverage: float | None,
    timeframe: str | None,
    params: dict | None = None,  # kept for API compat (unused)
    existing_versions: list["StoredVersion"] | None = None,  # kept for API compat (unused)
) -> str:
    """Deterministic pure-triplet slug: `{mode}_L{int(baseline)}_{tf}`.

    Task #133 (2026-04-15): pure triplet — no params-hash suffix. Same
    `(mode, leverage, tf)` → same slug → upsert-in-place. Re-tuning params
    updates the same row (with keep-best semantics applied to top-level
    metrics) instead of spawning a new hashed variant.

    Rationale: Prince explicitly asked for "no duplicates by window". The
    former hash-suffix design (v1) created one row per params-hash, which
    turned a single `deep_backtest` run with a few tweaks into an
    ever-growing forest of near-duplicate slugs cluttering the Strategies
    page. The pure-triplet design keeps the storage key aligned with the
    dimensions the user actually thinks in: "margin_capped at L10 on 1h".

    Examples:
        - `margin_capped_L10_1h`
        - `invariant_L15_5m`
        - `risk_scaled_L20_15m`
        - `kelly_fractional_L10_30m`
        - `1h` (pre-leverage-mode legacy; mode+leverage None)

    The `params` and `existing_versions` kwargs are preserved for backwards
    compatibility with earlier call sites and tests. They are ignored.
    """
    parts: list[str] = []
    if leverage_mode:
        parts.append(leverage_mode)
    if baseline_leverage is not None:
        parts.append(f"L{int(baseline_leverage)}")
    if timeframe:
        parts.append(timeframe)
    return "_".join(parts) if parts else "default"


def _generate_description(
    leverage_mode: str | None,
    baseline_leverage: float | None,
    timeframe: str | None,
) -> str:
    """Human-readable version description auto-generated from config triplet.

    Examples:
        - `("margin_capped", 10, "1h")` → `"Margin Capped @ L10 · 1h"`
        - `("invariant", 1, "5m")`      → `"Invariant @ L1 · 5m"`
        - `("risk_scaled", 15, "15m")`  → `"Risk Scaled @ L15 · 15m"`
        - `(None, None, "1h")`          → `"1h"`  (legacy / pre-leverage-mode)

    Task #133: replaces the former docstring-derived description so version
    rows always have a meaningful human label regardless of whether the
    strategy class had a useful __doc__ first line.
    """
    if not leverage_mode and baseline_leverage is None and timeframe:
        return timeframe
    mode_label = (leverage_mode or "").replace("_", " ").title()
    lev_label = f"L{int(baseline_leverage)}" if baseline_leverage is not None else ""
    tf_label = timeframe or ""

    pieces: list[str] = []
    if mode_label:
        pieces.append(mode_label)
    if lev_label:
        pieces.append(f"@ {lev_label}")
    if tf_label:
        pieces.append(f"· {tf_label}")
    return " ".join(pieces) if pieces else "default"


# ── Keep-best metric comparator + facets upsert (schema v6) ─────────────


def _is_better_version_metric(new: dict, existing: dict | None) -> bool:
    """Return True when `new` should replace `existing` under keep-best.

    Tiebreak order (task #133, answered question 4 = (a) sane-first):
        1. SANE wins over non-sane. A sane cell is ALWAYS preferred over an
           insane one regardless of headline return, because an insane cell
           with +340% on 4 trades and 89% DD is a vanity trap — shipping it
           to the Strategies hero slot would mislead.
        2. WF Calmar (continuous) wins when both rows have it. Walk-forward
           is the OOS truth; in-sample return is marketing.
        3. max_return_pct breaks the tie when neither WF Calmar is available
           (or both are None). This is the explicit user-ask metric.

    Both arguments are facet dicts (per-(window,fee) payloads). `existing`
    may be None (first write for this key) — always True.
    """
    if existing is None:
        return True

    new_sane = bool(new.get("sane"))
    old_sane = bool(existing.get("sane"))
    if new_sane != old_sane:
        return new_sane  # sane beats non-sane regardless of return

    new_wf = new.get("wf_calmar")
    old_wf = existing.get("wf_calmar")
    if new_wf is not None and old_wf is not None:
        return float(new_wf) > float(old_wf)
    if new_wf is not None and old_wf is None:
        return True  # WF data is strictly more trustworthy than no-WF
    if new_wf is None and old_wf is not None:
        return False

    new_ret = new.get("return_pct")
    old_ret = existing.get("return_pct")
    if new_ret is None:
        return False
    if old_ret is None:
        return True
    return float(new_ret) > float(old_ret)


def _compute_facets_from_matrix(
    matrix_df: Any,
    *,
    wf_calmar: float | None = None,
    verdict: str | None = None,
    verdict_reason: str | None = None,
    report_dir: str | None = None,
    report_html_path: str | None = None,
    ts: str | None = None,
) -> dict[str, dict]:
    """Group matrix rows by `(window_label, fee_profile)`, pick best-sane cell.

    Produces the facets dict that gets merged into existing `facets_json` by
    `_merge_facets`. Each facet value encodes everything the dashboard needs
    to render a Layer 4 leaf + Layer 2/3 hero picks:

        {
            "window_days": int,
            "window_label": str,
            "fee_profile": str,
            "trades": int,
            "return_pct": float,
            "maxdd_pct": float,
            "calmar": float,
            "sharpe": float,
            "win_rate": float,
            "profit_factor": float,
            "sane": bool,
            "warning": str,              # empty when sane
            "wf_calmar": float | None,   # denormalized from DeepBacktestResult
            "updated_at": str,           # ISO timestamp of the write
        }

    Sanity wins over headline return within each group: we prefer a 12% sane
    cell over a 340% vanity cell for the SAME (window, fee). If ALL cells in
    a group are insane, we still pick the max-return one so the UI has
    something to show (with the warning badge surfacing the risk).
    """
    # Lazy-import the sanity helper to avoid any circular dependency risk.
    from src.backtest.deep_backtest import _is_max_return_sane

    if matrix_df is None or getattr(matrix_df, "empty", True):
        return {}

    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    facets: dict[str, dict] = {}

    # Drop all-NaN return rows (engine error cells)
    clean = matrix_df.dropna(subset=["return_pct"])
    if clean.empty:
        return {}

    # Group by (window_label, fee_profile) — the two dimensions that anchor
    # the hierarchical Strategies page. Leverage and timeframe are INSIDE
    # the group because they distinguish Layer 4 leaves.
    grouped = clean.groupby(["window_label", "fee_profile"])
    for (window_label, fee_profile), grp in grouped:
        # Sort by sanity-first then return: the top row is the keep-best
        # winner for this (window, fee) group.
        rows = [dict(r._asdict()) if hasattr(r, "_asdict") else dict(r)
                for _, r in grp.iterrows()]
        best_row: dict | None = None
        for r in rows:
            candidate = {
                "window_days": int(r["window_days"]),
                "window_label": str(r["window_label"]),
                "fee_profile": str(r["fee_profile"]),
                "timeframe": str(r["timeframe"]),
                "leverage": float(r["leverage"]),
                "trades": int(r["trades"]),
                "return_pct": float(r["return_pct"]),
                "maxdd_pct": float(r["maxdd_pct"]),
                "calmar": float(r["calmar"]),
                "sharpe": float(r["sharpe"]),
                "win_rate": float(r["win_rate"]),
                "profit_factor": float(r["profit_factor"]),
            }
            sane_flag, warning = _is_max_return_sane(candidate)
            candidate["sane"] = sane_flag
            candidate["warning"] = warning or ""
            candidate["wf_calmar"] = wf_calmar
            # Facet provenance — carries the run-level context of the cell
            # so the top-level hero picker never drifts from the cell's
            # original verdict/report when a later worse-params re-run
            # overwrites the row with stale context.
            candidate["verdict"] = verdict
            candidate["verdict_reason"] = verdict_reason
            candidate["report_dir"] = report_dir
            candidate["report_html_path"] = report_html_path
            candidate["updated_at"] = ts
            if _is_better_version_metric(candidate, best_row):
                best_row = candidate
        if best_row is not None:
            key = f"{window_label}::{fee_profile}"
            facets[key] = best_row

    return facets


def _merge_facets(
    existing: dict[str, dict],
    new: dict[str, dict],
) -> dict[str, dict]:
    """Merge new facets into existing under keep-best semantics (per key).

    Non-destructive: keys present only in `existing` survive untouched.
    Keys present only in `new` get added. Keys in both get compared via
    `_is_better_version_metric` and the winner is kept.

    This is the core "same config re-run updates in place, with keep-best"
    behavior that task #133 MUST preserve.
    """
    merged: dict[str, dict] = dict(existing or {})
    for key, new_facet in (new or {}).items():
        old_facet = merged.get(key)
        if _is_better_version_metric(new_facet, old_facet):
            merged[key] = new_facet
    return merged


def _top_level_from_facets(facets: dict[str, dict]) -> dict[str, Any]:
    """Pick the hero facet across ALL windows+fees to populate top-level metrics.

    Returns a dict with keys suitable for splatting into a StoredVersion
    update: max_return_pct, max_return_cell, max_return_sane,
    max_return_warning, best_calmar, best_calmar_cell,
    best_calmar_return_pct.

    Sanity-first + WF-Calmar-first + max-return-fallback, same as
    `_is_better_version_metric`. This function is the SINGLE call site for
    "given a facets dict, which cell is the overall winner?". The dashboard
    reads top-level columns directly (no JSON parsing) for the headline
    rollup, so the write path populates them here.
    """
    if not facets:
        return {}
    hero: dict | None = None
    for facet in facets.values():
        if _is_better_version_metric(facet, hero):
            hero = facet
    if hero is None:
        return {}
    return {
        "max_return_pct": hero.get("return_pct"),
        "max_return_cell": {
            k: hero[k]
            for k in ("window_days", "window_label", "timeframe", "leverage",
                      "fee_profile", "trades", "return_pct", "maxdd_pct",
                      "calmar", "sharpe", "win_rate", "profit_factor")
            if k in hero
        },
        "max_return_sane": hero.get("sane"),
        "max_return_warning": hero.get("warning") or None,
        "best_calmar": hero.get("calmar"),
        "best_calmar_return_pct": hero.get("return_pct"),
        # Hero-cell provenance — verdict + report paths come from the
        # winning cell's original run, not the latest re-run. This keeps
        # keep-best semantics end-to-end: if a later worse-params run
        # overwrites the row, the kept facet's verdict survives.
        "verdict": hero.get("verdict"),
        "verdict_reason": hero.get("verdict_reason"),
        "report_dir": hero.get("report_dir"),
        "report_html_path": hero.get("report_html_path"),
    }


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
                    wf_profitable_folds, backtest_run_id, facets_json
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
                    :wf_profitable_folds, :backtest_run_id, :facets_json
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
                    facets_json              = COALESCE(excluded.facets_json, facets_json),
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


def list_recent_runs(
    *,
    limit: int = 20,
    cutoff_days: int | None = 30,
    db_path: str | None = None,
) -> list[dict]:
    """Global 'most recent deep_backtest runs across ALL strategies' query.

    Task #141.2 — powers the Run Deep Backtest page 7 History section.
    Returns the most recent N rows from `strategy_version_runs` joined
    with parent strategy + version metadata, ordered by run_timestamp
    DESC. Optionally filters out runs older than `cutoff_days` days.

    Args:
        limit: max rows to return (default 20, enough for a page of
            history without overwhelming the UI or the DB).
        cutoff_days: filter out runs older than N days. `None` = no
            cutoff. Default 30 — a month-old run is usually not what
            the user wants to see in "recent history".
        db_path: optional DB override for test isolation.

    Returns:
        List of dicts (not dataclasses — history rows are audit records,
        not mutable entities). Each dict has:
          - run_timestamp: ISO-8601 string
          - strategy_name, version_slug, description
          - leverage_mode, baseline_leverage, timeframe
          - verdict, max_return_pct, max_return_sane, best_calmar
          - matrix_n_cells, report_dir, report_html_path
          - version_id (for deep-linking to the Strategies page)
    """
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT r.id AS run_id, r.version_id, r.run_timestamp, "
            "       r.report_dir AS run_report_dir, "
            "       r.report_html_path AS run_report_html_path, "
            "       r.verdict AS run_verdict, "
            "       r.max_return_pct AS run_max_return_pct, "
            "       r.max_return_sane AS run_max_return_sane, "
            "       r.best_calmar AS run_best_calmar, "
            "       r.matrix_n_cells, "
            "       s.name AS strategy_name, "
            "       v.version_slug, v.description, v.leverage_mode, "
            "       v.baseline_leverage, v.timeframe, "
            "       v.report_html_path AS version_report_html_path "
            "FROM strategy_version_runs r "
            "JOIN strategy_versions v ON v.id = r.version_id "
            "JOIN strategies s ON s.id = v.strategy_id"
        )
        params: list[Any] = []
        if cutoff_days is not None:
            sql += " WHERE datetime(r.run_timestamp) >= datetime('now', ?)"
            params.append(f"-{int(cutoff_days)} days")
        sql += f" ORDER BY datetime(r.run_timestamp) DESC, r.id DESC LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for row in rows:
            d = dict(row)
            # Normalize max_return_sane to bool
            if d.get("run_max_return_sane") is not None:
                d["run_max_return_sane"] = bool(d["run_max_return_sane"])
            # Prefer the run's stored report path over the version's
            # current path (the version's top-level report_html_path
            # tracks the HERO cell via keep-best, not the latest run).
            d["report_html_path"] = (
                d.get("run_report_html_path") or d.get("version_report_html_path")
            )
            out.append(d)
        return out
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

    Task #133 rewrite — facets-aware, keep-best, pure-triplet slug:

      1. Resolve `(leverage_mode, baseline_leverage, timeframe)` from the
         config. These form the pure-triplet slug — same config re-run hits
         the same row every time.
      2. Compute a new facets dict from `result.matrix_df`, grouped by
         `(window_label, fee_profile)`, sanity-first + return-ranked within
         each group. Each facet embeds `wf_calmar` (from DeepBacktestResult.
         walk_forward.continuous_calmar) so the Strategies hero picker has
         OOS truth available without a second query.
      3. Read the existing row (if any) and merge facets under keep-best:
         sane-first → WF Calmar → max_return_pct. Keys present only in
         existing survive untouched (cross-window persistence across re-runs).
      4. Pick top-level hero across all (window,fee) facets to populate
         `max_return_pct`, `max_return_cell`, etc., using the SAME comparator
         so the top-level and facets payloads are always consistent.
      5. Auto-generate description from the triplet
         (`"Margin Capped @ L10 · 1h"`) replacing the former docstring-derived
         description which was often empty or stale.
      6. Write. Append a run history row to `strategy_version_runs`.

    The facets merge is monotonic across re-runs: a single row accumulates
    one facet per (window, fee) cell the user has ever tested against it,
    always holding the best result across runs. This is the "organised" +
    "new heroes without hindering backtesting" behavior Prince explicitly
    asked for — the write path itself runs a full hero recompute and the
    Dashboard just reads the precomputed columns.

    Returns (strategy_id, version_id).
    """
    strategy_name, inst = _resolve_strategy(strategy)

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

    # Parent description: still prefer the strategy __doc__ first line so
    # families surfaced on the rollup table retain their human-written
    # summary. Per-version description comes from the triplet (below).
    parent_description = None
    if inst is not None and getattr(inst, "__doc__", None):
        doc = (type(inst).__doc__ or "").strip()
        if doc:
            parent_description = doc.split("\n", 1)[0].strip()

    parent = StoredStrategy(
        name=strategy_name,
        display_name=strategy_name,
        description=parent_description,
        base_class=base_class,
        tier=tier,
        markets=markets,
        default_timeframe=default_tf,
        leverage_range=lev_range,
        status="researching",
    )
    strategy_id = upsert_strategy(parent, db_path=db_path)

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

    # Walk-forward denormalization (schema v2) — computed first so it can
    # flow into the facets as `wf_calmar`.
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

    # Determine the slug. Honor `config.version_slug` if set (task #120
    # `--version-slug` CLI override). Otherwise auto-generate from the pure
    # triplet — no params hash, no collision fallback.
    explicit_slug = getattr(config, "version_slug", None) if config is not None else None
    if explicit_slug:
        slug = explicit_slug
    else:
        slug = _generate_version_slug(
            leverage_mode=lev_mode_val,
            baseline_leverage=baseline_leverage,
            timeframe=timeframe,
        )

    # Auto-generated description from the triplet. Always set — overwrites
    # any previous description on re-run (semantic is stable by design).
    description = _generate_description(
        leverage_mode=lev_mode_val,
        baseline_leverage=baseline_leverage,
        timeframe=timeframe,
    )

    # Read the existing version row (if any) to merge facets under keep-best.
    # First-write path: `existing_version` is None, existing_facets is {}.
    existing_version = get_version(
        strategy_name=strategy_name,
        version_slug=slug,
        db_path=db_path,
    )
    existing_facets: dict[str, dict] = (
        existing_version.facets if existing_version is not None else {}
    )

    # Paths (current run) — store relative to repo root. Embedded in each
    # facet so the hero cell's report_html_path travels with the winning cell.
    current_report_dir = getattr(result, "report_dir", None)
    current_report_dir_rel = _to_relative(current_report_dir) if current_report_dir is not None else None
    current_html_path_rel = None
    current_summary_path_rel = None
    if current_report_dir is not None:
        _rdp = Path(current_report_dir)
        _hp = _rdp / "index.html"
        _sp = _rdp / "summary.json"
        if _hp.exists():
            current_html_path_rel = _to_relative(_hp)
        if _sp.exists():
            current_summary_path_rel = _to_relative(_sp)

    # Compute new facets from matrix_df — each facet is stamped with the
    # CURRENT run's verdict + report paths. When keep-best later picks a
    # cell from a previous run, that cell's original verdict + report
    # travel with it via `_top_level_from_facets`.
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    current_verdict = getattr(result, "verdict", None)
    current_verdict_reason = getattr(result, "verdict_reason", None)
    new_facets = _compute_facets_from_matrix(
        getattr(result, "matrix_df", None),
        wf_calmar=wf_calmar,
        verdict=current_verdict,
        verdict_reason=current_verdict_reason,
        report_dir=current_report_dir_rel,
        report_html_path=current_html_path_rel,
        ts=ts,
    )

    # Keep-best merge: per (window, fee), pick the better facet.
    merged_facets = _merge_facets(existing_facets, new_facets)

    # Top-level hero across the merged facets (sane-first → WF → return).
    # verdict + report paths come from the winning cell's provenance, so a
    # later worse-params re-run does NOT stomp the good row with stale
    # verdict or a broken report path.
    top_level = _top_level_from_facets(merged_facets)

    version = StoredVersion(
        strategy_id=strategy_id,
        version_slug=slug,
        description=description,
        params=params,
        leverage_mode=lev_mode_val,
        baseline_leverage=baseline_leverage,
        timeframe=timeframe,
        last_backtested_at=ts,
        report_dir=top_level.get("report_dir") or current_report_dir_rel,
        report_html_path=top_level.get("report_html_path") or current_html_path_rel,
        summary_json_path=current_summary_path_rel,
        verdict=top_level.get("verdict") or current_verdict,
        verdict_reason=top_level.get("verdict_reason") or current_verdict_reason,
        matrix_n_cells=len(getattr(result, "matrix", []) or []),
        max_return_pct=top_level.get("max_return_pct"),
        max_return_cell=top_level.get("max_return_cell"),
        max_return_sane=top_level.get("max_return_sane"),
        max_return_warning=top_level.get("max_return_warning"),
        best_calmar=top_level.get("best_calmar"),
        best_calmar_return_pct=top_level.get("best_calmar_return_pct"),
        best_calmar_cell=top_level.get("max_return_cell"),
        wf_continuous_return_pct=wf_ret,
        wf_continuous_dd_pct=wf_dd,
        wf_continuous_calmar=wf_calmar,
        wf_gate_passed=wf_gate,
        wf_n_folds=wf_nf,
        wf_profitable_folds=wf_prof,
        facets=merged_facets,
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
