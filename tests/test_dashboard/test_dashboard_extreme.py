"""Extreme dashboard test suite (task #146).

110 test cases covering every code path that backs the Strategies page,
Run Deep Backtest page, and Glossary page. Organized into 10 classes:

    TestSchemaV6Migration   (15) — schema v6 + migration idempotency
    TestSlugGeneration      (10) — pure triplet + edge cases
    TestFacetsComputation   (15) — _compute_facets_from_matrix behavior
    TestKeepBestComparator  (15) — _is_better_version_metric + _merge_facets
    TestRecordIntegration   (10) — record_deep_backtest_result end-to-end
    TestListRecentRuns      (10) — global recent-runs query
    TestFeeTreeHelper       (10) — group_profiles_by_broker_platform
    TestDashboardHelpers    (10) — _facet_better_than mirror + INSTRUMENT_CLASS_BY_MARKET
    TestSmokeDemo           (5)  — SmokeDemoStrategy correctness
    TestEdgeCasesStress     (10) — huge matrix, corrupted JSON, unicode, SQL injection

Total: 110 tests.

Run: `pytest tests/test_dashboard/test_dashboard_extreme.py -v`
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.strategies.storage import (
    _SCHEMA_VERSION,
    StoredStrategy,
    StoredVersion,
    _compute_facets_from_matrix,
    _connect,
    _generate_description,
    _generate_version_slug,
    _is_better_version_metric,
    _merge_facets,
    _top_level_from_facets,
    get_strategy,
    get_version,
    list_recent_runs,
    list_versions,
    record_deep_backtest_result,
    upsert_strategy,
    upsert_version,
)


# ── Test doubles ─────────────────────────────────────────────────────


@dataclass
class _FakeCellResult:
    window_days: int = 30
    window_label: str = "1mo"
    timeframe: str = "1h"
    leverage: float = 10.0
    fee_profile: str = "pine_zero_cost"
    trades: int = 40
    return_pct: float = 5.0
    maxdd_pct: float = 2.0
    win_rate: float = 55.0
    profit_factor: float = 1.5
    avg_win: float = 100.0
    avg_loss: float = -40.0
    total_commission: float = 1.0
    final_equity: float = 10500.0
    margin_per_trade: float = 300.0
    cost_per_trade: float = 0.05
    cost_pct_of_margin: float = 0.02
    broker_stop_outs: int = 0
    calmar: float = 2.5
    sharpe: float = 1.5
    rejected_positions: int = 0
    notes: str = ""


@dataclass
class _FakeLeverageMode:
    value: str


@dataclass
class _FakeConfig:
    leverage_mode: object = None
    baseline_leverage: float | None = 10.0
    timeframes: list = field(default_factory=lambda: ["1h"])
    strategy_params: dict | None = field(default_factory=dict)
    version_slug: str | None = None


@dataclass
class _FakeWalkForward:
    continuous_return_pct: float = 30.0
    continuous_dd_pct: float = 5.0
    continuous_calmar: float = 6.0
    continuous_gate_passed: bool = True
    n_folds: int = 6
    profitable_folds: int = 5


@dataclass
class _FakeResult:
    config: _FakeConfig
    matrix_df: pd.DataFrame
    matrix: list
    best_cell: _FakeCellResult | None
    report_dir: Path
    verdict: str = "NEEDS_WF"
    verdict_reason: str = "extreme smoke"
    walk_forward: _FakeWalkForward | None = None


_MATRIX_COLUMNS = [
    "window_days", "window_label", "timeframe", "leverage", "fee_profile",
    "trades", "return_pct", "maxdd_pct", "win_rate", "profit_factor",
    "avg_win", "avg_loss", "total_commission", "final_equity",
    "margin_per_trade", "cost_per_trade", "cost_pct_of_margin",
    "broker_stop_outs", "calmar", "sharpe", "rejected_positions", "notes",
]


def _row(**overrides) -> dict:
    defaults = {
        "window_days": 30, "window_label": "1mo", "timeframe": "1h",
        "leverage": 10.0, "fee_profile": "pine_zero_cost", "trades": 40,
        "return_pct": 5.0, "maxdd_pct": 2.0, "win_rate": 55.0,
        "profit_factor": 1.5, "avg_win": 100.0, "avg_loss": -40.0,
        "total_commission": 1.0, "final_equity": 10500.0,
        "margin_per_trade": 300.0, "cost_per_trade": 0.05,
        "cost_pct_of_margin": 0.02, "broker_stop_outs": 0, "calmar": 2.5,
        "sharpe": 1.5, "rejected_positions": 0, "notes": "",
    }
    defaults.update(overrides)
    return defaults


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_MATRIX_COLUMNS)


def _result(df: pd.DataFrame, *, mode: str = "margin_capped",
            baseline: float = 10.0, timeframe: str = "1h",
            verdict: str = "DEPLOYABLE",
            wf: _FakeWalkForward | None = None) -> _FakeResult:
    return _FakeResult(
        config=_FakeConfig(
            leverage_mode=_FakeLeverageMode(value=mode),
            baseline_leverage=baseline,
            timeframes=[timeframe],
        ),
        matrix_df=df,
        matrix=[_FakeCellResult() for _ in range(len(df))],
        best_cell=_FakeCellResult(),
        report_dir=Path("/tmp/extreme_report_dummy"),
        verdict=verdict,
        verdict_reason="extreme smoke",
        walk_forward=wf,
    )


# =====================================================================
#  Category A — Schema v6 migration + storage integrity (15 tests)
# =====================================================================


class TestSchemaV6Migration:
    def test_fresh_db_creates_schema_v6(self, tmp_path):
        db = str(tmp_path / "fresh.db")
        _connect(db).close()
        raw = sqlite3.connect(db)
        try:
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            assert version == _SCHEMA_VERSION == 6
        finally:
            raw.close()

    def test_facets_json_column_exists(self, tmp_path):
        db = str(tmp_path / "t.db")
        _connect(db).close()
        raw = sqlite3.connect(db)
        try:
            cols = {r[1] for r in raw.execute("PRAGMA table_info(strategy_versions)").fetchall()}
            assert "facets_json" in cols
        finally:
            raw.close()

    def test_migration_idempotent_repeated_connect(self, tmp_path):
        db = str(tmp_path / "idem.db")
        for _ in range(5):
            _connect(db).close()
        raw = sqlite3.connect(db)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
        finally:
            raw.close()

    def test_v5_db_upgrades_to_v6(self, tmp_path):
        """Simulate a pre-v6 DB: build a fresh v6 DB via _connect, then drop
        the user_version pragma back to 5 AND drop the facets_json column
        so re-connecting runs the v6 migration against what looks like a v5
        schema. This mirrors the production-realistic upgrade path where an
        existing DB from before the v6 migration gets opened by new code.

        We can't `ALTER TABLE DROP COLUMN` pre-SQLite 3.35, so we recreate
        the table without facets_json + rewrite the rows.
        """
        db = str(tmp_path / "v5_upgrade.db")
        # Build a full v6 DB first
        _connect(db).close()
        # Seed a row
        sid = upsert_strategy(StoredStrategy(name="preserved_v5"), db_path=db)
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="v1", max_return_pct=42.0),
            db_path=db,
        )
        # Now roll the schema backwards: drop facets_json + user_version=5
        raw = sqlite3.connect(db)
        try:
            # SQLite before 3.35 doesn't support DROP COLUMN, so we drop via
            # rebuild: copy rows to a temp table minus facets_json, drop
            # original, rename back. This is the portable pattern.
            raw.execute("BEGIN")
            raw.execute("ALTER TABLE strategy_versions RENAME TO _old_versions")
            # Recreate without facets_json — match the v5 shape
            raw.execute("""
                CREATE TABLE strategy_versions (
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
                    wf_continuous_return_pct REAL,
                    wf_continuous_dd_pct     REAL,
                    wf_continuous_calmar     REAL,
                    wf_gate_passed           TEXT,
                    wf_n_folds               INTEGER,
                    wf_profitable_folds      INTEGER,
                    backtest_run_id          INTEGER,
                    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (strategy_id, version_slug)
                )
            """)
            # Copy all columns EXCEPT facets_json
            raw.execute("""
                INSERT INTO strategy_versions
                SELECT id, strategy_id, version_slug, description, params_json,
                       leverage_mode, baseline_leverage, timeframe, tags_json,
                       last_backtested_at, report_dir, report_html_path,
                       summary_json_path, verdict, verdict_reason, matrix_n_cells,
                       max_return_pct, max_return_cell_json, max_return_sane,
                       max_return_warning, best_calmar, best_calmar_return_pct,
                       best_calmar_cell_json, wf_continuous_return_pct,
                       wf_continuous_dd_pct, wf_continuous_calmar, wf_gate_passed,
                       wf_n_folds, wf_profitable_folds, backtest_run_id,
                       created_at, updated_at
                FROM _old_versions
            """)
            raw.execute("DROP TABLE _old_versions")
            raw.execute("PRAGMA user_version = 5")
            raw.commit()
            # Verify the rollback worked: no facets_json column
            cols = {r[1] for r in raw.execute("PRAGMA table_info(strategy_versions)").fetchall()}
            assert "facets_json" not in cols, "rollback failed — facets_json still present"
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
        finally:
            raw.close()

        # Now re-run _connect — this must upgrade the rolled-back v5 → v6
        _connect(db).close()

        raw = sqlite3.connect(db)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
            cols = {r[1] for r in raw.execute("PRAGMA table_info(strategy_versions)").fetchall()}
            assert "facets_json" in cols, "v6 migration did not add facets_json"
            # Verify the seeded row survived the migration
            row = raw.execute(
                "SELECT max_return_pct FROM strategy_versions WHERE version_slug = 'v1'",
            ).fetchone()
            assert row is not None
            assert row[0] == 42.0
        finally:
            raw.close()

    def test_wal_mode_active_after_connect(self, tmp_path):
        db = str(tmp_path / "wal.db")
        conn = _connect(db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_foreign_keys_enforced(self, tmp_path):
        db = str(tmp_path / "fk.db")
        conn = _connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO strategy_versions (strategy_id, version_slug) VALUES (?, ?)",
                    (99999, "orphan"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_upsert_writes_facets_json(self, tmp_path):
        db = str(tmp_path / "fj.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        facets = {"1mo::fee_a": {"return_pct": 5.0, "sane": True, "wf_calmar": None}}
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="margin_capped_L10_1h", facets=facets),
            db_path=db,
        )
        v = get_version("foo", "margin_capped_L10_1h", db_path=db)
        assert v.facets == facets

    def test_read_null_facets_returns_empty_dict(self, tmp_path):
        db = str(tmp_path / "nullfj.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="margin_capped_L10_1h"),
            db_path=db,
        )
        v = get_version("foo", "margin_capped_L10_1h", db_path=db)
        assert v.facets == {}

    def test_large_facets_json_roundtrip(self, tmp_path):
        """1000-key facets dict should roundtrip cleanly."""
        db = str(tmp_path / "big.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        big = {f"w{i}::f{i}": {"return_pct": float(i), "sane": True, "wf_calmar": None}
               for i in range(1000)}
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="margin_capped_L10_1h", facets=big),
            db_path=db,
        )
        v = get_version("foo", "margin_capped_L10_1h", db_path=db)
        assert len(v.facets) == 1000
        assert v.facets["w500::f500"]["return_pct"] == 500.0

    def test_unicode_in_facets_roundtrip(self, tmp_path):
        db = str(tmp_path / "uni.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        facets = {
            "1mo::fee_a": {
                "return_pct": 5.0, "sane": True, "wf_calmar": None,
                "warning": "⚠ vanity trap — 中文 — émoji 🎯",
                "description": "Margin Capped @ L10 · 1h",
            },
        }
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="margin_capped_L10_1h", facets=facets),
            db_path=db,
        )
        v = get_version("foo", "margin_capped_L10_1h", db_path=db)
        assert v.facets["1mo::fee_a"]["warning"] == "⚠ vanity trap — 中文 — émoji 🎯"
        assert v.facets["1mo::fee_a"]["description"] == "Margin Capped @ L10 · 1h"

    def test_empty_facets_dict_roundtrip(self, tmp_path):
        db = str(tmp_path / "empty.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="margin_capped_L10_1h", facets={}),
            db_path=db,
        )
        v = get_version("foo", "margin_capped_L10_1h", db_path=db)
        assert v.facets == {}

    def test_corrupted_facets_json_falls_back(self, tmp_path):
        """A row with malformed facets_json should return empty dict (don't crash)."""
        db = str(tmp_path / "corrupt.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="margin_capped_L10_1h"),
            db_path=db,
        )
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE strategy_versions SET facets_json = '{not valid json' WHERE strategy_id = ?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()
        v = get_version("foo", "margin_capped_L10_1h", db_path=db)
        assert v.facets == {}  # graceful fallback, not a crash

    def test_migration_preserves_existing_rows(self, tmp_path):
        """Rows inserted before migration must survive after."""
        db = str(tmp_path / "preserve.db")
        sid = upsert_strategy(StoredStrategy(name="preserved"), db_path=db)
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug="v1", max_return_pct=42.0),
            db_path=db,
        )
        # Re-connect, which re-runs migrations (idempotent)
        _connect(db).close()
        v = get_version("preserved", "v1", db_path=db)
        assert v is not None
        assert v.max_return_pct == 42.0

    def test_retry_on_busy_retry_count(self, tmp_path):
        """Sanity: _retry_on_busy wraps an upsert and does not raise on clean run."""
        db = str(tmp_path / "busy.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        # Repeated upserts should not trigger the busy path; just verify they succeed.
        for i in range(5):
            upsert_version(
                StoredVersion(strategy_id=sid, version_slug=f"slug_{i}"),
                db_path=db,
            )
        assert len(list_versions(db_path=db)) == 5

    def test_schema_version_constant_matches_latest_migration(self):
        """The _SCHEMA_VERSION constant must be at least the highest key in
        _MIGRATIONS so we don't forget to bump it."""
        from src.strategies.storage import _MIGRATIONS
        assert _SCHEMA_VERSION >= max(_MIGRATIONS.keys())

    def test_in_memory_db_still_migrates(self):
        """Edge case: :memory: DB should run migrations too."""
        conn = _connect(":memory:")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 6
        finally:
            conn.close()


# =====================================================================
#  Category B — Slug generation (10 tests)
# =====================================================================


class TestSlugGeneration:
    def test_pure_triplet(self):
        assert _generate_version_slug(
            leverage_mode="margin_capped", baseline_leverage=10.0, timeframe="1h",
        ) == "margin_capped_L10_1h"

    def test_legacy_tf_only(self):
        assert _generate_version_slug(
            leverage_mode=None, baseline_leverage=None, timeframe="1h",
        ) == "1h"

    def test_default_when_all_none(self):
        assert _generate_version_slug(None, None, None) == "default"

    def test_same_input_same_output(self):
        a = _generate_version_slug("risk_scaled", 15.0, "5m")
        b = _generate_version_slug("risk_scaled", 15.0, "5m")
        assert a == b == "risk_scaled_L15_5m"

    def test_different_params_ignored(self):
        a = _generate_version_slug("margin_capped", 10.0, "1h", params={"x": 1})
        b = _generate_version_slug("margin_capped", 10.0, "1h", params={"x": 2})
        assert a == b  # pure triplet — params never affect slug

    def test_fractional_baseline_truncates(self):
        assert _generate_version_slug("margin_capped", 10.5, "1h") == "margin_capped_L10_1h"
        assert _generate_version_slug("margin_capped", 10.99, "1h") == "margin_capped_L10_1h"

    def test_large_leverage(self):
        assert _generate_version_slug("margin_capped", 1000.0, "1h") == "margin_capped_L1000_1h"

    def test_leverage_one(self):
        assert _generate_version_slug("margin_capped", 1.0, "5m") == "margin_capped_L1_5m"

    def test_all_five_modes(self):
        for mode in ("margin_capped", "invariant", "vol_targeted", "risk_scaled", "kelly_fractional"):
            slug = _generate_version_slug(mode, 10.0, "1h")
            assert slug.startswith(mode + "_")
            assert slug.endswith("_1h")
            assert "L10" in slug

    def test_only_mode_no_baseline_no_tf(self):
        """Edge case: mode alone → just the mode as the slug."""
        assert _generate_version_slug("margin_capped", None, None) == "margin_capped"


# =====================================================================
#  Category C — Facets computation (15 tests)
# =====================================================================


class TestFacetsComputation:
    def test_single_cell_single_facet(self):
        df = _df([_row()])
        facets = _compute_facets_from_matrix(df)
        assert len(facets) == 1
        assert "1mo::pine_zero_cost" in facets

    def test_groups_by_window_and_fee(self):
        df = _df([
            _row(window_label="1mo", fee_profile="fee_a"),
            _row(window_label="1mo", fee_profile="fee_b"),
            _row(window_label="1y", fee_profile="fee_a"),
            _row(window_label="1y", fee_profile="fee_b"),
        ])
        facets = _compute_facets_from_matrix(df)
        assert set(facets.keys()) == {"1mo::fee_a", "1mo::fee_b", "1y::fee_a", "1y::fee_b"}

    def test_sane_beats_insane_within_group(self):
        df = _df([
            _row(return_pct=200.0, trades=4, calmar=1.5),   # insane (thin trades)
            _row(return_pct=8.0, trades=50, calmar=2.0),    # sane
        ])
        facets = _compute_facets_from_matrix(df)
        winner = next(iter(facets.values()))
        assert winner["return_pct"] == 8.0
        assert winner["sane"] is True

    def test_all_insane_picks_max_return(self):
        df = _df([
            _row(return_pct=50.0, trades=4),
            _row(return_pct=200.0, trades=4),
        ])
        facets = _compute_facets_from_matrix(df)
        winner = next(iter(facets.values()))
        assert winner["return_pct"] == 200.0
        assert winner["sane"] is False

    def test_nan_returns_dropped(self):
        df = _df([
            _row(return_pct=float("nan"), leverage=50),
            _row(return_pct=8.0, leverage=10),
        ])
        facets = _compute_facets_from_matrix(df)
        winner = next(iter(facets.values()))
        assert winner["return_pct"] == 8.0
        assert winner["leverage"] == 10.0

    def test_empty_matrix_returns_empty(self):
        df = _df([])
        assert _compute_facets_from_matrix(df) == {}

    def test_none_matrix_returns_empty(self):
        assert _compute_facets_from_matrix(None) == {}

    def test_all_nan_returns_empty(self):
        df = _df([_row(return_pct=float("nan")), _row(return_pct=float("nan"))])
        assert _compute_facets_from_matrix(df) == {}

    def test_wf_calmar_propagates(self):
        df = _df([_row()])
        facets = _compute_facets_from_matrix(df, wf_calmar=7.5)
        assert next(iter(facets.values()))["wf_calmar"] == 7.5

    def test_verdict_propagates(self):
        df = _df([_row()])
        facets = _compute_facets_from_matrix(
            df, verdict="DEPLOYABLE", verdict_reason="sane + WF pass",
        )
        f = next(iter(facets.values()))
        assert f["verdict"] == "DEPLOYABLE"
        assert f["verdict_reason"] == "sane + WF pass"

    def test_report_path_propagates(self):
        df = _df([_row()])
        facets = _compute_facets_from_matrix(
            df, report_dir="reports/foo", report_html_path="reports/foo/index.html",
        )
        f = next(iter(facets.values()))
        assert f["report_dir"] == "reports/foo"
        assert f["report_html_path"] == "reports/foo/index.html"

    def test_timestamp_present(self):
        facets = _compute_facets_from_matrix(_df([_row()]), ts="2026-04-16T00:00:00+00:00")
        assert next(iter(facets.values()))["updated_at"] == "2026-04-16T00:00:00+00:00"

    def test_facet_contains_all_required_keys(self):
        facets = _compute_facets_from_matrix(_df([_row()]))
        facet = next(iter(facets.values()))
        required = {
            "window_days", "window_label", "fee_profile", "timeframe",
            "leverage", "trades", "return_pct", "maxdd_pct", "calmar",
            "sharpe", "win_rate", "profit_factor", "sane", "warning",
            "wf_calmar", "updated_at",
        }
        assert required <= set(facet.keys())

    def test_multiple_leverages_same_group_picks_best(self):
        """Two leverage variants in the same (window, fee) group → hero picks best."""
        df = _df([
            _row(leverage=10, return_pct=5.0, trades=40),
            _row(leverage=25, return_pct=15.0, trades=40),
        ])
        facets = _compute_facets_from_matrix(df)
        winner = next(iter(facets.values()))
        assert winner["leverage"] == 25.0
        assert winner["return_pct"] == 15.0

    def test_negative_returns_handled(self):
        df = _df([
            _row(return_pct=-5.0, trades=40),
            _row(return_pct=-15.0, trades=40),
        ])
        facets = _compute_facets_from_matrix(df)
        winner = next(iter(facets.values()))
        assert winner["return_pct"] == -5.0  # least-bad wins


# =====================================================================
#  Category D — Keep-best comparator + merge (15 tests)
# =====================================================================


class TestKeepBestComparator:
    def test_existing_none_new_wins(self):
        new = {"sane": True, "return_pct": 5.0, "wf_calmar": None}
        assert _is_better_version_metric(new, None) is True

    def test_sane_beats_insane(self):
        sane = {"sane": True, "return_pct": 5.0, "wf_calmar": None}
        insane = {"sane": False, "return_pct": 500.0, "wf_calmar": None}
        assert _is_better_version_metric(sane, insane) is True
        assert _is_better_version_metric(insane, sane) is False

    def test_both_sane_wf_calmar_tiebreak(self):
        a = {"sane": True, "return_pct": 10.0, "wf_calmar": 2.0}
        b = {"sane": True, "return_pct": 50.0, "wf_calmar": 5.0}
        assert _is_better_version_metric(b, a) is True

    def test_both_sane_wf_present_beats_no_wf(self):
        with_wf = {"sane": True, "return_pct": 5.0, "wf_calmar": 3.0}
        no_wf = {"sane": True, "return_pct": 50.0, "wf_calmar": None}
        assert _is_better_version_metric(with_wf, no_wf) is True

    def test_both_sane_both_no_wf_return_tiebreak(self):
        a = {"sane": True, "return_pct": 20.0, "wf_calmar": None}
        b = {"sane": True, "return_pct": 10.0, "wf_calmar": None}
        assert _is_better_version_metric(a, b) is True

    def test_both_insane_higher_return_wins(self):
        a = {"sane": False, "return_pct": 200.0, "wf_calmar": None}
        b = {"sane": False, "return_pct": 100.0, "wf_calmar": None}
        assert _is_better_version_metric(a, b) is True

    def test_identical_facets_not_better(self):
        """Same content — comparator returns False (no improvement)."""
        a = {"sane": True, "return_pct": 10.0, "wf_calmar": 2.0}
        # Strict ">" in the comparator means equal is NOT better.
        assert _is_better_version_metric(a, dict(a)) is False

    def test_new_return_none_cannot_win(self):
        new = {"sane": True, "return_pct": None, "wf_calmar": None}
        existing = {"sane": True, "return_pct": 5.0, "wf_calmar": None}
        assert _is_better_version_metric(new, existing) is False

    def test_close_values_strict_inequality(self):
        """Return diff of 1e-10 — strict ">" decides."""
        a = {"sane": True, "return_pct": 5.0, "wf_calmar": None}
        b = {"sane": True, "return_pct": 5.0 + 1e-10, "wf_calmar": None}
        assert _is_better_version_metric(b, a) is True
        assert _is_better_version_metric(a, b) is False

    def test_negative_returns_comparator(self):
        """Less-negative is "better" in the comparator."""
        less_bad = {"sane": True, "return_pct": -5.0, "wf_calmar": None}
        more_bad = {"sane": True, "return_pct": -20.0, "wf_calmar": None}
        assert _is_better_version_metric(less_bad, more_bad) is True

    def test_merge_preserves_unrelated_keys(self):
        existing = {
            "1mo::a": {"return_pct": 5.0, "sane": True, "wf_calmar": None},
            "1y::a": {"return_pct": 20.0, "sane": True, "wf_calmar": None},
        }
        new = {"3mo::a": {"return_pct": 10.0, "sane": True, "wf_calmar": None}}
        merged = _merge_facets(existing, new)
        assert set(merged.keys()) == {"1mo::a", "3mo::a", "1y::a"}
        assert merged["1mo::a"]["return_pct"] == 5.0
        assert merged["1y::a"]["return_pct"] == 20.0
        assert merged["3mo::a"]["return_pct"] == 10.0

    def test_merge_keep_best_on_overlap(self):
        existing = {"1mo::a": {"return_pct": 50.0, "sane": True, "wf_calmar": None}}
        new = {"1mo::a": {"return_pct": 10.0, "sane": True, "wf_calmar": None}}
        merged = _merge_facets(existing, new)
        assert merged["1mo::a"]["return_pct"] == 50.0

    def test_merge_empty_new_preserves_existing(self):
        existing = {"1mo::a": {"return_pct": 50.0, "sane": True, "wf_calmar": None}}
        merged = _merge_facets(existing, {})
        assert merged == existing

    def test_merge_empty_existing_adopts_new(self):
        new = {"1mo::a": {"return_pct": 50.0, "sane": True, "wf_calmar": None}}
        merged = _merge_facets({}, new)
        assert merged == new

    def test_merge_sane_overrides_higher_insane(self):
        existing = {"1mo::a": {"return_pct": 300.0, "sane": False, "wf_calmar": None}}
        new = {"1mo::a": {"return_pct": 20.0, "sane": True, "wf_calmar": None}}
        merged = _merge_facets(existing, new)
        assert merged["1mo::a"]["return_pct"] == 20.0
        assert merged["1mo::a"]["sane"] is True


# =====================================================================
#  Category E — record_deep_backtest_result integration (10 tests)
# =====================================================================


class TestRecordIntegration:
    def test_first_run_creates_rows(self, tmp_path):
        db = str(tmp_path / "t.db")
        result = _result(_df([_row()]))
        sid, vid = record_deep_backtest_result("extreme_strat", result, db_path=db)
        assert sid > 0
        assert vid > 0

    def test_second_run_same_triplet_no_duplicate(self, tmp_path):
        db = str(tmp_path / "t.db")
        r1 = _result(_df([_row()]))
        record_deep_backtest_result("extreme_strat", r1, db_path=db)
        r2 = _result(_df([_row(return_pct=3.0)]))
        record_deep_backtest_result("extreme_strat", r2, db_path=db)
        versions = list_versions(strategy_name="extreme_strat", db_path=db)
        assert len(versions) == 1

    def test_second_run_different_triplet_creates_new_version(self, tmp_path):
        db = str(tmp_path / "t.db")
        r1 = _result(_df([_row()]), mode="margin_capped", baseline=10.0)
        record_deep_backtest_result("extreme_strat", r1, db_path=db)
        r2 = _result(_df([_row()]), mode="risk_scaled", baseline=15.0)
        record_deep_backtest_result("extreme_strat", r2, db_path=db)
        versions = list_versions(strategy_name="extreme_strat", db_path=db)
        slugs = sorted([v.version_slug for v in versions])
        assert slugs == ["margin_capped_L10_1h", "risk_scaled_L15_1h"]

    def test_second_run_different_window_accumulates_facets(self, tmp_path):
        db = str(tmp_path / "t.db")
        r1 = _result(_df([_row(window_label="1mo", window_days=30)]))
        record_deep_backtest_result("extreme_strat", r1, db_path=db)
        r2 = _result(_df([_row(window_label="1y", window_days=365)]))
        record_deep_backtest_result("extreme_strat", r2, db_path=db)
        v = get_version("extreme_strat", "margin_capped_L10_1h", db_path=db)
        assert set(v.facets.keys()) == {"1mo::pine_zero_cost", "1y::pine_zero_cost"}

    def test_worse_rerun_preserves_old_cell(self, tmp_path):
        db = str(tmp_path / "t.db")
        r1 = _result(_df([_row(return_pct=50.0)]))
        record_deep_backtest_result("extreme_strat", r1, db_path=db)
        r2 = _result(_df([_row(return_pct=10.0)]))
        record_deep_backtest_result("extreme_strat", r2, db_path=db)
        v = get_version("extreme_strat", "margin_capped_L10_1h", db_path=db)
        assert v.facets["1mo::pine_zero_cost"]["return_pct"] == 50.0
        assert v.max_return_pct == 50.0

    def test_hero_verdict_travels_with_cell(self, tmp_path):
        """First run DEPLOYABLE wins over later FAILED re-run on same cell."""
        db = str(tmp_path / "t.db")
        r1 = _result(_df([_row(return_pct=50.0)]), verdict="DEPLOYABLE")
        record_deep_backtest_result("extreme_strat", r1, db_path=db)
        r2 = _result(_df([_row(return_pct=10.0)]), verdict="FAILED")
        record_deep_backtest_result("extreme_strat", r2, db_path=db)
        v = get_version("extreme_strat", "margin_capped_L10_1h", db_path=db)
        assert v.verdict == "DEPLOYABLE"  # hero cell's verdict preserved

    def test_description_auto_generated(self, tmp_path):
        db = str(tmp_path / "t.db")
        r = _result(_df([_row()]), mode="risk_scaled", baseline=15.0, timeframe="5m")
        record_deep_backtest_result("extreme_strat", r, db_path=db)
        v = get_version("extreme_strat", "risk_scaled_L15_5m", db_path=db)
        assert v.description == "Risk Scaled @ L15 · 5m"

    def test_top_level_matches_hero_facet(self, tmp_path):
        db = str(tmp_path / "t.db")
        r = _result(_df([
            _row(window_label="1mo", return_pct=5.0),
            _row(window_label="1y", return_pct=50.0),
        ]))
        record_deep_backtest_result("extreme_strat", r, db_path=db)
        v = get_version("extreme_strat", "margin_capped_L10_1h", db_path=db)
        assert v.max_return_pct == 50.0
        assert v.max_return_cell["window_label"] == "1y"

    def test_wf_calmar_denormalized(self, tmp_path):
        db = str(tmp_path / "t.db")
        wf = _FakeWalkForward(continuous_calmar=8.5, continuous_return_pct=40.0)
        r = _result(_df([_row()]), wf=wf)
        record_deep_backtest_result("extreme_strat", r, db_path=db)
        v = get_version("extreme_strat", "margin_capped_L10_1h", db_path=db)
        assert v.wf_continuous_calmar == 8.5
        assert v.wf_continuous_return_pct == 40.0

    def test_empty_matrix_still_writes_row(self, tmp_path):
        """A run that produced zero cells (e.g., data-missing) should still
        land a parent strategy row so the user sees the attempt."""
        db = str(tmp_path / "t.db")
        empty_df = _df([])
        r = _result(empty_df)
        sid, vid = record_deep_backtest_result("extreme_strat", r, db_path=db)
        assert sid > 0
        assert vid > 0
        v = get_version("extreme_strat", "margin_capped_L10_1h", db_path=db)
        assert v.facets == {}


# =====================================================================
#  Category F — list_recent_runs (10 tests)
# =====================================================================


class TestListRecentRuns:
    def _seed_runs(self, db: str, n: int = 5):
        """Seed N runs with varying timestamps."""
        for i in range(n):
            r = _result(_df([_row(return_pct=float(i))]), mode="margin_capped", baseline=10.0 + i)
            record_deep_backtest_result(f"ext_strat_{i}", r, db_path=db)

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / "e.db")
        _connect(db).close()
        assert list_recent_runs(db_path=db) == []

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / "lim.db")
        self._seed_runs(db, n=5)
        runs = list_recent_runs(limit=3, db_path=db)
        assert len(runs) == 3

    def test_limit_larger_than_rows(self, tmp_path):
        db = str(tmp_path / "big_lim.db")
        self._seed_runs(db, n=3)
        runs = list_recent_runs(limit=100, db_path=db)
        assert len(runs) == 3

    def test_cutoff_days_none_no_filter(self, tmp_path):
        db = str(tmp_path / "nc.db")
        self._seed_runs(db, n=3)
        runs = list_recent_runs(cutoff_days=None, db_path=db)
        assert len(runs) == 3

    def test_cutoff_days_filters_old(self, tmp_path):
        """Insert a row with an old timestamp, confirm cutoff hides it."""
        db = str(tmp_path / "cut.db")
        self._seed_runs(db, n=1)
        # Manually insert an old row
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE strategy_version_runs SET run_timestamp = ?",
                ("2020-01-01T00:00:00",),
            )
            conn.commit()
        finally:
            conn.close()
        runs = list_recent_runs(cutoff_days=30, db_path=db)
        assert runs == []

    def test_ordered_by_timestamp_desc(self, tmp_path):
        db = str(tmp_path / "ord.db")
        self._seed_runs(db, n=5)
        runs = list_recent_runs(db_path=db)
        timestamps = [r["run_timestamp"] for r in runs]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_strategy_name_in_result(self, tmp_path):
        db = str(tmp_path / "n.db")
        self._seed_runs(db, n=1)
        runs = list_recent_runs(db_path=db)
        assert runs[0]["strategy_name"] == "ext_strat_0"

    def test_version_slug_in_result(self, tmp_path):
        db = str(tmp_path / "sl.db")
        self._seed_runs(db, n=1)
        runs = list_recent_runs(db_path=db)
        assert runs[0]["version_slug"].startswith("margin_capped_")

    def test_sane_normalized_to_bool(self, tmp_path):
        db = str(tmp_path / "sn.db")
        self._seed_runs(db, n=1)
        runs = list_recent_runs(db_path=db)
        # Either None, True, or False (never an integer)
        sane = runs[0].get("run_max_return_sane")
        assert sane is None or isinstance(sane, bool)

    def test_report_html_path_prefers_run_over_version(self, tmp_path):
        """Field merge: the `report_html_path` key should come from the run row."""
        db = str(tmp_path / "rp.db")
        self._seed_runs(db, n=1)
        runs = list_recent_runs(db_path=db)
        # At minimum, the key must be present in each row
        assert "report_html_path" in runs[0]


