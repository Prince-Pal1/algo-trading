"""Unit tests for src/strategies/storage.py (task #117 G.8).

Uses file-backed SQLite in `tmp_path` for tests that need cross-connection
persistence (most of them), and `:memory:` for pure schema/pragma checks.
Mirrors the `tests/test_strategies/test_graveyard.py` pattern.

Covers:
  - Schema + pragmas (idempotent + WAL/FK enforcement)
  - Upsert semantics (insert + do-not-clobber update)
  - FK enforcement
  - Version slug auto-generation (idempotent + collision hashing)
  - Max-return cell picker (absolute max, NaN-safe, NOT Calmar-ranked)
  - Max-return sanity flag (thin trades / high DD / low Calmar)
  - record_deep_backtest_result integration (empty matrix, idempotent)
  - Retry-on-busy wrapper
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.deep_backtest import _compute_max_return_cell, _is_max_return_sane
from src.strategies.storage import (
    _SCHEMA_VERSION,
    StoredStrategy,
    StoredVersion,
    _connect,
    _generate_version_slug,
    _retry_on_busy,
    get_strategy,
    get_version,
    kill_strategy,
    list_strategies,
    list_version_runs,
    list_versions,
    query_best_by_max_return,
    query_deployable_by_calmar,
    query_vanity_traps,
    record_deep_backtest_result,
    upsert_strategy,
    upsert_version,
)


# ── Fixtures + test doubles ─────────────────────────────────────────────


@dataclass
class _FakeCellResult:
    """Minimal stand-in for CellResult — enough fields for best_cell serialization."""
    window_days: int = 90
    window_label: str = "3mo"
    timeframe: str = "1h"
    leverage: float = 10.0
    fee_profile: str = "ic_markets_mt4_xauusd_normal"
    trades: int = 40
    return_pct: float = 5.0
    maxdd_pct: float = 1.5
    win_rate: float = 62.5
    profit_factor: float = 4.5
    avg_win: float = 100.0
    avg_loss: float = -40.0
    total_commission: float = 1.0
    final_equity: float = 10500.0
    margin_per_trade: float = 300.0
    cost_per_trade: float = 0.05
    cost_pct_of_margin: float = 0.02
    broker_stop_outs: int = 0
    calmar: float = 13.3
    sharpe: float = 4.0
    rejected_positions: int = 0
    notes: str = ""


@dataclass
class _FakeConfig:
    leverage_mode: object = None  # None or an enum-like with .value
    baseline_leverage: float | None = 10.0
    timeframes: list = field(default_factory=lambda: ["1h"])
    strategy_params: dict | None = field(default_factory=dict)


@dataclass
class _FakeLeverageMode:
    value: str


@dataclass
class _FakeWalkForward:
    """Minimal stand-in for WalkForwardSummary — just the continuous fields
    the storage layer denormalizes."""
    continuous_return_pct: float = 50.0
    continuous_dd_pct: float = 8.0
    continuous_calmar: float = 12.0
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
    verdict_reason: str = "smoke test"
    walk_forward: _FakeWalkForward | None = None


def _make_matrix_df(rows: list[dict]) -> pd.DataFrame:
    """Construct a matrix DataFrame from a list of row dicts."""
    columns = [
        "window_days", "window_label", "timeframe", "leverage", "fee_profile",
        "trades", "return_pct", "maxdd_pct", "win_rate", "profit_factor",
        "avg_win", "avg_loss", "total_commission", "final_equity",
        "margin_per_trade", "cost_per_trade", "cost_pct_of_margin",
        "broker_stop_outs", "calmar", "sharpe", "rejected_positions", "notes",
    ]
    return pd.DataFrame(rows, columns=columns)


def _sample_row(**overrides) -> dict:
    defaults = {
        "window_days": 90,
        "window_label": "3mo",
        "timeframe": "1h",
        "leverage": 10.0,
        "fee_profile": "ic_markets_mt4_xauusd_normal",
        "trades": 40,
        "return_pct": 5.0,
        "maxdd_pct": 1.5,
        "win_rate": 62.5,
        "profit_factor": 4.5,
        "avg_win": 100.0,
        "avg_loss": -40.0,
        "total_commission": 1.0,
        "final_equity": 10500.0,
        "margin_per_trade": 300.0,
        "cost_per_trade": 0.05,
        "cost_pct_of_margin": 0.02,
        "broker_stop_outs": 0,
        "calmar": 13.3,
        "sharpe": 4.0,
        "rejected_positions": 0,
        "notes": "",
    }
    defaults.update(overrides)
    return defaults


# ── Schema + pragmas ────────────────────────────────────────────────────


class TestSchemaAndPragmas:
    def test_schema_creates_idempotent(self, tmp_path):
        db = str(tmp_path / "t.db")
        _connect(db).close()  # first install
        _connect(db).close()  # second call must not error
        # Confirm both tables exist
        conn = sqlite3.connect(db)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "strategies" in tables
            assert "strategy_versions" in tables
        finally:
            conn.close()

    def test_pragmas_set_on_file_db(self, tmp_path):
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert journal.lower() == "wal"
            assert fk == 1
        finally:
            conn.close()


# ── Upsert semantics ────────────────────────────────────────────────────


class TestUpsertStrategy:
    def test_insert_roundtrip(self, tmp_path):
        db = str(tmp_path / "t.db")
        s = StoredStrategy(
            name="foo_strategy",
            description="initial description",
            family="breakout",
            tier="TIER_2",
            markets=["XAUUSD"],
            default_timeframe="1h",
        )
        strategy_id = upsert_strategy(s, db_path=db)
        assert strategy_id > 0

        fetched = get_strategy("foo_strategy", db_path=db)
        assert fetched is not None
        assert fetched.id == strategy_id
        assert fetched.description == "initial description"
        assert fetched.family == "breakout"
        assert fetched.tier == "TIER_2"
        assert fetched.markets == ["XAUUSD"]
        assert fetched.default_timeframe == "1h"
        assert fetched.status == "researching"

    def test_update_preserves_user_fields_on_none_input(self, tmp_path):
        """Do-not-clobber: a second upsert with description=None must NOT
        wipe the first upsert's description. Auto-capture never overwrites
        user annotations typed via `scripts/strategies.py register`."""
        db = str(tmp_path / "t.db")
        upsert_strategy(
            StoredStrategy(name="foo", description="original D", family="breakout"),
            db_path=db,
        )
        # Second call with None for both user-editable fields
        upsert_strategy(
            StoredStrategy(name="foo", description=None, family=None, base_class="FooStrategy"),
            db_path=db,
        )
        fetched = get_strategy("foo", db_path=db)
        assert fetched.description == "original D"
        assert fetched.family == "breakout"
        assert fetched.base_class == "FooStrategy"  # newly set field


class TestUpsertVersion:
    def test_insert_fk_link(self, tmp_path):
        db = str(tmp_path / "t.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        v = StoredVersion(strategy_id=sid, version_slug="risk_scaled_L10_1h")
        vid = upsert_version(v, db_path=db)
        assert vid > 0

        fetched = get_version("foo", "risk_scaled_L10_1h", db_path=db)
        assert fetched is not None
        assert fetched.strategy_id == sid
        assert fetched.version_slug == "risk_scaled_L10_1h"

    def test_update_preserves_performance_fields_on_none(self, tmp_path):
        """Do-not-clobber: a second upsert with max_return_pct=None must NOT
        wipe the first upsert's performance data. Stage 2 backfill touches
        parent fields without re-running the matrix; this guards that case."""
        db = str(tmp_path / "t.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        upsert_version(
            StoredVersion(
                strategy_id=sid,
                version_slug="v1",
                max_return_pct=50.0,
                verdict="DEPLOYABLE",
                max_return_cell={"return_pct": 50.0, "trades": 40},
            ),
            db_path=db,
        )
        upsert_version(
            StoredVersion(
                strategy_id=sid,
                version_slug="v1",
                description="annotation added later",
                max_return_pct=None,  # don't clobber
                verdict=None,  # don't clobber
            ),
            db_path=db,
        )
        fetched = get_version("foo", "v1", db_path=db)
        assert fetched.max_return_pct == 50.0
        assert fetched.verdict == "DEPLOYABLE"
        assert fetched.description == "annotation added later"
        assert fetched.max_return_cell == {"return_pct": 50.0, "trades": 40}


class TestForeignKeyEnforcement:
    def test_fk_violation_orphan_version(self, tmp_path):
        """Direct INSERT into strategy_versions with non-existent strategy_id
        raises IntegrityError — proves FK is actually enforced."""
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO strategy_versions
                    (strategy_id, version_slug, created_at, updated_at)
                    VALUES (?, ?, datetime('now'), datetime('now'))
                    """,
                    (9999, "orphan"),
                )
                conn.commit()
        finally:
            conn.close()


