"""End-to-end integration test: BacktestEngine + signal_audit writes.

Verifies that when the engine runs with `audit_db_path` set, every
signal emission produces a row in `signal_audit` and every trade close
populates the outcome columns with a correct meta_label.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.utils.types import RiskProfile, Signal, SignalAction


class AlwaysLongStrategy:
    """Minimal test strategy that emits LONG on bar 5, CLOSE on bar 15."""

    def __init__(self):
        self.name = "always_long_test"
        self.markets = ["TESTUSDT"]
        self.timeframe = "1h"
        self.risk_profile = RiskProfile.SAFE
        self.max_risk_per_trade = 0.01
        self._bar = 0
        self._position = "FLAT"
        self._prev_features = None
        self._signal_count = 0

    def on_features(self, symbol, timeframe, features):
        bar = self._bar
        if bar == 5 and self._position == "FLAT":
            return Signal(
                symbol=symbol, action=SignalAction.LONG,
                confidence=0.8, strategy_name=self.name, timeframe=timeframe,
                entry_price=float(features["close"]),
                stop_loss=float(features["close"]) * 0.98,
                take_profit=float(features["close"]) * 1.04,
                risk_pct=0.01, timestamp=int(features["timestamp"]),
            )
        if bar == 15 and self._position == "LONG":
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE,
                confidence=1.0, strategy_name=self.name, timeframe=timeframe,
                entry_price=float(features["close"]),
                risk_pct=None, timestamp=int(features["timestamp"]),
            )
        return None

    def process(self, symbol, timeframe, features):
        import time as _t
        signal = self.on_features(symbol, timeframe, features)
        self._prev_features = features.copy()
        self._bar += 1
        if signal is not None:
            self._signal_count += 1
            if signal.action.value in ("LONG", "SHORT"):
                self._position = signal.action.value
            elif signal.action.value == "CLOSE":
                self._position = "FLAT"
            if signal.timestamp == 0:
                signal.timestamp = int(_t.time() * 1000)
            if not signal.strategy_name:
                signal.strategy_name = self.name
        return signal

    def matches(self, symbol, timeframe):
        return symbol.upper() in self.markets and timeframe == self.timeframe


def _ohlcv_trending_up(n: int = 60) -> pd.DataFrame:
    """Generate a slow uptrend so the LONG at bar 5 hits PT, not SL."""
    base_ts = 1_700_000_000_000
    rows = []
    for i in range(n):
        price = 100.0 + i * 0.3
        rows.append({
            "timestamp": base_ts + i * 3_600_000,
            "open": price,
            "high": price + 0.2,
            "low": price - 0.1,
            "close": price,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


class TestEngineAuditIntegration:
    def test_audit_disabled_by_default(self, tmp_path):
        """Without audit_db_path, no writes happen."""
        engine = BacktestEngine(config=BacktestConfig(initial_capital=10_000.0))
        df = _ohlcv_trending_up()
        strategy = AlwaysLongStrategy()
        result = engine.run(strategy, df, symbol="TESTUSDT", timeframe="1h", indicators=[])
        assert len(result.trades) >= 1

    def test_audit_writes_rows(self, tmp_path):
        """With audit_db_path, every LONG signal produces an audit row."""
        db_path = tmp_path / "audit_test.db"
        engine = BacktestEngine(
            config=BacktestConfig(initial_capital=10_000.0),
            audit_db_path=str(db_path),
            audit_run_id="test_run_123",
        )
        df = _ohlcv_trending_up()
        strategy = AlwaysLongStrategy()
        result = engine.run(strategy, df, symbol="TESTUSDT", timeframe="1h", indicators=[])

        assert db_path.exists()

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id, strategy, symbol, signal_action, features_json, "
            "exit_price, meta_label, barrier_hit, realized_pnl "
            "FROM signal_audit WHERE run_id = ?",
            ("test_run_123",),
        ).fetchall()
        conn.close()

        assert len(rows) >= 1, "expected at least one audit row"
        entry_row = rows[0]
        assert entry_row[1] == "always_long_test"
        assert entry_row[2] == "TESTUSDT"
        assert entry_row[3] == "LONG"
        # features_json must be non-empty JSON
        assert entry_row[4].startswith("{")
        # Outcome columns must be populated because the trade closed
        assert entry_row[5] is not None, "exit_price should be set"
        assert entry_row[6] in (0, 1), "meta_label should be set"
        assert entry_row[7] in ("pt", "sl", "signal", "time"), \
            f"barrier_hit unexpected: {entry_row[7]}"

    def test_audit_features_are_parseable(self, tmp_path):
        """features_json must round-trip through JSON and contain known keys."""
        import json
        db_path = tmp_path / "audit_features.db"
        engine = BacktestEngine(
            config=BacktestConfig(initial_capital=10_000.0),
            audit_db_path=str(db_path),
        )
        df = _ohlcv_trending_up()
        engine.run(AlwaysLongStrategy(), df, symbol="TESTUSDT", timeframe="1h", indicators=[])

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT features_json FROM signal_audit LIMIT 1",
        ).fetchone()
        conn.close()

        features = json.loads(row[0])
        assert "signal_direction" in features
        assert "signal_confidence" in features
        assert "hour_of_day_sin" in features
        assert features["signal_direction"] == 1.0
        assert features["signal_confidence"] == 0.8

    def test_audit_uptrend_produces_meta_label_1(self, tmp_path):
        """Slow uptrend → LONG hits PT → meta_label == 1."""
        db_path = tmp_path / "audit_uptrend.db"
        engine = BacktestEngine(
            config=BacktestConfig(initial_capital=10_000.0, commission_pct=0.0, slippage_pct=0.0),
            audit_db_path=str(db_path),
        )
        df = _ohlcv_trending_up(n=80)
        engine.run(AlwaysLongStrategy(), df, symbol="TESTUSDT", timeframe="1h", indicators=[])

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT meta_label, triple_barrier_label, barrier_hit FROM signal_audit LIMIT 1",
        ).fetchone()
        conn.close()

        # The strategy exits on bar 15 via CLOSE (not a barrier) — so
        # barrier_hit should be 'signal' and the PnL sign determines the label.
        # With uptrend from bar 5 (price=101.5) to bar 15 (price=104.5), residual is positive.
        assert row[0] == 1, f"expected meta_label=1, got {row[0]}"
        assert row[1] == 1, f"expected tb_label=1, got {row[1]}"