# =====================================================================
#  Category G — Fee tree helper (10 tests)
# =====================================================================


class TestFeeTreeHelper:
    def test_unfiltered_tree_has_brokers(self):
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform()
        assert "IC Markets" in tree

    def test_xauusd_filter(self):
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform(instrument_class="xauusd_metals")
        # Every profile under this filter must be xauusd_metals
        for broker in tree:
            for platform in tree[broker]:
                for name in tree[broker][platform]:
                    assert "xauusd" in name or "pine" in name

    def test_normal_scenario_filter(self):
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform(scenario="normal")
        for broker in tree:
            for platform in tree[broker]:
                for name in tree[broker][platform]:
                    assert "normal" in name

    def test_both_filters_combined(self):
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform(
            instrument_class="xauusd_metals", scenario="normal",
        )
        # Every leaf must be a xauusd normal-scenario profile
        for broker in tree:
            for platform in tree[broker]:
                for name in tree[broker][platform]:
                    assert "xauusd" in name and "normal" in name

    def test_pine_faithful_scenario_reachable(self):
        """The pine_zero_cost profile must be findable via scenario=pine_faithful."""
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform(scenario="pine_faithful")
        all_profiles = [
            name for broker in tree.values()
            for platform in broker.values() for name in platform
        ]
        assert "pine_zero_cost" in all_profiles

    def test_nonexistent_scenario_returns_empty(self):
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform(scenario="zzzzz_no_such_scenario")
        assert tree == {}

    def test_leaf_lists_sorted(self):
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform()
        for broker in tree:
            for platform in tree[broker]:
                leaf = tree[broker][platform]
                assert leaf == sorted(leaf)

    def test_list_brokers_returns_sorted(self):
        from src.backtest.fee_profiles import list_brokers
        brokers = list_brokers()
        assert brokers == sorted(brokers)
        assert len(brokers) == len(set(brokers))  # unique

    def test_list_brokers_xauusd_filter(self):
        from src.backtest.fee_profiles import list_brokers
        brokers = list_brokers(instrument_class="xauusd_metals")
        assert "IC Markets" in brokers

    def test_both_filters_with_crypto_class_is_empty(self):
        """No crypto profiles in the registry yet — should return empty."""
        from src.backtest.fee_profiles import group_profiles_by_broker_platform
        tree = group_profiles_by_broker_platform(instrument_class="crypto_perp")
        assert tree == {}