# ── Version slug generation ─────────────────────────────────────────────


class TestVersionSlug:
    def test_slug_idempotent_same_config(self):
        """Same config called twice → same slug (no collision even when an
        existing version with identical params is already recorded)."""
        slug1 = _generate_version_slug(
            leverage_mode="risk_scaled",
            baseline_leverage=10.0,
            timeframe="1h",
            params={"max_risk_per_trade": 0.02},
            existing_versions=[],
        )
        existing_stored = StoredVersion(
            strategy_id=1,
            version_slug=slug1,
            params={"max_risk_per_trade": 0.02},
        )
        slug2 = _generate_version_slug(
            leverage_mode="risk_scaled",
            baseline_leverage=10.0,
            timeframe="1h",
            params={"max_risk_per_trade": 0.02},
            existing_versions=[existing_stored],
        )
        # Re-running with identical params reuses the base slug (same row).
        assert slug1 == slug2 == "risk_scaled_L10_1h"

    def test_slug_hash_collision_only_when_params_differ(self):
        """Same (mode, leverage, tf) but DIFFERENT params → base slug already
        taken by a row with other params, so the new call gets a hash suffix."""
        base = _generate_version_slug(
            leverage_mode="risk_scaled",
            baseline_leverage=10.0,
            timeframe="1h",
            params={"max_risk_per_trade": 0.02},
            existing_versions=[],
        )
        existing_stored = StoredVersion(
            strategy_id=1,
            version_slug=base,
            params={"max_risk_per_trade": 0.02},  # the first row's params
        )
        collision = _generate_version_slug(
            leverage_mode="risk_scaled",
            baseline_leverage=10.0,
            timeframe="1h",
            params={"max_risk_per_trade": 0.03},  # different param dict
            existing_versions=[existing_stored],
        )
        assert base == "risk_scaled_L10_1h"
        assert collision.startswith("risk_scaled_L10_1h_")
        assert len(collision) == len("risk_scaled_L10_1h") + 1 + 6

    def test_slug_default_when_no_config(self):
        slug = _generate_version_slug(
            leverage_mode=None,
            baseline_leverage=None,
            timeframe=None,
            params=None,
            existing_versions=[],
        )
        assert slug == "default"


