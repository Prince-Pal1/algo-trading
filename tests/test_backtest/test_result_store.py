"""ResultStore triple-output tests — SQLite + JSON + HTML all produced atomically.

Writes to tmp_path so we never touch real data/trades.db or reports/.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.engine import Trade
from src.backtest.result_store import ResultStore


def _sample_equity() -> pd.Series:
    return pd.Series([10000, 10100, 10080, 10200, 10180], index=[0, 1, 2, 3, 4], dtype=float)


def _sample_ohlcv() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(5)],
        "open": [100.0, 101.0, 100.5, 101.5, 101.0],
        "high": [101.0, 102.0, 101.5, 102.5, 101.5],
        "low": [99.0, 100.5, 100.0, 101.0, 100.8],
        "close": [100.5, 101.5, 101.0, 102.0, 101.2],
        "volume": [1000.0] * 5,
    })


def _sample_trades() -> list[Trade]:
    return [
        Trade(
            entry_idx=0, exit_idx=1, side="LONG",
            entry_price=100.0, exit_price=101.5,
            quantity=1.0, pnl=1.5, pnl_pct=0.015,
            commission=0.0, exit_reason="signal",
        ),
        Trade(
            entry_idx=2, exit_idx=3, side="LONG",
            entry_price=101.0, exit_price=102.0,
            quantity=1.0, pnl=1.0, pnl_pct=0.0099,
            commission=0.0, exit_reason="signal",
        ),
    ]


@pytest.fixture
def tmp_store(tmp_path: Path) -> ResultStore:
    return ResultStore(
        db_path=str(tmp_path / "trades.db"),
        reports_dir=str(tmp_path / "reports"),
    )


class TestSaveRun:
    def test_save_run_produces_triple_output(self, tmp_store: ResultStore, tmp_path: Path):
        record = tmp_store.save_run(
            strategy_id="test_strat",
            symbol="BTCUSDT", timeframe="1h",
            equity=_sample_equity(),
            trades=_sample_trades(),
            ohlcv_df=_sample_ohlcv(),
        )

        assert record.run_id > 0
        # 1. SQLite row exists
        conn = sqlite3.connect(tmp_path / "trades.db")
        row = conn.execute(
            "SELECT strategy_id, symbol, timeframe FROM backtest_runs WHERE id = ?",
            (record.run_id,),
        ).fetchone()
        assert row == ("test_strat", "BTCUSDT", "1h")
        result_row = conn.execute(
            "SELECT total_trades FROM backtest_results WHERE run_id = ?",
            (record.run_id,),
        ).fetchone()
        assert result_row[0] == 2
        conn.close()

        # 2. JSON file written
        assert record.json_path.exists()
        payload = json.loads(record.json_path.read_text())
        assert payload["run_id"] == record.run_id
        assert payload["strategy_id"] == "test_strat"
        assert len(payload["trades"]) == 2

        # 3. HTML file written
        assert record.html_path.exists()
        assert record.html_path.stat().st_size > 100  # not empty
        assert "test_strat" in record.html_path.read_text()

    def test_metrics_consistent_across_outputs(self, tmp_store: ResultStore):
        """Metrics in the returned record must match what's in the JSON and DB."""
        record = tmp_store.save_run(
            strategy_id="consistency",
            symbol="BTCUSDT", timeframe="1h",
            equity=_sample_equity(),
            trades=_sample_trades(),
            ohlcv_df=_sample_ohlcv(),
        )
        record_total = record.metrics["total_trades"]

        payload = json.loads(record.json_path.read_text())
        json_total = payload["total_trades"]

        conn = sqlite3.connect(tmp_store._db_path)
        db_total = conn.execute(
            "SELECT total_trades FROM backtest_results WHERE run_id = ?",
            (record.run_id,),
        ).fetchone()[0]
        conn.close()

        assert record_total == json_total == db_total == 2

    def test_zero_trades_run_succeeds(self, tmp_store: ResultStore):
        """Backtest with zero trades must still produce a valid triple output."""
        record = tmp_store.save_run(
            strategy_id="empty",
            symbol="BTCUSDT", timeframe="1h",
            equity=_sample_equity(),
            trades=[],
            ohlcv_df=_sample_ohlcv(),
        )
        assert record.json_path.exists()
        assert record.html_path.exists()
        assert record.metrics["total_trades"] == 0