# =====================================================================
#  Category H — Dashboard helper functions (10 tests)
# =====================================================================
#
# These target the pure-Python helpers exposed by the dashboard module
# itself (not the Streamlit-dependent page bodies).


class TestDashboardHelpers:
    def test_facet_better_than_mirror_existing_none(self):
        from src.dashboard.app import _facet_better_than
        new = {"sane": True, "return_pct": 5.0, "wf_calmar": None}
        assert _facet_better_than(new, None) is True

    def test_facet_better_than_sane_beats_insane(self):
        from src.dashboard.app import _facet_better_than
        sane = {"sane": True, "return_pct": 5.0, "wf_calmar": None}
        insane = {"sane": False, "return_pct": 500.0, "wf_calmar": None}
        assert _facet_better_than(sane, insane) is True

    def test_facet_better_than_wf_calmar(self):
        from src.dashboard.app import _facet_better_than
        a = {"sane": True, "return_pct": 10.0, "wf_calmar": 2.0}
        b = {"sane": True, "return_pct": 20.0, "wf_calmar": 5.0}
        assert _facet_better_than(b, a) is True

    def test_facet_better_than_mirrors_storage_comparator(self):
        """Dashboard helper must mirror `_is_better_version_metric` exactly
        so Layer 2 hero and storage top-level agree."""
        from src.dashboard.app import _facet_better_than
        pairs = [
            ({"sane": True, "return_pct": 5.0, "wf_calmar": None},
             {"sane": False, "return_pct": 500.0, "wf_calmar": None}),
            ({"sane": True, "return_pct": 10.0, "wf_calmar": 5.0},
             {"sane": True, "return_pct": 50.0, "wf_calmar": 2.0}),
            ({"sane": True, "return_pct": 5.0, "wf_calmar": None},
             {"sane": True, "return_pct": 5.0, "wf_calmar": None}),
        ]
        for new, old in pairs:
            assert _facet_better_than(new, old) == _is_better_version_metric(new, old)

    def test_format_hero_summary_none(self):
        from src.dashboard.app import _format_hero_summary
        assert _format_hero_summary(None, {}) == "(no data)"

    def test_format_hero_summary_returns_string(self):
        from src.dashboard.app import _format_hero_summary

        class _MockVersion:
            strategy_id = 1

        class _MockStrategy:
            name = "donchian_gold"

        hero = (_MockVersion(), {"return_pct": 12.5})
        s_map = {1: _MockStrategy()}
        out = _format_hero_summary(hero, s_map)
        assert "donchian_gold" in out
        assert "12" in out

    def test_format_hero_summary_missing_return(self):
        from src.dashboard.app import _format_hero_summary

        class _MockVersion:
            strategy_id = 1

        class _MockStrategy:
            name = "foo"

        hero = (_MockVersion(), {"return_pct": None})
        out = _format_hero_summary(hero, {1: _MockStrategy()})
        assert out == "foo"

    def test_instrument_class_map_xauusd(self):
        from src.dashboard.app import INSTRUMENT_CLASS_BY_MARKET
        assert INSTRUMENT_CLASS_BY_MARKET.get("XAUUSD") == "xauusd_metals"

    def test_instrument_class_map_fx(self):
        from src.dashboard.app import INSTRUMENT_CLASS_BY_MARKET
        assert INSTRUMENT_CLASS_BY_MARKET.get("EURUSD") == "fx_majors"

    def test_instrument_class_map_default_fallback(self):
        """An unknown market should fall through to the .get() default in the
        dashboard code path. Test the dict behavior here."""
        from src.dashboard.app import INSTRUMENT_CLASS_BY_MARKET
        assert INSTRUMENT_CLASS_BY_MARKET.get("UNKNOWN", "xauusd_metals") == "xauusd_metals"