# ── Max-return cell picker ──────────────────────────────────────────────


class TestMaxReturnCell:
    def test_picks_absolute_max_not_calmar(self):
        """Cell A: +10% return, DD 50%, Calmar 0.2.
        Cell B: +5% return, DD 0.5%, Calmar 10 (max Calmar).
        Max-return MUST pick A (higher return), NOT B (higher Calmar)."""
        df = _make_matrix_df([
            _sample_row(return_pct=5.0, maxdd_pct=0.5, calmar=10.0, leverage=10),
            _sample_row(return_pct=10.0, maxdd_pct=50.0, calmar=0.2, leverage=20),
        ])
        cell = _compute_max_return_cell(df)
        assert cell["return_pct"] == 10.0
        assert cell["leverage"] == 20.0
        assert cell["calmar"] == 0.2  # high return but low Calmar — that's the test

    def test_skips_nan_rows(self):
        """Matrix with a NaN return_pct row — _compute_max_return_cell picks
        the non-NaN max via dropna(subset=['return_pct'])."""
        df = _make_matrix_df([
            _sample_row(return_pct=float("nan"), leverage=30),
            _sample_row(return_pct=8.0, leverage=15),
            _sample_row(return_pct=3.0, leverage=10),
        ])
        cell = _compute_max_return_cell(df)
        assert cell["return_pct"] == 8.0
        assert cell["leverage"] == 15.0

    def test_empty_matrix_returns_empty_dict(self):
        cell = _compute_max_return_cell(pd.DataFrame())
        assert cell == {}

    def test_none_matrix_returns_empty_dict(self):
        cell = _compute_max_return_cell(None)
        assert cell == {}


