from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.data.feature_engine import _compute_indicators
from src.strategies.trend_following.donchian_gold import (
    DonchianGoldStrategy,
    _is_in_session,
)
from src.utils.types import Signal, SignalAction


def _ts(h: int, m: int = 0) -> int:
    """Fixed Tuesday 2024-01-02 at the given hour/minute."""
    dt = datetime(2024, 1, 2, h, m, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class TestSessionWindows:
    def test_london_morning_is_in_session(self):
        assert _is_in_session(_ts(8, 30)) is True
        assert _is_in_session(_ts(7, 0)) is True
        assert _is_in_session(_ts(11, 0)) is True

    def test_ny_afternoon_is_in_session(self):
        assert _is_in_session(_ts(14, 0)) is True
        assert _is_in_session(_ts(16, 30)) is True

    def test_asia_overnight_out_of_session(self):
        assert _is_in_session(_ts(2, 0)) is False
        assert _is_in_session(_ts(5, 0)) is False

    def test_between_sessions_out(self):
        # 12:00 UTC is between London close (11:00) and NY open (13:30)
        assert _is_in_session(_ts(12, 0)) is False


class TestStrategyConstruction:
    def test_defaults_xauusd_and_leverage(self):
        s = DonchianGoldStrategy()
        assert s.markets == ["XAUUSD"]
        assert s.timeframe == "1h"
        assert s.leverage_range == (10.0, 50.0)
        assert s.session_filter is True

    def test_leverage_range_forwarded_to_base(self):
        s = DonchianGoldStrategy(leverage_range=(5.0, 100.0))
        assert s.leverage_range == (5.0, 100.0)

    def test_invalid_leverage_range_rejected(self):
        with pytest.raises(ValueError, match="leverage_range"):
            DonchianGoldStrategy(leverage_range=(0.5, 100.0))


def _synthetic_breakout_df(n: int = 300) -> pd.DataFrame:
    """Flat 2400 baseline for ~250 bars then a clean breakout ramp.

    n >= 260 is required so donchian_120 has enough history to emit.
    """
    base_dt = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    ts = np.array([
        int((base_dt + timedelta(hours=i)).timestamp() * 1000) for i in range(n)
    ])
    prices = np.full(n, 2400.0, dtype=float)
    for i in range(250, n):
        prices[i] = 2400.0 + (i - 250) * 5.0
    opens = prices - 0.5
    closes = prices + 0.5
    highs = closes + 1.0
    lows = opens - 1.0
    return pd.DataFrame({
        "timestamp": ts,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.full(n, 100.0),
    })


def _stamp_all_at_hour(df: pd.DataFrame, hour: int, minute: int = 0) -> pd.DataFrame:
    """Overwrite every row's timestamp with the same hour-of-day, advancing
    only the day. Used to force a whole series into a single session window.
    """
    base = datetime(2024, 1, 2, hour, minute, tzinfo=timezone.utc)
    new_ts = [
        int((base + timedelta(days=i)).timestamp() * 1000) for i in range(len(df))
    ]
    df = df.copy()
    df["timestamp"] = new_ts
    return df


_INDICATORS = [
    "donchian_20", "donchian_55", "donchian_120",
    "atr_14", "adx_14",
]


def _run_strategy(df: pd.DataFrame, s: DonchianGoldStrategy) -> list[Signal]:
    df = _compute_indicators(df, _INDICATORS)
    signals: list[Signal] = []
    for i in range(len(df)):
        sig = s.process("XAUUSD", "1h", df.iloc[i])
        if sig is not None:
            signals.append(sig)
    return signals


class TestSignalGeneration:
    def test_session_filter_blocks_asia_entry(self):
        df = _stamp_all_at_hour(_synthetic_breakout_df(), hour=3)
        s = DonchianGoldStrategy(session_filter=True, adx_trend_threshold=None)
        entries = [
            sig for sig in _run_strategy(df, s)
            if sig.action in (SignalAction.LONG, SignalAction.SHORT)
        ]
        assert len(entries) == 0

    def test_session_filter_allows_london_entry(self):
        df = _stamp_all_at_hour(_synthetic_breakout_df(), hour=9)
        s = DonchianGoldStrategy(session_filter=True, adx_trend_threshold=None)
        entries = [
            sig for sig in _run_strategy(df, s)
            if sig.action in (SignalAction.LONG, SignalAction.SHORT)
        ]
        assert len(entries) >= 1
        assert entries[0].action == SignalAction.LONG

    def test_disabling_session_filter_allows_any_time(self):
        df = _stamp_all_at_hour(_synthetic_breakout_df(), hour=3)
        s = DonchianGoldStrategy(session_filter=False, adx_trend_threshold=None)
        entries = [
            sig for sig in _run_strategy(df, s)
            if sig.action in (SignalAction.LONG, SignalAction.SHORT)
        ]
        assert len(entries) >= 1