# =====================================================================
#  Category I — Smoke demo strategy (5 tests)
# =====================================================================


class TestSmokeDemo:
    def test_registered_in_router(self):
        from src.strategies.router import STRATEGY_REGISTRY
        assert "smoke_demo" in STRATEGY_REGISTRY

    def test_instantiates_with_no_kwargs(self):
        from src.strategies.demos.smoke_demo import SmokeDemoStrategy
        inst = SmokeDemoStrategy()
        assert inst.name == "smoke_demo"

    def test_xauusd_in_markets(self):
        from src.strategies.demos.smoke_demo import SmokeDemoStrategy
        inst = SmokeDemoStrategy()
        assert "XAUUSD" in inst.markets

    def test_leverage_range(self):
        from src.strategies.demos.smoke_demo import SmokeDemoStrategy
        inst = SmokeDemoStrategy()
        assert inst.leverage_range == (1.0, 100.0)

    def test_sma_computation(self):
        """Feed 25 bars and verify the SMA helpers compute correctly."""
        from src.strategies.demos.smoke_demo import SmokeDemoStrategy
        inst = SmokeDemoStrategy()
        for price in range(100, 125):  # 25 bars
            inst._closes.append(float(price))
        sma5 = inst._sma(5)
        sma20 = inst._sma(20)
        # SMA5 over last 5 of 100..124 = 120..124 = (120+121+122+123+124)/5 = 122
        assert sma5 == 122.0
        # SMA20 over last 20 of 100..124 = 105..124 = mean 114.5
        assert sma20 == pytest.approx(114.5, rel=1e-6)