# ── Max-return sanity flag ──────────────────────────────────────────────


class TestMaxReturnSane:
    def test_sane_row_passes(self):
        cell = {"trades": 40, "maxdd_pct": 10.0, "calmar": 5.0}
        sane, warning = _is_max_return_sane(cell)
        assert sane is True
        assert warning == ""

    def test_flags_thin_trade_count(self):
        cell = {"trades": 5, "maxdd_pct": 10.0, "calmar": 5.0}
        sane, warning = _is_max_return_sane(cell)
        assert sane is False
        assert "only 5 trades" in warning

    def test_flags_high_dd(self):
        cell = {"trades": 40, "maxdd_pct": 75.0, "calmar": 5.0}
        sane, warning = _is_max_return_sane(cell)
        assert sane is False
        assert "DD 75%" in warning

    def test_flags_low_calmar(self):
        cell = {"trades": 40, "maxdd_pct": 10.0, "calmar": 0.1}
        sane, warning = _is_max_return_sane(cell)
        assert sane is False
        assert "Calmar 0.10" in warning

    def test_empty_cell_flags_no_cells(self):
        sane, warning = _is_max_return_sane({})
        assert sane is False
        assert warning == "no cells"


# ── record_deep_backtest_result integration ─────────────────────────────


class TestRecordDeepBacktestResult:
    def test_empty_matrix_writes_row_with_nulls(self, tmp_path):
        db = str(tmp_path / "t.db")
        result = _FakeResult(
            config=_FakeConfig(leverage_mode=_FakeLeverageMode("risk_scaled")),
            matrix_df=pd.DataFrame(),
            matrix=[],
            best_cell=None,
            report_dir=tmp_path,
        )
        sid, vid = record_deep_backtest_result("foo_strategy", result, db_path=db)
        assert sid > 0 and vid > 0

        version = get_version("foo_strategy", "risk_scaled_L10_1h", db_path=db)
        assert version is not None
        assert version.max_return_pct is None
        assert version.max_return_cell is None
        assert version.verdict == "NEEDS_WF"

    def test_idempotent_preserves_created_at(self, tmp_path):
        db = str(tmp_path / "t.db")
        df = _make_matrix_df([
            _sample_row(return_pct=5.0, leverage=10),
            _sample_row(return_pct=8.0, leverage=20),
        ])
        result = _FakeResult(
            config=_FakeConfig(leverage_mode=_FakeLeverageMode("risk_scaled")),
            matrix_df=df,
            matrix=[1, 2],
            best_cell=_FakeCellResult(return_pct=5.0, leverage=10, calmar=13.3),
            report_dir=tmp_path,
        )

        sid1, vid1 = record_deep_backtest_result("foo_strategy", result, db_path=db)
        version1 = get_version("foo_strategy", "risk_scaled_L10_1h", db_path=db)
        created_at_1 = version1.created_at

        # Second call — should UPSERT same row (same slug)
        sid2, vid2 = record_deep_backtest_result("foo_strategy", result, db_path=db)
        version2 = get_version("foo_strategy", "risk_scaled_L10_1h", db_path=db)

        assert sid1 == sid2
        assert vid1 == vid2
        # Only one version row total for the strategy
        versions = list_versions(strategy_name="foo_strategy", db_path=db)
        assert len(versions) == 1
        # created_at preserved
        assert version2.created_at == created_at_1
        # Max return captured
        assert version2.max_return_pct == 8.0


