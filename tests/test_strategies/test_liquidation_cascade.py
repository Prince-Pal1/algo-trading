"""Unit tests for LiquidationCascadeStrategy (Stage 2)."""

from __future__ import annotations

import pandas as pd

from src.strategies.event_driven.liquidation_cascade import LiquidationCascadeStrategy
from src.utils.types import RiskProfile, SignalAction


def _make(**overrides):
    """Build a strategy with test-friendly defaults (short warmup)."""
    defaults = dict(
        name="liquidation_cascade",
        markets=["BTCUSDT"],
        timeframe="1m",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.01,
        cascade_sigma=4.0,
        cascade_min_move_bps=50.0,
        rolling_window_bars=240,  # small so warmup (240//4 = 60) reaches fast
        sl_bps=200.0,
        tp_bps=100.0,
        max_hold_bars=30,
        cooldown_bars=5,           # small cooldown for test
        long_only=True,
    )
    defaults.update(overrides)
    return LiquidationCascadeStrategy(**defaults)


def _feed_benign_bars(strat, n: int, base_price: float = 65_000.0, step_bps: float = 1.0):
    """Push n bars of small random-ish returns to warm up the buffer.

    step_bps=1 means each bar is ±1 bp of the last close. Produces a
    rolling std near 1 bp once warmed up — cascade is then easy to
    trigger with a larger move.
    """
    price = base_price
    for i in range(n):
        # Alternate tiny up/down to keep price roughly stationary
        price = price * (1 + (step_bps / 10_000.0) * (1 if i % 2 == 0 else -1))
        strat.process("BTCUSDT", "1m", pd.Series({"close": price, "high": price, "low": price}))
    return price


def test_warmup_emits_no_signal():
    """During warmup (< rolling_window_bars // 4 bars), strategy stays FLAT."""
    strat = _make(rolling_window_bars=240)
    warmup_threshold = 240 // 4  # 60 bars
    price = 65_000.0
    for i in range(warmup_threshold - 1):
        price = price * (1.0001 if i % 2 == 0 else 0.9999)
        sig = strat.process("BTCUSDT", "1m", pd.Series({"close": price}))
        assert sig is None, f"expected no signal during warmup at bar {i}, got {sig}"
    assert strat._position == "FLAT"


def test_cascade_triggers_long_after_warmup():
    """After warmup, a large negative 1-minute return (>4σ) fires a LONG signal."""
    strat = _make(rolling_window_bars=240, cooldown_bars=0)
    # Warm up with small benign moves (~1 bp alternating)
    last_price = _feed_benign_bars(strat, n=120, base_price=65_000.0, step_bps=1.0)
    # Now feed a big negative move: -100 bps (std was ~1bp so z = -100)
    cascade_price = last_price * (1 - 0.01)  # -100 bps
    sig = strat.process("BTCUSDT", "1m", pd.Series({"close": cascade_price}))
    assert sig is not None, "expected LONG signal on cascade bar"
    assert sig.action == SignalAction.LONG
    assert sig.metadata["entry_reason"] == "cascade_down"
    assert sig.metadata["z_score"] <= -4.0  # z must exceed sigma threshold
    assert strat._position == "LONG"


def test_sl_fires_on_adverse_move_after_entry():
    """After LONG entry, drop through -200 bps stop → CLOSE signal (exit_reason='sl')."""
    strat = _make(rolling_window_bars=240, cooldown_bars=0, sl_bps=200.0)
    # Warmup + cascade entry
    last_price = _feed_benign_bars(strat, n=120, base_price=65_000.0, step_bps=1.0)
    entry_price = last_price * (1 - 0.01)  # -100 bp cascade bar
    entry_sig = strat.process("BTCUSDT", "1m", pd.Series({"close": entry_price}))
    assert entry_sig is not None and entry_sig.action == SignalAction.LONG
    # Now drop another 200+ bps below entry
    sl_breach = entry_price * (1 - 0.021)  # -210 bps from entry (below SL)
    exit_sig = strat.process("BTCUSDT", "1m", pd.Series({"close": sl_breach}))
    assert exit_sig is not None
    assert exit_sig.action == SignalAction.CLOSE
    assert exit_sig.metadata["exit_reason"] == "sl"
    assert strat._position == "FLAT"


def test_tp_fires_on_favorable_move():
    """After LONG entry, retrace up by +100 bps → CLOSE signal (exit_reason='tp')."""
    strat = _make(rolling_window_bars=240, cooldown_bars=0, tp_bps=100.0)
    last_price = _feed_benign_bars(strat, n=120, base_price=65_000.0, step_bps=1.0)
    entry_price = last_price * (1 - 0.01)
    entry_sig = strat.process("BTCUSDT", "1m", pd.Series({"close": entry_price}))
    assert entry_sig.action == SignalAction.LONG
    tp_breach = entry_price * (1 + 0.0105)  # +105 bps above entry (tp hit)
    exit_sig = strat.process("BTCUSDT", "1m", pd.Series({"close": tp_breach}))
    assert exit_sig is not None
    assert exit_sig.action == SignalAction.CLOSE
    assert exit_sig.metadata["exit_reason"] == "tp"


def test_time_stop_closes_position():
    """After max_hold_bars bars without SL/TP, position time-stops."""
    strat = _make(rolling_window_bars=240, cooldown_bars=0, max_hold_bars=5)
    last_price = _feed_benign_bars(strat, n=120, base_price=65_000.0, step_bps=1.0)
    entry_price = last_price * (1 - 0.01)
    strat.process("BTCUSDT", "1m", pd.Series({"close": entry_price}))
    assert strat._position == "LONG"
    # Feed 5 quiet bars (no SL/TP hit) — should trigger time stop on the 5th
    flat_price = entry_price  # unchanged
    for i in range(5):
        sig = strat.process("BTCUSDT", "1m", pd.Series({"close": flat_price}))
        if sig is not None and sig.action == SignalAction.CLOSE:
            assert sig.metadata["exit_reason"] == "time_stop"
            assert strat._position == "FLAT"
            return
    raise AssertionError("time stop never fired within max_hold_bars")


def test_from_config_reads_config_section():
    """from_config() wires in all params from config/strategies.toml."""
    s = LiquidationCascadeStrategy.from_config("liquidation_cascade")
    assert s.markets == ["BTCUSDT"]
    assert s.timeframe == "1m"
    assert s.cascade_sigma == 4.0
    assert s.sl_bps == 200.0
    assert s.tp_bps == 100.0
    assert s.max_hold_bars == 30
    assert s.long_only is True
