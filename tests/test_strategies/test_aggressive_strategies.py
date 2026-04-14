from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest.costs import NewsWindow
from src.backtest.structure_levels import StructureLevel
from src.strategies.aggressive.candle_burst_hunter import CandleBurstHunterStrategy
from src.strategies.aggressive.hedged_structure_play import (
    HedgedStructurePlayStrategy,
    HedgeState,
)
from src.strategies.aggressive.news_spike_fade import NewsSpikeFadeStrategy
from src.utils.types import Signal, SignalAction


def _bar(
    open_: float, high: float, low: float, close: float,
    atr: float = 5.0, ts_ms: int = 0,
) -> pd.Series:
    return pd.Series({
        "timestamp": ts_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100.0,
        "ATR_20": atr,
        "ATR_14": atr,
    })


# ══════════════════════════════════════════════════════════════════════
# CandleBurstHunterStrategy
# ══════════════════════════════════════════════════════════════════════


class TestCandleBurstHunter:
    def test_no_entry_on_small_bar(self):
        s = CandleBurstHunterStrategy(burst_atr_mult=1.5, atr_period=20)
        b = _bar(2400.0, 2403.0, 2399.0, 2402.0, atr=5.0)
        assert s.process("XAUUSD", "5m", b) is None

    def test_long_entry_on_upward_burst(self):
        s = CandleBurstHunterStrategy(burst_atr_mult=1.5, atr_period=20)
        # Travel = 10, ATR = 5, 10 > 1.5 × 5
        b = _bar(2400.0, 2412.0, 2399.0, 2410.0, atr=5.0)
        sig = s.process("XAUUSD", "5m", b)
        assert sig is not None
        assert sig.action == SignalAction.LONG
        assert sig.stop_loss is not None
        assert sig.stop_loss < sig.entry_price

    def test_short_entry_on_downward_burst(self):
        s = CandleBurstHunterStrategy(burst_atr_mult=1.5, atr_period=20)
        b = _bar(2410.0, 2411.0, 2395.0, 2397.0, atr=5.0)
        sig = s.process("XAUUSD", "5m", b)
        assert sig is not None
        assert sig.action == SignalAction.SHORT

    def test_hard_sl_exit_on_long(self):
        s = CandleBurstHunterStrategy(burst_atr_mult=1.5, hard_sl_pct=0.003)
        # Enter long at 2400
        b1 = _bar(2390.0, 2402.0, 2389.0, 2400.0, atr=5.0)
        entry = s.process("XAUUSD", "5m", b1)
        assert entry.action == SignalAction.LONG
        # Drop that hits the 0.3% hard SL (2400 - 7.2 = 2392.8)
        b2 = _bar(2400.0, 2401.0, 2391.0, 2393.0, atr=5.0)
        exit_sig = s.process("XAUUSD", "5m", b2)
        assert exit_sig is not None
        assert exit_sig.action == SignalAction.CLOSE
        assert exit_sig.metadata["exit_reason"] == "hard_sl"

    def test_timeout_exit(self):
        s = CandleBurstHunterStrategy(
            burst_atr_mult=1.5, max_hold_bars=2, trail_activation_pips=200.0,
        )
        b1 = _bar(2390.0, 2402.0, 2389.0, 2400.0, atr=5.0)
        s.process("XAUUSD", "5m", b1)
        # Flat bars — no gain, trail never arms
        b2 = _bar(2400.0, 2400.1, 2399.9, 2400.0, atr=5.0)
        s.process("XAUUSD", "5m", b2)
        b3 = _bar(2400.0, 2400.1, 2399.9, 2400.0, atr=5.0)
        exit_sig = s.process("XAUUSD", "5m", b3)
        assert exit_sig is not None
        assert exit_sig.action == SignalAction.CLOSE
        assert exit_sig.metadata["exit_reason"] == "timeout"


# ══════════════════════════════════════════════════════════════════════
# NewsSpikeFadeStrategy
# ══════════════════════════════════════════════════════════════════════