# ── List queries ────────────────────────────────────────────────────────


class TestListQueries:
    def test_list_versions_filter_by_verdict(self, tmp_path):
        db = str(tmp_path / "t.db")
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        upsert_version(StoredVersion(strategy_id=sid, version_slug="a", verdict="DEPLOYABLE"), db_path=db)
        upsert_version(StoredVersion(strategy_id=sid, version_slug="b", verdict="FAILED"), db_path=db)
        upsert_version(StoredVersion(strategy_id=sid, version_slug="c", verdict="DEPLOYABLE"), db_path=db)

        deployable = list_versions(verdict="DEPLOYABLE", db_path=db)
        assert len(deployable) == 2
        assert {v.version_slug for v in deployable} == {"a", "c"}


# ── Retry-on-busy ───────────────────────────────────────────────────────


class TestRetryOnBusy:
    def test_retries_on_locked_then_succeeds(self):
        """First call raises OperationalError: database is locked.
        Second attempt returns a value. _retry_on_busy should eat the
        first error and return the second call's result."""
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return "success"

        result = _retry_on_busy(fn, attempts=3, backoff_ms=1)
        assert result == "success"
        assert calls["n"] == 2

    def test_raises_after_max_attempts(self):
        """Error persists across all attempts — final call re-raises."""
        def fn():
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _retry_on_busy(fn, attempts=2, backoff_ms=1)

    def test_non_locked_error_raises_immediately(self):
        """Error that isn't 'locked' passes through without retry."""
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise sqlite3.OperationalError("syntax error")

        with pytest.raises(sqlite3.OperationalError, match="syntax"):
            _retry_on_busy(fn, attempts=3, backoff_ms=1)
        assert calls["n"] == 1  # no retries


# ── Schema migrations (v2 → v5, tasks #119/#121/#124/#123) ──────────────


