"""Tests for AlternateTimeframeBuilder — SWIFT strategy alt-TF helper (task #103)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.timeframe import AlternateTimeframeBuilder


def _bar(open_: float, high: float, low: float, close: float, volume: float = 100.0, ts: int = 0) -> pd.Series:
    return pd.Series({
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "timestamp": ts,
    })


class TestConstruction:
    def test_positive_multiplier_ok(self):
        b = AlternateTimeframeBuilder(multiplier=8)
        assert b.has_history(0) is True
        assert b.has_history(1) is False

    def test_zero_multiplier_raises(self):
        with pytest.raises(ValueError, match="multiplier must be > 0"):
            AlternateTimeframeBuilder(multiplier=0)

    def test_negative_multiplier_raises(self):
        with pytest.raises(ValueError, match="multiplier must be > 0"):
            AlternateTimeframeBuilder(multiplier=-3)


class TestAggregation:
    def test_multiplier_2_emits_every_2_bars(self):
        b = AlternateTimeframeBuilder(multiplier=2)
        out1 = b.feed(_bar(100, 105, 99, 102, volume=50, ts=1000))
        assert out1 is None  # still pending
        out2 = b.feed(_bar(102, 108, 101, 107, volume=60, ts=2000))
        assert out2 is not None
        # Alt bar = OHLCV over 2 base bars
        assert out2["open"] == 100.0
        assert out2["high"] == 108.0  # max of 105, 108
        assert out2["low"] == 99.0    # min of 99, 101
        assert out2["close"] == 107.0
        assert out2["volume"] == 110.0  # sum of 50, 60
        assert out2["timestamp"] == 1000  # timestamp of first bar

    def test_multiplier_8_m5_to_h40(self):
        """8 × M5 = 40 minutes — simulates the SWIFT default."""
        b = AlternateTimeframeBuilder(multiplier=8)
        for i in range(7):
            r = b.feed(_bar(100 + i, 101 + i, 99 + i, 100.5 + i, ts=i * 300_000))
            assert r is None
        # 8th bar closes the alt
        r = b.feed(_bar(107, 115, 106, 113, ts=7 * 300_000))
        assert r is not None
        assert r["open"] == 100.0  # first bar's open
        assert r["close"] == 113.0  # 8th bar's close
        assert r["high"] == 115.0   # max across all
        assert r["low"] == 99.0     # min across all
        assert r["timestamp"] == 0  # first bar's timestamp

    def test_multiple_alt_bars_accumulate_history(self):
        b = AlternateTimeframeBuilder(multiplier=3)
        # Feed 9 base bars → 3 alt bars
        for i in range(9):
            b.feed(_bar(100 + i, 101 + i, 99 + i, 100.5 + i, ts=i * 60_000))
        assert b.has_history(3) is True
        assert b.has_history(4) is False
        history = b.history()
        assert len(history) == 3
        # First alt bar: 100..102.5, second: 103..105.5, third: 106..108.5
        assert history[0]["open"] == 100.0
        assert history[0]["close"] == 102.5
        assert history[1]["open"] == 103.0
        assert history[1]["close"] == 105.5
        assert history[2]["open"] == 106.0
        assert history[2]["close"] == 108.5


class TestHistoryAccess:
    def _build(self) -> AlternateTimeframeBuilder:
        b = AlternateTimeframeBuilder(multiplier=2)
        for i in range(10):  # 5 alt bars
            b.feed(_bar(100 + i, 101 + i, 99 + i, 100.5 + i, ts=i * 60_000))
        return b

    def test_history_all(self):
        b = self._build()
        assert len(b.history()) == 5

    def test_history_last_n(self):
        b = self._build()
        last_2 = b.history(2)
        assert len(last_2) == 2
        assert last_2[0]["open"] == 106.0
        assert last_2[1]["open"] == 108.0

    def test_history_as_dataframe(self):
        b = self._build()
        df = b.history_as_dataframe()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 5
        assert set(df.columns) >= {"open", "high", "low", "close", "volume", "timestamp"}

    def test_history_as_dataframe_empty(self):
        b = AlternateTimeframeBuilder(multiplier=2)
        df = b.history_as_dataframe()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


class TestReset:
    def test_reset_clears_history_and_pending(self):
        b = AlternateTimeframeBuilder(multiplier=3)
        # Accumulate some state
        for i in range(5):  # 1 closed alt + 2 pending
            b.feed(_bar(100 + i, 101 + i, 99 + i, 100.5 + i))
        assert b.has_history(1) is True

        b.reset()
        assert b.has_history(1) is False
        assert len(b.history()) == 0

        # After reset, next bar should be treated as start of new alt
        r = b.feed(_bar(200, 201, 199, 200.5))
        assert r is None  # still pending


class TestHistorySize:
    def test_history_size_limits_buffer(self):
        """history_size bounds how many closed alt bars we retain."""
        b = AlternateTimeframeBuilder(multiplier=2, history_size=3)
        # Feed 10 base bars → 5 alt bars; history should retain last 3
        for i in range(10):
            b.feed(_bar(100 + i, 101 + i, 99 + i, 100.5 + i, ts=i * 60_000))
        assert len(b.history()) == 3
        # Last 3 should be alt bars 2, 3, 4 (closed from bars 4-5, 6-7, 8-9)
        assert b.history()[-1]["open"] == 108.0
