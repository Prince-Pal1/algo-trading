"""Tests for src/m3s/signal_filter/audit.py — SQLite write-through."""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.backtest.result_store import _BACKTEST_SCHEMA
from src.m3s.signal_filter.audit import CloseInfo, audit_close, audit_signal
from src.utils.types import Signal, SignalAction


@pytest.fixture
def conn():
    """In-memory SQLite with the signal_audit table created."""
    c = sqlite3.connect(":memory:")
    c.executescript(_BACKTEST_SCHEMA)
    c.commit()
    yield c
    c.close()


def _signal(
    action: SignalAction = SignalAction.LONG,
    entry: float = 100.0,
    risk_pct: float = 0.01,
    ts: int = 1_700_000_000_000,
) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        action=action,
        confidence=0.8,
        strategy_name="bb_rsi_mr_opt",
        timeframe="1h",
        entry_price=entry,
        stop_loss=entry * 0.98,
        take_profit=entry * 1.04,
        risk_pct=risk_pct,
        metadata=None,
        timestamp=ts,
    )


class TestSchemaCreation:
    def test_signal_audit_table_exists(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_audit'"
        ).fetchall()
        assert len(rows) == 1

    def test_indexes_created(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_signal_audit%'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert "idx_signal_audit_strategy_ts" in names
        assert "idx_signal_audit_label_t1" in names
        assert "idx_signal_audit_run" in names
        assert "idx_signal_audit_trade_id" in names


class TestAuditSignal:
    def test_writes_single_row(self, conn):
        audit_id = audit_signal(
            conn, run_id="test_run",
            signal=_signal(),
            features_dict={"rsi_14": 30.0, "atr_14_pct": 0.02},
        )
        assert audit_id is not None
        assert audit_id > 0

        row = conn.execute(
            "SELECT strategy, symbol, signal_action, features_json FROM signal_audit WHERE id = ?",
            (audit_id,),
        ).fetchone()
        assert row[0] == "bb_rsi_mr_opt"
        assert row[1] == "BTCUSDT"
        assert row[2] == "LONG"
        feats = json.loads(row[3])
        assert feats["rsi_14"] == 30.0

    def test_outcome_columns_are_null_on_insert(self, conn):
        audit_id = audit_signal(conn, run_id="r1", signal=_signal(), features_dict={})
        row = conn.execute(
            "SELECT trade_id, exit_price, realized_pnl, meta_label "
            "FROM signal_audit WHERE id = ?",
            (audit_id,),
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None

    def test_multiple_rows_per_strategy(self, conn):
        for _ in range(5):
            audit_signal(conn, run_id="r1", signal=_signal(), features_dict={})
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_audit WHERE strategy = ?",
            ("bb_rsi_mr_opt",),
        ).fetchone()[0]
        assert count == 5

    def test_features_with_none_values_serializable(self, conn):
        feats = {"rsi_14": None, "atr_14_pct": 0.02, "portfolio_equity": None}
        audit_id = audit_signal(conn, run_id="r1", signal=_signal(), features_dict=feats)
        assert audit_id is not None
        row = conn.execute(
            "SELECT features_json FROM signal_audit WHERE id = ?", (audit_id,),
        ).fetchone()
        loaded = json.loads(row[0])
        assert loaded["rsi_14"] is None
        assert loaded["atr_14_pct"] == 0.02


class TestAuditClose:
    def test_close_populates_outcome_columns(self, conn):
        audit_id = audit_signal(
            conn, run_id="r1", signal=_signal(entry=100.0), features_dict={},
        )

        ok = audit_close(
            conn, audit_id,
            CloseInfo(
                trade_id=42,
                exit_ts_ms=1_700_000_100_000,
                exit_price=104.0,
                realized_pnl=40.0,
                barrier_hit="pt",
                entry_price=100.0,
                direction=1,
            ),
        )
        assert ok is True

        row = conn.execute(
            "SELECT trade_id, exit_price, realized_pnl, barrier_hit, "
            "triple_barrier_label, meta_label FROM signal_audit WHERE id = ?",
            (audit_id,),
        ).fetchone()
        assert row[0] == 42
        assert row[1] == 104.0
        assert row[2] == 40.0
        assert row[3] == "pt"
        assert row[4] == 1          # PT hit → tb_label = +1
        assert row[5] == 1          # direction == +1, tb_label == +1 → meta = 1

    def test_close_sl_hit_meta_label_zero(self, conn):
        audit_id = audit_signal(
            conn, run_id="r1", signal=_signal(entry=100.0), features_dict={},
        )
        audit_close(
            conn, audit_id,
            CloseInfo(
                trade_id=43, exit_ts_ms=1_700_000_200_000, exit_price=98.0,
                realized_pnl=-20.0, barrier_hit="sl",
                entry_price=100.0, direction=1,
            ),
        )
        row = conn.execute(
            "SELECT triple_barrier_label, meta_label FROM signal_audit WHERE id = ?",
            (audit_id,),
        ).fetchone()
        assert row[0] == -1
        assert row[1] == 0

    def test_close_unknown_audit_id_is_noop(self, conn):
        ok = audit_close(
            conn, 0,
            CloseInfo(
                trade_id=1, exit_ts_ms=0, exit_price=100.0, realized_pnl=0.0,
                barrier_hit="signal", entry_price=100.0, direction=1,
            ),
        )
        assert ok is False


class TestIdempotency:
    def test_insert_failure_does_not_crash(self, conn):
        """A sqlite error on insert must return None, not raise."""
        # Corrupt the connection by closing it; subsequent insert will fail
        conn.close()
        # Attempt on a closed connection — should be caught and logged
        audit_id = audit_signal(conn, run_id="r1", signal=_signal(), features_dict={})
        assert audit_id is None