class TestSchemaMigrations:
    def test_pragma_user_version_stamped_after_connect(self, tmp_path):
        """Fresh connect against an empty file stamps user_version to the
        current _SCHEMA_VERSION so subsequent connects see a fully-migrated DB."""
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == _SCHEMA_VERSION
        finally:
            conn.close()

    def test_migrations_idempotent_across_reconnects(self, tmp_path):
        db = str(tmp_path / "t.db")
        _connect(db).close()
        _connect(db).close()
        # Schema + user_version should still be valid
        conn = _connect(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        finally:
            conn.close()

    def test_v2_wf_columns_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_versions)").fetchall()}
            for c in (
                "wf_continuous_return_pct",
                "wf_continuous_dd_pct",
                "wf_continuous_calmar",
                "wf_gate_passed",
                "wf_n_folds",
                "wf_profitable_folds",
            ):
                assert c in cols, f"expected {c} column"
        finally:
            conn.close()

    def test_v3_killed_graveyard_id_column_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)").fetchall()}
            assert "killed_graveyard_id" in cols
        finally:
            conn.close()

    def test_v4_backtest_run_id_column_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_versions)").fetchall()}
            assert "backtest_run_id" in cols
        finally:
            conn.close()

    def test_v5_history_table_exists(self, tmp_path):
        db = str(tmp_path / "t.db")
        conn = _connect(db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_version_runs'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


# ── Walk-forward denormalization (task #119) ────────────────────────────


class TestWalkForwardDenormalization:
    def _make_result_with_wf(self, tmp_path):
        df = _make_matrix_df([_sample_row(return_pct=8.0, leverage=10)])
        wf = _FakeWalkForward(
            continuous_return_pct=142.3,
            continuous_dd_pct=9.4,
            continuous_calmar=11.7,
            continuous_gate_passed=True,
            n_folds=6,
            profitable_folds=5,
        )
        return _FakeResult(
            config=_FakeConfig(leverage_mode=_FakeLeverageMode("risk_scaled")),
            matrix_df=df,
            matrix=[1],
            best_cell=_FakeCellResult(return_pct=8.0, leverage=10, calmar=13.3),
            report_dir=tmp_path,
            walk_forward=wf,
        )

    def test_wf_fields_populated_when_walk_forward_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        result = self._make_result_with_wf(tmp_path)
        record_deep_backtest_result("wf_test_strat", result, db_path=db)
        version = get_version("wf_test_strat", "risk_scaled_L10_1h", db_path=db)
        assert version.wf_continuous_return_pct == 142.3
        assert version.wf_continuous_dd_pct == 9.4
        assert version.wf_continuous_calmar == 11.7
        assert version.wf_gate_passed == "True"
        assert version.wf_n_folds == 6
        assert version.wf_profitable_folds == 5

    def test_wf_fields_none_when_no_walk_forward(self, tmp_path):
        db = str(tmp_path / "t.db")
        df = _make_matrix_df([_sample_row(return_pct=8.0, leverage=10)])
        result = _FakeResult(
            config=_FakeConfig(leverage_mode=_FakeLeverageMode("risk_scaled")),
            matrix_df=df,
            matrix=[1],
            best_cell=_FakeCellResult(),
            report_dir=tmp_path,
            walk_forward=None,  # explicitly no WF
        )
        record_deep_backtest_result("no_wf_strat", result, db_path=db)
        version = get_version("no_wf_strat", "risk_scaled_L10_1h", db_path=db)
        assert version.wf_continuous_return_pct is None
        assert version.wf_continuous_calmar is None
        assert version.wf_gate_passed is None


# ── Version-run history table (task #123) ──────────────────────────────


class TestVersionRunHistory:
    def test_each_record_call_appends_run_row(self, tmp_path):
        db = str(tmp_path / "t.db")
        df = _make_matrix_df([_sample_row(return_pct=5.0)])
        result = _FakeResult(
            config=_FakeConfig(leverage_mode=_FakeLeverageMode("risk_scaled")),
            matrix_df=df,
            matrix=[1],
            best_cell=_FakeCellResult(),
            report_dir=tmp_path,
        )
        record_deep_backtest_result("hist_strat", result, db_path=db)
        record_deep_backtest_result("hist_strat", result, db_path=db)
        record_deep_backtest_result("hist_strat", result, db_path=db)

        # Three runs should produce three history rows even though
        # strategy_versions has a single row (idempotent UPSERT).
        runs = list_version_runs(strategy_name="hist_strat", db_path=db)
        assert len(runs) == 3
        assert all(r["verdict"] == "NEEDS_WF" for r in runs)
        versions = list_versions(strategy_name="hist_strat", db_path=db)
        assert len(versions) == 1

    def test_history_ordered_by_timestamp_desc(self, tmp_path):
        db = str(tmp_path / "t.db")
        df = _make_matrix_df([_sample_row(return_pct=5.0)])
        result = _FakeResult(
            config=_FakeConfig(leverage_mode=_FakeLeverageMode("risk_scaled")),
            matrix_df=df, matrix=[1],
            best_cell=_FakeCellResult(),
            report_dir=tmp_path,
        )
        record_deep_backtest_result("hist_strat", result, db_path=db)
        record_deep_backtest_result("hist_strat", result, db_path=db)
        runs = list_version_runs(strategy_name="hist_strat", db_path=db)
        # Most recent first (both have same timestamp in fast test; id DESC tiebreaks)
        assert runs[0]["id"] >= runs[1]["id"]


# ── Graveyard linkage (task #121) ───────────────────────────────────────


class TestKillStrategy:
    def test_kill_sets_status_and_fk(self, tmp_path):
        db = str(tmp_path / "t.db")
        upsert_strategy(StoredStrategy(name="dead_strat"), db_path=db)
        kill_strategy("dead_strat", graveyard_id=42, db_path=db)
        s = get_strategy("dead_strat", db_path=db)
        assert s.status == "killed"
        assert s.killed_graveyard_id == 42

    def test_kill_nonexistent_is_noop(self, tmp_path):
        """No-op when strategy doesn't exist — silently does nothing."""
        db = str(tmp_path / "t.db")
        kill_strategy("ghost_strat", graveyard_id=1, db_path=db)
        assert get_strategy("ghost_strat", db_path=db) is None

    def test_kill_preserves_other_fields(self, tmp_path):
        db = str(tmp_path / "t.db")
        upsert_strategy(
            StoredStrategy(name="dead_strat", description="notes", family="momentum"),
            db_path=db,
        )
        kill_strategy("dead_strat", graveyard_id=5, db_path=db)
        s = get_strategy("dead_strat", db_path=db)
        assert s.description == "notes"
        assert s.family == "momentum"
        assert s.status == "killed"


# ── JSON1 query helpers (task #129) ─────────────────────────────────────


class TestQueryHelpers:
    def _seed(self, db: str):
        sid = upsert_strategy(StoredStrategy(name="foo"), db_path=db)
        upsert_version(StoredVersion(
            strategy_id=sid, version_slug="v1",
            verdict="DEPLOYABLE", max_return_pct=50.0,
            max_return_sane=True, best_calmar=10.0,
            wf_continuous_calmar=8.0,
        ), db_path=db)
        upsert_version(StoredVersion(
            strategy_id=sid, version_slug="v2",
            verdict="DEPLOYABLE", max_return_pct=340.0,
            max_return_sane=False, max_return_warning="only 4 trades",
            best_calmar=0.1, wf_continuous_calmar=0.2,
        ), db_path=db)
        upsert_version(StoredVersion(
            strategy_id=sid, version_slug="v3",
            verdict="FAILED", max_return_pct=200.0,
            max_return_sane=False,
        ), db_path=db)

    def test_query_best_by_max_return_orders_descending(self, tmp_path):
        db = str(tmp_path / "t.db")
        self._seed(db)
        results = query_best_by_max_return(db_path=db)
        # v2 (+340%) → v3 (+200%) → v1 (+50%)
        slugs = [v.version_slug for v in results]
        assert slugs == ["v2", "v3", "v1"]

    def test_query_best_by_max_return_verdict_filter(self, tmp_path):
        db = str(tmp_path / "t.db")
        self._seed(db)
        results = query_best_by_max_return(verdict="DEPLOYABLE", db_path=db)
        slugs = [v.version_slug for v in results]
        assert slugs == ["v2", "v1"]  # v3 is FAILED, excluded

    def test_query_deployable_by_calmar_uses_wf_by_default(self, tmp_path):
        db = str(tmp_path / "t.db")
        self._seed(db)
        results = query_deployable_by_calmar(db_path=db)
        # By WF Calmar: v1 (8.0) > v2 (0.2). v3 is FAILED.
        slugs = [v.version_slug for v in results]
        assert slugs == ["v1", "v2"]

    def test_query_deployable_by_calmar_falls_back_to_best_calmar(self, tmp_path):
        db = str(tmp_path / "t.db")
        self._seed(db)
        results = query_deployable_by_calmar(use_wf_calmar=False, db_path=db)
        slugs = [v.version_slug for v in results]
        assert slugs == ["v1", "v2"]  # best_calmar: 10.0 > 0.1

    def test_query_vanity_traps_finds_unsafe_deployable(self, tmp_path):
        """The critical safety query: DEPLOYABLE verdict with sane=False
        max-return cell. These are the 'looks-great-but-actually-fragile'
        misreads we need to audit before promoting to live capital."""
        db = str(tmp_path / "t.db")
        self._seed(db)
        traps = query_vanity_traps(db_path=db)
        # Only v2: DEPLOYABLE + max_return_sane=False (warning: "only 4 trades")
        # v1 is DEPLOYABLE but sane=True → NOT a trap
        # v3 is sane=False but FAILED → not a trap (already rejected)
        assert len(traps) == 1
        assert traps[0].version_slug == "v2"
        assert traps[0].max_return_warning == "only 4 trades"