def _ts(day: int, hour: int, minute: int = 0) -> int:
    dt = datetime(2025, 2, day, hour, minute, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class TestNewsSpikeFade:
    def test_no_entry_outside_news_window(self):
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(news_windows=[window], spike_trigger_pips=30.0)
        b = _bar(2400.0, 2402.5, 2399.5, 2402.0, ts_ms=_ts(6, 12, 0))
        assert s.process("XAUUSD", "5m", b) is None

    def test_fade_up_spike_with_short(self):
        """Enter the window at 2400, subsequent bar has high=2405 = 50 pip up-spike → fade short."""
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(news_windows=[window], spike_trigger_pips=30.0)

        # Pre-window bar at 12:00 — establishes pre_window_price=2400
        pre = _bar(2400.0, 2400.5, 2399.5, 2400.0, ts_ms=_ts(7, 13, 20))
        s.process("XAUUSD", "5m", pre)

        # First window bar — should capture pre_window_price and return None
        b1 = _bar(2400.0, 2401.0, 2399.5, 2400.5, ts_ms=_ts(7, 13, 26))
        sig1 = s.process("XAUUSD", "5m", b1)
        assert sig1 is None  # pre_window_price just captured, no excursion yet

        # Second window bar with high=2405 = 50 pip excursion from pre_price
        b2 = _bar(2400.5, 2405.0, 2400.0, 2404.5, ts_ms=_ts(7, 13, 31))
        sig2 = s.process("XAUUSD", "5m", b2)
        assert sig2 is not None
        assert sig2.action == SignalAction.SHORT
        assert sig2.stop_loss > sig2.entry_price

    def test_fade_down_spike_with_long(self):
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(news_windows=[window], spike_trigger_pips=30.0)

        pre = _bar(2400.0, 2400.5, 2399.5, 2400.0, ts_ms=_ts(7, 13, 20))
        s.process("XAUUSD", "5m", pre)
        b1 = _bar(2400.0, 2400.5, 2399.5, 2400.0, ts_ms=_ts(7, 13, 26))
        s.process("XAUUSD", "5m", b1)
        # Down excursion to 2395 = 50 pips
        b2 = _bar(2400.0, 2400.5, 2395.0, 2396.0, ts_ms=_ts(7, 13, 31))
        sig = s.process("XAUUSD", "5m", b2)
        assert sig is not None
        assert sig.action == SignalAction.LONG

    def test_target_exit(self):
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(
            news_windows=[window], spike_trigger_pips=30.0, target_pct=0.5,
        )
        pre = _bar(2400.0, 2400.5, 2399.5, 2400.0, ts_ms=_ts(7, 13, 20))
        s.process("XAUUSD", "5m", pre)
        b1 = _bar(2400.0, 2400.5, 2399.5, 2400.0, ts_ms=_ts(7, 13, 26))
        s.process("XAUUSD", "5m", b1)
        # Entry bar: 50 pip up-excursion → enter SHORT at 2404.5
        b2 = _bar(2400.5, 2405.0, 2400.0, 2404.5, ts_ms=_ts(7, 13, 31))
        s.process("XAUUSD", "5m", b2)
        # Target at 50% of 50-pip spike = 25 pips retracement
        # Entry 2404.5 - 25 pips = 2402.0
        b3 = _bar(2404.5, 2404.5, 2401.5, 2402.0, ts_ms=_ts(7, 13, 36))
        exit_sig = s.process("XAUUSD", "5m", b3)
        assert exit_sig is not None
        assert exit_sig.action == SignalAction.CLOSE
        assert exit_sig.metadata["exit_reason"] == "target"


# ══════════════════════════════════════════════════════════════════════
# HedgedStructurePlayStrategy — state machine
# ══════════════════════════════════════════════════════════════════════


class TestHedgedStructurePlay:
    def test_flat_to_primary(self):
        s = HedgedStructurePlayStrategy()
        s.set_structure_levels([])
        b = _bar(2400.0, 2402.0, 2399.0, 2401.0, atr=5.0)
        sig = s.process("XAUUSD", "5m", b)
        assert sig is not None
        assert sig.action == SignalAction.LONG
        assert s._s.state == HedgeState.PRIMARY_LONG

    def test_primary_to_hedged_on_adverse(self):
        s = HedgedStructurePlayStrategy(hedge_trigger_pct=0.01)
        # 1: enter primary long at 2401
        s.process("XAUUSD", "5m", _bar(2400.0, 2402.0, 2399.0, 2401.0, atr=5.0))
        # 2: price drops 1.1% from 2401 → 2374.6, low = 2370
        b2 = _bar(2401.0, 2401.0, 2370.0, 2374.0, atr=5.0)
        sig = s.process("XAUUSD", "5m", b2)
        assert sig is not None
        # Should be opening the hedge (short)
        assert sig.action == SignalAction.SHORT
        assert s._s.state == HedgeState.HEDGED_FROM_LONG

    def test_hedge_timeout_force_closes(self):
        s = HedgedStructurePlayStrategy(hedge_trigger_pct=0.01, max_bars_in_hedge=2)
        s.process("XAUUSD", "5m", _bar(2400.0, 2402.0, 2399.0, 2401.0, atr=5.0))
        # Trigger hedge
        s.process("XAUUSD", "5m", _bar(2401.0, 2401.0, 2370.0, 2374.0, atr=5.0))
        # No structure levels → timeout after max_bars_in_hedge
        s.process("XAUUSD", "5m", _bar(2374.0, 2376.0, 2370.0, 2374.0, atr=5.0))
        exit_sig = s.process("XAUUSD", "5m", _bar(2374.0, 2376.0, 2370.0, 2374.0, atr=5.0))
        assert exit_sig is not None
        assert exit_sig.action == SignalAction.CLOSE
        assert exit_sig.metadata["exit_reason"] == "hedge_timeout"
        assert s._s.state == HedgeState.FLAT

    def test_primary_gain_exit(self):
        s = HedgedStructurePlayStrategy()
        s.process("XAUUSD", "5m", _bar(2400.0, 2402.0, 2399.0, 2401.0, atr=5.0))
        # Run up 1%+: 2401 → 2425
        b = _bar(2401.0, 2426.0, 2401.0, 2425.0, atr=5.0)
        sig = s.process("XAUUSD", "5m", b)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "primary_gain"
        assert s._s.state == HedgeState.FLAT