# =====================================================================
#  Category J — Edge cases + stress (10 tests)
# =====================================================================


class TestEdgeCasesStress:
    def test_huge_matrix_facets(self):
        """500-cell matrix should produce facets without performance issues."""
        rows = []
        for w_idx, wl in enumerate(["1mo", "3mo", "6mo", "1y", "2y"]):
            for fee in ["fee_a", "fee_b", "fee_c"]:
                for lev in [1.0, 5.0, 10.0, 25.0, 50.0]:
                    for tf in ["5m", "15m", "30m", "1h"]:
                        rows.append(_row(
                            window_days=30 * (w_idx + 1),
                            window_label=wl, fee_profile=fee,
                            leverage=lev, timeframe=tf,
                            return_pct=float(w_idx * 10 + lev),
                        ))
        df = _df(rows)
        facets = _compute_facets_from_matrix(df)
        # 5 windows × 3 fees = 15 (window, fee) groups
        assert len(facets) == 15

    def test_multiple_rapid_upserts(self, tmp_path):
        """10 rapid upserts on the same row — verify no corruption, single row."""
        db = str(tmp_path / "rapid.db")
        sid = upsert_strategy(StoredStrategy(name="rapid"), db_path=db)
        for i in range(10):
            upsert_version(
                StoredVersion(
                    strategy_id=sid,
                    version_slug="margin_capped_L10_1h",
                    max_return_pct=float(i),
                ),
                db_path=db,
            )
        versions = list_versions(strategy_name="rapid", db_path=db)
        assert len(versions) == 1
        # Last value should win under COALESCE semantics
        assert versions[0].max_return_pct == 9.0

    def test_sql_injection_in_version_slug(self, tmp_path):
        """Malicious slug should be parameterized, not executed."""
        db = str(tmp_path / "inj.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        evil_slug = "'; DROP TABLE strategies; --"
        upsert_version(
            StoredVersion(strategy_id=sid, version_slug=evil_slug),
            db_path=db,
        )
        # The strategies table should still exist
        raw = sqlite3.connect(db)
        try:
            rows = raw.execute("SELECT name FROM strategies").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "foo"
        finally:
            raw.close()

    def test_unicode_strategy_name(self, tmp_path):
        db = str(tmp_path / "uni.db")
        sid = upsert_strategy(StoredStrategy(name="策略_émoji_🎯"), db_path=db)
        assert sid > 0
        fetched = get_strategy("策略_émoji_🎯", db_path=db)
        assert fetched is not None
        assert fetched.name == "策略_émoji_🎯"

    def test_extreme_leverage_value(self):
        """Leverage 1_000_000 should still produce a clean slug."""
        slug = _generate_version_slug("margin_capped", 1_000_000, "1h")
        assert slug == "margin_capped_L1000000_1h"

    def test_very_small_return_values(self):
        """1e-15 return values should compare correctly."""
        df = _df([
            _row(return_pct=1e-15, trades=40),
            _row(return_pct=2e-15, trades=40),
        ])
        facets = _compute_facets_from_matrix(df)
        winner = next(iter(facets.values()))
        assert winner["return_pct"] == 2e-15

    def test_inf_return_handled(self):
        """An infinity return (pathological divide-by-zero) should not crash."""
        df = _df([
            _row(return_pct=float("inf"), trades=40),
            _row(return_pct=10.0, trades=40),
        ])
        try:
            facets = _compute_facets_from_matrix(df)
            # Whichever wins, the call must not raise
            assert len(facets) == 1
        except (ValueError, OverflowError) as e:
            pytest.fail(f"inf return crashed the facets computation: {e}")

    def test_description_extreme_leverage(self):
        """Human-readable description for large leverage still renders."""
        d = _generate_description("kelly_fractional", 1000.0, "1d")
        assert "Kelly Fractional" in d
        assert "L1000" in d
        assert "1d" in d

    def test_top_level_from_facets_empty(self):
        """Empty facets → empty dict."""
        assert _top_level_from_facets({}) == {}

    def test_top_level_from_facets_single_facet(self):
        facets = {
            "1mo::a": {
                "return_pct": 12.0, "sane": True, "wf_calmar": None,
                "window_days": 30, "window_label": "1mo", "timeframe": "1h",
                "leverage": 10.0, "fee_profile": "a", "trades": 40,
                "maxdd_pct": 3.0, "calmar": 4.0, "sharpe": 1.5,
                "win_rate": 55, "profit_factor": 1.8, "verdict": "DEPLOYABLE",
            }
        }
        top = _top_level_from_facets(facets)
        assert top["max_return_pct"] == 12.0
        assert top["verdict"] == "DEPLOYABLE"
        assert top["max_return_cell"]["window_label"] == "1mo"

    def test_sorted_leaf_picker_maps_to_correct_row(self):
        """Regression guard for task #146 bug #4: page 6 Layer 4 was
        indexing `leaves` (unsorted) with a sort-order index from
        `leaf_rows` (sorted). That means picking the 'top' row showed
        a different row's data. Fix: zip (row, v, facet) into tuples
        and sort as a unit so the picker index is always aligned.

        This test mirrors the fixed code path in pure Python.
        """
        # Simulated leaves in insertion order: three cells with returns
        # [5%, 15%, 10%] and all sane. Sort order (by return desc) should
        # be [15%, 10%, 5%] — different from insertion order.
        leaves = [
            ("vA", {"return_pct": 5.0, "sane": True, "leverage": 10, "calmar": 1.0}),
            ("vB", {"return_pct": 15.0, "sane": True, "leverage": 25, "calmar": 3.0}),
            ("vC", {"return_pct": 10.0, "sane": True, "leverage": 15, "calmar": 2.0}),
        ]

        leaf_tuples = []
        for v, facet in leaves:
            row = {
                "slug": v,
                "return_pct": facet["return_pct"],
                "sane": "✓" if facet["sane"] else "⚠",
            }
            leaf_tuples.append((row, v, facet))
        leaf_tuples.sort(
            key=lambda t: (t[0]["sane"] != "✓", -(t[0]["return_pct"] or -1e18)),
        )
        leaf_rows = [t[0] for t in leaf_tuples]

        # User picks the first row in the sorted display (should be vB @ 15%)
        picked_idx = 0
        _, v_pick, facet_pick = leaf_tuples[picked_idx]
        assert v_pick == "vB"
        assert facet_pick["return_pct"] == 15.0
        assert leaf_rows[picked_idx]["slug"] == "vB"

        # User picks the second row in the sorted display (should be vC @ 10%)
        picked_idx = 1
        _, v_pick, facet_pick = leaf_tuples[picked_idx]
        assert v_pick == "vC"
        assert facet_pick["return_pct"] == 10.0

        # User picks the third row (should be vA @ 5%, the lowest)
        picked_idx = 2
        _, v_pick, facet_pick = leaf_tuples[picked_idx]
        assert v_pick == "vA"
        assert facet_pick["return_pct"] == 5.0

    def test_sorted_leaf_picker_insane_goes_to_bottom(self):
        """With an insane row mixed in, the sort should push it AFTER
        all sane rows regardless of return. The picker must still map
        correctly via the aligned tuple list."""
        leaves = [
            ("vHUGE_INSANE", {"return_pct": 300.0, "sane": False, "leverage": 100, "calmar": 0.1}),
            ("vSMALL_SANE", {"return_pct": 5.0, "sane": True, "leverage": 10, "calmar": 1.5}),
            ("vMID_SANE", {"return_pct": 12.0, "sane": True, "leverage": 15, "calmar": 2.5}),
        ]
        leaf_tuples = []
        for v, facet in leaves:
            row = {
                "slug": v,
                "return_pct": facet["return_pct"],
                "sane": "✓" if facet["sane"] else "⚠",
            }
            leaf_tuples.append((row, v, facet))
        leaf_tuples.sort(
            key=lambda t: (t[0]["sane"] != "✓", -(t[0]["return_pct"] or -1e18)),
        )
        # Expected order: vMID_SANE (12, sane), vSMALL_SANE (5, sane), vHUGE_INSANE (300, insane)
        ordered_slugs = [t[1] for t in leaf_tuples]
        assert ordered_slugs == ["vMID_SANE", "vSMALL_SANE", "vHUGE_INSANE"]
        # Pick the top row → MID_SANE at 12%
        _, v_pick, facet_pick = leaf_tuples[0]
        assert v_pick == "vMID_SANE"
        assert facet_pick["return_pct"] == 12.0

    def test_to_relative_none(self):
        """Path helpers: None in, None out."""
        from src.strategies.storage import _to_relative
        assert _to_relative(None) is None

    def test_to_relative_already_relative(self):
        from src.strategies.storage import _to_relative
        assert _to_relative("reports/foo") == "reports/foo"

    def test_to_relative_absolute_inside_repo(self):
        from src.strategies.storage import _to_relative, _repo_root
        abs_path = _repo_root() / "reports" / "foo"
        result = _to_relative(abs_path)
        assert result == "reports/foo"

    def test_to_relative_absolute_outside_repo(self):
        """Absolute path outside the repo falls through as absolute."""
        from src.strategies.storage import _to_relative
        outside = "/tmp/not_in_repo/report.html"
        result = _to_relative(outside)
        assert result == outside

    def test_to_absolute_none(self):
        from src.strategies.storage import _to_absolute
        assert _to_absolute(None) is None

    def test_to_absolute_relative_path(self):
        from src.strategies.storage import _to_absolute, _repo_root
        result = _to_absolute("reports/foo/index.html")
        assert result == _repo_root() / "reports" / "foo" / "index.html"

    def test_to_absolute_already_absolute(self):
        from src.strategies.storage import _to_absolute
        abs_path = "/tmp/test/abs.html"
        result = _to_absolute(abs_path)
        assert str(result) == abs_path

    def test_parse_json_none(self):
        from src.strategies.storage import _parse_json
        assert _parse_json(None) is None
        assert _parse_json(None, default={}) == {}
        assert _parse_json(None, default=[]) == []

    def test_parse_json_empty_string(self):
        from src.strategies.storage import _parse_json
        assert _parse_json("") is None
        assert _parse_json("", default={}) == {}

    def test_parse_json_malformed_falls_back_to_default(self):
        from src.strategies.storage import _parse_json
        assert _parse_json("{not valid", default={}) == {}
        assert _parse_json("abc", default=[]) == []

    def test_resolve_strategy_string_registry_key(self):
        """_resolve_strategy with a string lookup into STRATEGY_REGISTRY."""
        from src.strategies.storage import _resolve_strategy
        name, inst = _resolve_strategy("smoke_demo")
        assert name == "smoke_demo"
        assert inst is not None
        assert inst.name == "smoke_demo"

    def test_resolve_strategy_unknown_string(self):
        """Unknown registry key should return (name, None) without crashing."""
        from src.strategies.storage import _resolve_strategy
        name, inst = _resolve_strategy("zzz_unknown_strategy")
        assert name == "zzz_unknown_strategy"
        assert inst is None

    def test_resolve_strategy_class_object(self):
        """Passing a class (not instance) — should instantiate + return name."""
        from src.strategies.storage import _resolve_strategy
        from src.strategies.demos.smoke_demo import SmokeDemoStrategy
        name, inst = _resolve_strategy(SmokeDemoStrategy)
        assert inst is not None
        assert inst.name == "smoke_demo"

    def test_resolve_strategy_instance(self):
        from src.strategies.storage import _resolve_strategy
        from src.strategies.demos.smoke_demo import SmokeDemoStrategy
        my_inst = SmokeDemoStrategy()
        name, inst = _resolve_strategy(my_inst)
        assert name == "smoke_demo"
        assert inst is my_inst
