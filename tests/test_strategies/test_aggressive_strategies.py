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
        # 20 pip move OUTSIDE the window
        b = _bar(2400.0, 2402.5, 2399.5, 2402.0, ts_ms=_ts(6, 12, 0))
        assert s.process("XAUUSD", "5m", b) is None

    def test_fade_up_spike_with_short(self):
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(news_windows=[window], spike_trigger_pips=30.0, pip_size=0.10)
        # 50 pip UP spike INSIDE the window: open→close = 5.0, 50 pips > 30
        b = _bar(2400.0, 2405.0, 2399.5, 2405.0, ts_ms=_ts(7, 13, 30))
        sig = s.process("XAUUSD", "5m", b)
        assert sig is not None
        assert sig.action == SignalAction.SHORT
        assert sig.stop_loss > sig.entry_price

    def test_fade_down_spike_with_long(self):
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(news_windows=[window], spike_trigger_pips=30.0)
        b = _bar(2400.0, 2400.5, 2395.0, 2395.0, ts_ms=_ts(7, 13, 30))
        sig = s.process("XAUUSD", "5m", b)
        assert sig is not None
        assert sig.action == SignalAction.LONG

    def test_target_1_exit(self):
        window = NewsWindow(start_ms=_ts(7, 13, 25), end_ms=_ts(7, 13, 45), label="NFP")
        s = NewsSpikeFadeStrategy(
            news_windows=[window], spike_trigger_pips=30.0, target_1_pct=0.5,
        )
        # Enter SHORT on up-spike at 2405
        b1 = _bar(2400.0, 2405.5, 2399.5, 2405.0, ts_ms=_ts(7, 13, 30))
        s.process("XAUUSD", "5m", b1)
        # target_1 = entry - 0.5 × spike = 2405 - 2.5 = 2402.5
        b2 = _bar(2405.0, 2405.5, 2402.0, 2403.0, ts_ms=_ts(7, 13, 35))
        exit_sig = s.process("XAUUSD", "5m", b2)
        assert exit_sig is not None
        assert exit_sig.action == SignalAction.CLOSE
        assert exit_sig.metadata["exit_reason"] == "target_1"


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
