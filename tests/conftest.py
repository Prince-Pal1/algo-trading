"""Shared test fixtures for backtest engine verification."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction


# ── ProgrammableStrategy ─────────────────────────────────────────────────
# Emits pre-configured signals at specific bar indices.
# No indicators needed — pure mechanics testing.


class ProgrammableStrategy(BaseStrategy):
    """Test strategy that emits signals at pre-defined bar indices."""

    def __init__(self, signals: dict[int, Signal]):
        super().__init__(
            name="programmable_test",
            markets=["TESTUSDT"],
            timeframe="1h",
        )
        self._signals = signals
        self._bar_count = 0

    def on_features(self, symbol, timeframe, features):
        idx = self._bar_count
        self._bar_count += 1
        return self._signals.get(idx)


def make_signal(
    action: SignalAction,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    risk_pct: float | None = None,
) -> Signal:
    """Helper to build a Signal with sensible defaults."""
    return Signal(
        symbol="TESTUSDT",
        action=action,
        confidence=1.0,
        strategy_name="programmable_test",
        timeframe="1h",
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_pct=risk_pct,
    )


# ── Synthetic Data Builders ──────────────────────────────────────────────


def make_ohlcv(
    closes: list[float],
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start_ts: int = 1_735_689_600_000,  # 2025-01-01 00:00 UTC
    interval_ms: int = 3_600_000,       # 1h
    min_bars: int = 60,                 # engine requires >=50 bars
) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame from price lists.

    If opens/highs/lows are not given, they default to close values
    (flat bars). Auto-pads to min_bars by repeating the last values.
    """
    n = len(closes)
    if opens is None:
        opens = closes.copy()
    if highs is None:
        highs = [max(o, c) for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) for o, c in zip(opens, closes)]

    # Pad to minimum length (engine requires >=50 bars)
    if n < min_bars:
        pad = min_bars - n
        closes = closes + [closes[-1]] * pad
        opens = opens + [opens[-1]] * pad
        highs = highs + [highs[-1]] * pad
        lows = lows + [lows[-1]] * pad
        n = min_bars

    return pd.DataFrame({
        "timestamp": [start_ts + i * interval_ms for i in range(n)],
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
    })


# ── Config Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def zero_cost_config():
    return BacktestConfig(
        commission_pct=0.0,
        slippage_pct=0.0,
        risk_per_trade=0.01,
        max_notional_pct=2.0,
    )


@pytest.fixture
def default_config():
    return BacktestConfig()
