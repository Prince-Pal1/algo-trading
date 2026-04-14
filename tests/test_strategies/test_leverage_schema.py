"""Tests for the G.0c leverage-first schema changes.

Covers Signal.leverage, Signal.margin_used_pct, and BaseStrategy.leverage_range.
These are the foundational schema pieces — the actual M3S Compounder leverage
scalar, RiskManager leverage gates, and PaperExecutor margin tracking are
built on top in follow-up commits.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction


class _DummyStrategy(BaseStrategy):
    """Minimal concrete strategy used in tests."""

    def on_features(self, symbol, timeframe, features):
        return None


class TestSignalLeverageFields:
    def test_signal_default_leverage_is_none(self):
        sig = Signal(
            symbol="BTCUSDT",
            action=SignalAction.LONG,
            confidence=0.8,
            strategy_name="test",
            timeframe="1h",
        )
        assert sig.leverage is None
        assert sig.margin_used_pct is None

    def test_signal_with_explicit_leverage(self):
        sig = Signal(
            symbol="XAUUSD",
            action=SignalAction.LONG,
            confidence=0.8,
            strategy_name="test",
            timeframe="5m",
            leverage=50.0,
            margin_used_pct=0.02,
        )
        assert sig.leverage == 50.0
        assert sig.margin_used_pct == 0.02


class TestBaseStrategyLeverageRange:
    def test_default_leverage_range_is_unit(self):
        """Pre-G.0c behavior: default strategies get (1.0, 1.0) — no leverage."""
        s = _DummyStrategy(
            name="test",
            markets=["BTCUSDT"],
            timeframe="1h",
        )
        assert s.leverage_range == (1.0, 1.0)

    def test_explicit_leverage_range(self):
        s = _DummyStrategy(
            name="gold_scalper",
            markets=["XAUUSD"],
            timeframe="5m",
            leverage_range=(10.0, 200.0),
        )
        assert s.leverage_range == (10.0, 200.0)

    def test_min_below_one_raises(self):
        with pytest.raises(ValueError, match="leverage_range"):
            _DummyStrategy(
                name="bad",
                markets=["X"],
                timeframe="1h",
                leverage_range=(0.5, 10.0),
            )

    def test_max_below_min_raises(self):
        with pytest.raises(ValueError, match="leverage_range"):
            _DummyStrategy(
                name="bad",
                markets=["X"],
                timeframe="1h",
                leverage_range=(10.0, 5.0),
            )

    def test_zero_range_raises(self):
        with pytest.raises(ValueError, match="leverage_range"):
            _DummyStrategy(
                name="bad",
                markets=["X"],
                timeframe="1h",
                leverage_range=(0.0, 1.0),
            )

    def test_min_equals_max_is_valid(self):
        """A strategy can pin leverage to a single value by setting min == max."""
        s = _DummyStrategy(
            name="fixed_leverage",
            markets=["XAUUSD"],
            timeframe="1h",
            leverage_range=(25.0, 25.0),
        )
        assert s.leverage_range == (25.0, 25.0)


class TestExistingStrategiesUnchanged:
    """Regression check — existing crypto strategies shouldn't have changed."""

    def test_funding_carry_still_imports(self):
        from src.strategies.carry.funding_carry import FundingCarryStrategy
        s = FundingCarryStrategy(
            name="funding_carry",
            markets=["BTCUSDT-CARRY"],
            timeframe="8h",
        )
        # Default (1, 1) — funding_carry doesn't declare a range yet
        assert s.leverage_range == (1.0, 1.0)

    def test_vol_momentum_still_imports(self):
        from src.strategies.momentum.vol_momentum import VolMomentumStrategy
        s = VolMomentumStrategy(
            name="vol_momentum",
            markets=["BTCUSDT"],
            timeframe="1h",
        )
        assert s.leverage_range == (1.0, 1.0)
