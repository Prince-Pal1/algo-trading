"""Unit tests for ScalperStrategy helpers.

Tests the rolling history buffer, multi-bar velocity, volume
confirmation, structural break detection, and intrabar sub-bar
accessors.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.path import M1SubBar
from src.strategies.base_scalper import ScalperStrategy
from src.utils.types import RiskProfile, Signal


class _NoopScalper(ScalperStrategy):
    """Bare ScalperStrategy for testing — on_features does nothing."""

    def __init__(self, **kwargs):
        super().__init__(
            name="noop_scalper",
            markets=["XAUUSD"],
            timeframe="5m",
            risk_profile=RiskProfile.AGGRESSIVE,
            max_risk_per_trade=0.01,
            leverage_range=(1.0, 10.0),
            **kwargs,
        )

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        return None


def _bar(close: float, atr: float = 10.0, volume: float = 500.0, **kwargs) -> pd.Series:
    data = {
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": volume,
        "timestamp": 0,
        "ATR_20": atr,
    }
    data.update(kwargs)
    return pd.Series(data)


class TestHistoryBuffer:
    def test_fills_to_max_len(self):
        s = _NoopScalper(history_size=5)
        for i in range(5):
            s.process("XAUUSD", "5m", _bar(100.0 + i))
        assert len(s._history) == 5

    def test_rotates_past_max_len(self):
        s = _NoopScalper(history_size=5)
        for i in range(10):
            s.process("XAUUSD", "5m", _bar(100.0 + i))
        # Only the last 5 should be retained
        assert len(s._history) == 5
        closes = [float(b["close"]) for b in s._history]
        assert closes == [105.0, 106.0, 107.0, 108.0, 109.0]


class TestMultiBarVelocity:
    def test_insufficient_data_returns_none(self):
        s = _NoopScalper()
        s.process("XAUUSD", "5m", _bar(100.0))
        s.process("XAUUSD", "5m", _bar(101.0))
        # Only 2 bars in history, need n+1 = 4 for velocity(3)
        assert s.multi_bar_velocity(3) is None

    def test_monotonic_up_is_positive(self):
        s = _NoopScalper()
        for c in [100.0, 101.0, 102.0, 103.0]:
            s.process("XAUUSD", "5m", _bar(c, atr=10.0))
        # velocity_3 = (103 - 100) / (3 * 10) = 0.1
        v = s.multi_bar_velocity(3)
        assert v is not None
        assert v == pytest.approx(0.1)

    def test_monotonic_down_is_negative(self):
        s = _NoopScalper()
        for c in [103.0, 102.0, 101.0, 100.0]:
            s.process("XAUUSD", "5m", _bar(c, atr=10.0))
        v = s.multi_bar_velocity(3)
        assert v is not None
        assert v == pytest.approx(-0.1)

    def test_flat_is_zero(self):
        s = _NoopScalper()
        for _ in range(5):
            s.process("XAUUSD", "5m", _bar(100.0))
        v = s.multi_bar_velocity(3)
        assert v == 0.0

    def test_atr_missing_returns_none(self):
        s = _NoopScalper()
        for c in [100.0, 101.0, 102.0, 103.0]:
            b = pd.Series({
                "open": c, "high": c + 0.1, "low": c - 0.1,
                "close": c, "volume": 500.0, "timestamp": 0,
                # no ATR_20
            })
            s.process("XAUUSD", "5m", b)
        assert s.multi_bar_velocity(3) is None

    def test_atr_zero_returns_none(self):
        s = _NoopScalper()
        for c in [100.0, 101.0, 102.0, 103.0]:
            s.process("XAUUSD", "5m", _bar(c, atr=0.0))
        assert s.multi_bar_velocity(3) is None


class TestIsMultiBarBurst:
    def test_true_when_velocity_above_threshold(self):
        s = _NoopScalper()
        # velocity_3 = (10) / (3*1) = 3.33 → way above mult=1.5
        for c in [100.0, 103.0, 106.0, 110.0]:
            s.process("XAUUSD", "5m", _bar(c, atr=1.0))
        assert s.is_multi_bar_burst(n=3, mult=1.5) is True

    def test_false_when_velocity_below_threshold(self):
        s = _NoopScalper()
        for c in [100.0, 100.5, 101.0, 101.2]:
            s.process("XAUUSD", "5m", _bar(c, atr=10.0))
        assert s.is_multi_bar_burst(n=3, mult=1.5) is False

    def test_false_on_insufficient_history(self):
        s = _NoopScalper()
        s.process("XAUUSD", "5m", _bar(100.0))
        assert s.is_multi_bar_burst() is False


class TestIsVolumeConfirmed:
    def test_fires_on_volume_spike(self):
        s = _NoopScalper()
        # 19 bars with volume 500, then 1 bar with volume 1500 (3× prior)
        for _ in range(19):
            s.process("XAUUSD", "5m", _bar(100.0, volume=500.0))
        s.process("XAUUSD", "5m", _bar(100.0, volume=1500.0))
        assert s.is_volume_confirmed(threshold=1.5, n=20) is True

    def test_false_on_low_volume(self):
        s = _NoopScalper()
        for _ in range(19):
            s.process("XAUUSD", "5m", _bar(100.0, volume=500.0))
        s.process("XAUUSD", "5m", _bar(100.0, volume=600.0))  # only 1.2×
        assert s.is_volume_confirmed(threshold=1.5, n=20) is False

    def test_false_on_insufficient_history(self):
        s = _NoopScalper()
        for _ in range(5):
            s.process("XAUUSD", "5m", _bar(100.0, volume=500.0))
        assert s.is_volume_confirmed(threshold=1.5, n=20) is False

    def test_zero_current_volume_returns_false(self):
        s = _NoopScalper()
        for _ in range(19):
            s.process("XAUUSD", "5m", _bar(100.0, volume=500.0))
        s.process("XAUUSD", "5m", _bar(100.0, volume=0.0))
        assert s.is_volume_confirmed() is False

    def test_zero_median_volume_returns_false(self):
        s = _NoopScalper()
        for _ in range(19):
            s.process("XAUUSD", "5m", _bar(100.0, volume=0.0))
        s.process("XAUUSD", "5m", _bar(100.0, volume=500.0))
        assert s.is_volume_confirmed() is False


class TestStructuralBreak:
    def test_broke_n_bar_high_true(self):
        s = _NoopScalper()
        for _ in range(10):
            s.process("XAUUSD", "5m", _bar(100.0))
        # New bar with high > all prior
        b = _bar(100.0)
        b["high"] = 105.0
        s.process("XAUUSD", "5m", b)
        assert s.broke_n_bar_high(n=10) is True

    def test_broke_n_bar_high_false(self):
        s = _NoopScalper()
        for _ in range(10):
            s.process("XAUUSD", "5m", _bar(100.0))
        # New bar with high below prior max
        s.process("XAUUSD", "5m", _bar(100.0))
        assert s.broke_n_bar_high(n=10) is False

    def test_broke_n_bar_low_true(self):
        s = _NoopScalper()
        for _ in range(10):
            s.process("XAUUSD", "5m", _bar(100.0))
        b = _bar(100.0)
        b["low"] = 95.0
        s.process("XAUUSD", "5m", b)
        assert s.broke_n_bar_low(n=10) is True

    def test_insufficient_history_returns_false(self):
        s = _NoopScalper()
        s.process("XAUUSD", "5m", _bar(100.0))
        assert s.broke_n_bar_high(n=10) is False
        assert s.broke_n_bar_low(n=10) is False


class TestIntrabarHelpers:
    def test_max_velocity_without_sub_bars_returns_none(self):
        b = _bar(100.0)
        assert ScalperStrategy.intrabar_max_velocity(b) is None

    def test_max_velocity_with_sub_bars(self):
        b = _bar(100.0)
        b["intrabar_sub_bars"] = [
            M1SubBar(open=100.0, high=100.5, low=99.5, close=100.3, volume=100.0),
            M1SubBar(open=100.3, high=101.2, low=100.0, close=101.0, volume=150.0),  # abs(101 - 100.3) = 0.7
            M1SubBar(open=101.0, high=101.1, low=100.8, close=100.9, volume=100.0),
            M1SubBar(open=100.9, high=101.0, low=100.5, close=100.6, volume=100.0),
            M1SubBar(open=100.6, high=100.8, low=100.4, close=100.5, volume=100.0),
        ]
        # Largest |close - open| = 0.7 on the 2nd sub-bar
        assert ScalperStrategy.intrabar_max_velocity(b) == pytest.approx(0.7)

    def test_max_velocity_empty_list_returns_none(self):
        b = _bar(100.0)
        b["intrabar_sub_bars"] = []
        assert ScalperStrategy.intrabar_max_velocity(b) is None

    def test_body_direction_up(self):
        b = _bar(100.0)
        b["intrabar_sub_bars"] = [
            M1SubBar(open=100.0, high=100.5, low=99.5, close=100.3, volume=100.0),
            M1SubBar(open=100.3, high=101.0, low=100.0, close=100.8, volume=100.0),
        ]
        assert ScalperStrategy.intrabar_body_direction(b) == 1

    def test_body_direction_down(self):
        b = _bar(100.0)
        b["intrabar_sub_bars"] = [
            M1SubBar(open=100.5, high=100.6, low=99.8, close=99.9, volume=100.0),
            M1SubBar(open=99.9, high=100.0, low=99.2, close=99.3, volume=100.0),
        ]
        assert ScalperStrategy.intrabar_body_direction(b) == -1

    def test_body_direction_flat(self):
        b = _bar(100.0)
        b["intrabar_sub_bars"] = [
            M1SubBar(open=100.0, high=100.5, low=99.5, close=100.5, volume=100.0),
            M1SubBar(open=100.5, high=100.6, low=100.0, close=100.0, volume=100.0),  # -0.5
            # net = +0.5 - 0.5 = 0
        ]
        assert ScalperStrategy.intrabar_body_direction(b) == 0

    def test_volume_total(self):
        b = _bar(100.0)
        b["intrabar_sub_bars"] = [
            M1SubBar(open=100.0, high=100.5, low=99.5, close=100.3, volume=100.0),
            M1SubBar(open=100.3, high=101.0, low=100.0, close=100.8, volume=150.0),
            M1SubBar(open=100.8, high=101.1, low=100.5, close=100.9, volume=200.0),
        ]
        assert ScalperStrategy.intrabar_volume_total(b) == 450.0

    def test_body_direction_without_sub_bars_returns_none(self):
        assert ScalperStrategy.intrabar_body_direction(_bar(100.0)) is None
