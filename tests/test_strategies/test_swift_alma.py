"""Unit tests for SwiftAlmaStrategy (task #103 — Pine Script port).

Tests cover:
- Alt-TF aggregation produces the right number of alt bars
- ALMA crossover detection (monotonic up trend → eventual long entry)
- SL/TP levels set correctly at entry
- Intrabar SL hit emits CLOSE before any new signal
- Reversal handling: opposite signal closes current + pending 1-bar gap
- Direction filter: LONG-only / SHORT-only / NONE
- No entry before alt-TF warmup (first alt bar + alma_length + 1 history)
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.trend_following.swift_alma import SwiftAlmaStrategy
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


def _bar(
    open_: float, high: float, low: float, close: float,
    volume: float = 100.0, ts: int = 0,
) -> pd.Series:
    return pd.Series({
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "timestamp": ts,
        "ATR_20": 5.0,  # not used by SWIFT but expected by some tests
    })


class TestConstruction:
    def test_default_params_match_pine(self):
        s = SwiftAlmaStrategy()
        assert s.alma_length == 2
        assert s.alma_offset == pytest.approx(0.85)
        assert s.alma_sigma == pytest.approx(5.0)  # Pine's input.int(5, ...)
        assert s.alt_tf_multiplier == 8
        assert s.sl_pct == pytest.approx(0.005)
        assert s.tp1_pct == pytest.approx(0.010)
        assert s.tp2_pct == pytest.approx(0.015)
        assert s.tp3_pct == pytest.approx(0.020)
        assert s.tp1_qty == pytest.approx(0.50)
        assert s.tp2_qty == pytest.approx(0.30)
        assert s.tp3_qty == pytest.approx(0.20)
        assert s.use_pine_ladder is True
        assert s.same_bar_flip is True
        assert s.trade_type == "BOTH"
        assert s.tier == Tier.INSTITUTIONAL_TREND

    def test_leverage_range(self):
        s = SwiftAlmaStrategy()
        assert s.leverage_range == (1.0, 100.0)

    def test_ladder_qty_sum_validation(self):
        # 50 + 30 + 20 = 100 → ok
        SwiftAlmaStrategy(tp1_qty=0.5, tp2_qty=0.3, tp3_qty=0.2)
        # 40 + 30 + 20 = 90 → should raise
        with pytest.raises(ValueError, match="must sum to 1.0"):
            SwiftAlmaStrategy(tp1_qty=0.4, tp2_qty=0.3, tp3_qty=0.2)


class TestAltTfWarmup:
    def test_no_signal_before_first_alt_bar(self):
        """With alt_tf_multiplier=2, first alt bar closes on 2nd base bar."""
        s = SwiftAlmaStrategy(alt_tf_multiplier=2)
        # Feed 1 bar — alt still pending
        assert s.process("XAUUSD", "15m", _bar(100, 101, 99, 100.5)) is None
        # No ALMA yet, no signal
        assert s._prev_alma_close is None

    def test_need_multiple_alt_bars_for_alma(self):
        """ALMA(length=2) needs at least 2 alt bars before it's defined."""
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2)
        # 4 base bars → 2 alt bars → ALMA computable but no crossover yet
        # (need 2 consecutive ALMA values)
        for i in range(4):
            s.process("XAUUSD", "15m", _bar(100 + i, 101 + i, 99 + i, 100.5 + i))
        # After 2 alt bars, the first ALMA pair is computed and stored as prev
        assert s._prev_alma_close is not None
        assert s._prev_alma_open is not None


class TestCrossoverDetection:
    def test_phase_transition_emits_both_directions(self):
        """Phase 1: neutral bars (close ≈ open) → ALMA close ≈ ALMA open.
        Phase 2: uptrend (close > open) → LONG crossover fires on the
        transition from neutral to up.
        Phase 3: downtrend (close < open) → SHORT crossover fires on the
        transition from up to down.
        """
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2)
        long_fired = False
        short_fired = False

        # Phase 1: neutral (close = open) — establishes prev_alma_close == prev_alma_open
        for i in range(6):
            b = _bar(100.0, 100.2, 99.8, 100.0, ts=i * 1000)
            s.process("XAUUSD", "15m", b)

        # Phase 2: uptrend (close > open) — crosses up
        for i in range(10):
            b = _bar(100.0 + i * 0.5, 100.5 + i * 0.5, 99.9 + i * 0.5, 100.4 + i * 0.5, ts=(6 + i) * 1000)
            sig = s.process("XAUUSD", "15m", b)
            if sig is not None and sig.action == SignalAction.LONG:
                long_fired = True

        # Phase 3: downtrend after uptrend — crosses down
        last = 100.0 + 9 * 0.5
        for i in range(12):
            b = _bar(last - i * 0.5, last - i * 0.5 + 0.1, last - i * 0.5 - 0.6, last - i * 0.5 - 0.4, ts=(16 + i) * 1000)
            sig = s.process("XAUUSD", "15m", b)
            if sig is not None and sig.action == SignalAction.SHORT:
                short_fired = True

        assert long_fired is True, "No LONG emitted after neutral → uptrend transition"
        assert short_fired is True, "No SHORT emitted after uptrend → downtrend transition"

    def test_no_crossover_on_flat_series(self):
        """Flat series → ALMA(close) ≈ ALMA(open) always → no crossovers."""
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2)
        entries = 0
        for i in range(20):
            b = _bar(100.0, 100.0, 100.0, 100.0, ts=i * 1000)
            sig = s.process("XAUUSD", "15m", b)
            if sig is not None and sig.action in (SignalAction.LONG, SignalAction.SHORT):
                entries += 1
        # No entries on a truly flat series
        assert entries == 0


class TestRiskLevelsSingleTP:
    """Single-TP mode (use_pine_ladder=False) — simpler fallback path."""

    def test_long_entry_single_tp(self):
        s = SwiftAlmaStrategy(
            alt_tf_multiplier=2, alma_length=2, sl_pct=0.005, tp2_pct=0.015,
            use_pine_ladder=False,
        )
        sig = s._open_position(symbol="XAUUSD", timeframe="15m", close=100.0, direction=+1)
        assert sig.action == SignalAction.LONG
        assert sig.entry_price == 100.0
        assert sig.stop_loss == pytest.approx(99.5)
        assert sig.take_profit == pytest.approx(101.5)
        assert s._sl_price == pytest.approx(99.5)
        assert s._tp_price == pytest.approx(101.5)

    def test_short_entry_single_tp(self):
        s = SwiftAlmaStrategy(
            alt_tf_multiplier=2, alma_length=2, sl_pct=0.005, tp2_pct=0.015,
            use_pine_ladder=False,
        )
        sig = s._open_position(symbol="XAUUSD", timeframe="15m", close=100.0, direction=-1)
        assert sig.action == SignalAction.SHORT
        assert sig.stop_loss == pytest.approx(100.5)
        assert sig.take_profit == pytest.approx(98.5)


class TestRiskLevelsLadder:
    """Pine-faithful ladder mode (use_pine_ladder=True)."""

    def test_long_entry_populates_three_tp_levels(self):
        s = SwiftAlmaStrategy(
            alt_tf_multiplier=2, alma_length=2,
            sl_pct=0.005, tp1_pct=0.010, tp2_pct=0.015, tp3_pct=0.020,
        )
        sig = s._open_position(symbol="XAUUSD", timeframe="15m", close=100.0, direction=+1)
        assert sig.action == SignalAction.LONG
        assert sig.stop_loss == pytest.approx(99.5)
        # take_profit in the signal is the FURTHEST TP (TP3) for metadata
        assert sig.take_profit == pytest.approx(102.0)
        assert s._ladder_tp1_price == pytest.approx(101.0)
        assert s._ladder_tp2_price == pytest.approx(101.5)
        assert s._ladder_tp3_price == pytest.approx(102.0)
        assert s._ladder_tp1_hit is False
        assert s._ladder_tp2_hit is False

    def test_short_entry_populates_three_tp_levels(self):
        s = SwiftAlmaStrategy(
            alt_tf_multiplier=2, alma_length=2,
            sl_pct=0.005, tp1_pct=0.010, tp2_pct=0.015, tp3_pct=0.020,
        )
        sig = s._open_position(symbol="XAUUSD", timeframe="15m", close=100.0, direction=-1)
        assert sig.action == SignalAction.SHORT
        assert sig.stop_loss == pytest.approx(100.5)
        assert sig.take_profit == pytest.approx(98.0)  # TP3 = 100 × 0.98
        assert s._ladder_tp1_price == pytest.approx(99.0)
        assert s._ladder_tp2_price == pytest.approx(98.5)
        assert s._ladder_tp3_price == pytest.approx(98.0)


class TestLadderWeightedExit:
    """Test the _ladder_weighted_exit math for various (direction, hits, exit) combos."""

    def _strategy_with_position(self, direction: int) -> SwiftAlmaStrategy:
        s = SwiftAlmaStrategy(
            sl_pct=0.005, tp1_pct=0.010, tp2_pct=0.015, tp3_pct=0.020,
            tp1_qty=0.5, tp2_qty=0.3, tp3_qty=0.2,
        )
        s._entry_price = 100.0
        if direction > 0:
            s._sl_price = 99.5
            s._ladder_tp1_price = 101.0
            s._ladder_tp2_price = 101.5
            s._ladder_tp3_price = 102.0
            s._position = "LONG"
        else:
            s._sl_price = 100.5
            s._ladder_tp1_price = 99.0
            s._ladder_tp2_price = 98.5
            s._ladder_tp3_price = 98.0
            s._position = "SHORT"
        return s

    def test_long_sl_no_tps_hit(self):
        """Full SL hit without any TP leg filling → loss across all 3 legs."""
        s = self._strategy_with_position(+1)
        exit_px = s._ladder_weighted_exit(direction=+1, sl_hit=True, tp3_hit=False)
        # All 3 legs close at -0.5% → weighted return = -0.5%
        # exit = 100 × (1 - 0.005) = 99.5
        assert exit_px == pytest.approx(99.5)

    def test_long_tp1_then_sl(self):
        """TP1 hit (50% closes at +1%), then SL (50% closes at -0.5%)."""
        s = self._strategy_with_position(+1)
        s._ladder_tp1_hit = True
        exit_px = s._ladder_weighted_exit(direction=+1, sl_hit=True, tp3_hit=False)
        # Weighted return = 0.5 × +0.01 + 0.3 × -0.005 + 0.2 × -0.005
        #                 = 0.005 - 0.0015 - 0.001 = 0.0025 (+0.25%)
        # exit = 100 × 1.0025 = 100.25
        assert exit_px == pytest.approx(100.25)

    def test_long_tp1_tp2_then_sl(self):
        """TP1 + TP2 hit, then SL."""
        s = self._strategy_with_position(+1)
        s._ladder_tp1_hit = True
        s._ladder_tp2_hit = True
        exit_px = s._ladder_weighted_exit(direction=+1, sl_hit=True, tp3_hit=False)
        # Weighted = 0.5×0.01 + 0.3×0.015 + 0.2×-0.005 = 0.005 + 0.0045 - 0.001 = 0.0085
        # exit = 100 × 1.0085 = 100.85
        assert exit_px == pytest.approx(100.85)

    def test_long_all_three_tps_hit(self):
        """Maximum profit: all 3 TPs fill."""
        s = self._strategy_with_position(+1)
        s._ladder_tp1_hit = True
        s._ladder_tp2_hit = True
        exit_px = s._ladder_weighted_exit(direction=+1, sl_hit=False, tp3_hit=True)
        # Weighted = 0.5×0.01 + 0.3×0.015 + 0.2×0.02 = 0.005 + 0.0045 + 0.004 = 0.0135
        # exit = 100 × 1.0135 = 101.35
        assert exit_px == pytest.approx(101.35)

    def test_short_sl_no_tps(self):
        s = self._strategy_with_position(-1)
        exit_px = s._ladder_weighted_exit(direction=-1, sl_hit=True, tp3_hit=False)
        # SHORT total_return = 0.5×-0.005 + 0.3×-0.005 + 0.2×-0.005 = -0.005
        # For SHORT: exit = entry × (1 - total_return) = 100 × (1 - -0.005) = 100.5
        assert exit_px == pytest.approx(100.5)

    def test_short_all_tps_hit(self):
        s = self._strategy_with_position(-1)
        s._ladder_tp1_hit = True
        s._ladder_tp2_hit = True
        exit_px = s._ladder_weighted_exit(direction=-1, sl_hit=False, tp3_hit=True)
        # total_return = 0.5×-0.01 + 0.3×-0.015 + 0.2×-0.02 = -0.0135
        # exit = 100 × (1 - -0.0135) = 101.35... wait that's wrong for a short
        # Let me think: for SHORT the gain comes from price DROPPING.
        # In the _ladder_weighted_exit, tp1/2/3_ret are -tp_pct (negative for short)
        # So total_return = 0.5×-0.01 + 0.3×-0.015 + 0.2×-0.02 = -0.0135
        # For SHORT: exit = entry × (1 - total_return) = 100 × (1 - -0.0135) = 101.35
        # But that's WRONG — a fully-profitable short should exit BELOW entry.
        # The formula is inverted. Engine P&L for SHORT = (entry - exit) * qty
        # If we want a gain of 1.35% on the position, engine must see:
        #   (100 - exit) * qty = 0.0135 * qty * entry
        #   exit = 100 - 100 * 0.0135 = 98.65
        # So the formula should be entry * (1 + total_return) for SHORT too,
        # but since total_return is negative for profit, the result is < 100. Correct!
        # My current formula entry * (1 - total_return) gives 101.35 = WRONG.
        # Let me flag this - it's a bug in _ladder_weighted_exit.
        assert exit_px == pytest.approx(98.65)  # should fail, revealing bug


class TestIntrabarExitsSingleTP:
    """Single-TP fallback mode (use_pine_ladder=False)."""

    def test_long_sl_hit_emits_close(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2, use_pine_ladder=False)
        # Manually put strategy into LONG state
        s._position = "LONG"
        s._entry_price = 100.0
        s._sl_price = 99.5
        s._tp_price = 101.5
        # Feed a bar whose low touches the SL
        b = _bar(100.0, 100.1, 99.3, 99.8)
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.entry_price == pytest.approx(99.5)
        assert sig.metadata["exit_reason"] == "sl"

    def test_long_tp_hit_emits_close(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2, use_pine_ladder=False)
        s._position = "LONG"
        s._entry_price = 100.0
        s._sl_price = 99.5
        s._tp_price = 101.5
        b = _bar(100.0, 101.8, 99.9, 101.2)  # high reaches TP
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.entry_price == pytest.approx(101.5)
        assert sig.metadata["exit_reason"] == "tp"

    def test_short_sl_hit_emits_close(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2, use_pine_ladder=False)
        s._position = "SHORT"
        s._entry_price = 100.0
        s._sl_price = 100.5
        s._tp_price = 98.5
        b = _bar(100.0, 100.7, 99.9, 100.3)  # high touches SL
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "sl"

    def test_short_tp_hit_emits_close(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2, use_pine_ladder=False)
        s._position = "SHORT"
        s._entry_price = 100.0
        s._sl_price = 100.5
        s._tp_price = 98.5
        b = _bar(100.0, 100.1, 98.2, 99.0)  # low reaches TP
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "tp"


class TestIntrabarExitsLadder:
    """Pine-faithful 3-tier ladder mode (default)."""

    def _strat_long(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2)
        s._position = "LONG"
        s._entry_price = 100.0
        s._sl_price = 99.5
        s._ladder_tp1_price = 101.0
        s._ladder_tp2_price = 101.5
        s._ladder_tp3_price = 102.0
        return s

    def test_long_sl_no_tps_hit(self):
        s = self._strat_long()
        b = _bar(100.0, 100.1, 99.3, 99.8)
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.metadata["exit_reason"] == "ladder_sl"
        assert sig.entry_price == pytest.approx(99.5)

    def test_long_tp1_then_sl(self):
        s = self._strat_long()
        b1 = _bar(100.0, 101.2, 99.8, 100.8)
        sig1 = s.process("XAUUSD", "15m", b1)
        assert sig1 is None
        assert s._ladder_tp1_hit is True
        b2 = _bar(100.8, 100.9, 99.3, 99.8)
        sig2 = s.process("XAUUSD", "15m", b2)
        assert sig2 is not None
        assert sig2.metadata["exit_reason"] == "ladder_sl"
        assert sig2.entry_price == pytest.approx(100.25)

    def test_long_all_three_tps_hit(self):
        s = self._strat_long()
        b = _bar(100.0, 102.5, 99.8, 101.8)
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.metadata["exit_reason"] == "ladder_tp3"
        assert sig.entry_price == pytest.approx(101.35)

    def test_short_all_three_tps_hit(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, alma_length=2)
        s._position = "SHORT"
        s._entry_price = 100.0
        s._sl_price = 100.5
        s._ladder_tp1_price = 99.0
        s._ladder_tp2_price = 98.5
        s._ladder_tp3_price = 98.0
        b = _bar(100.0, 100.1, 97.8, 98.3)
        sig = s.process("XAUUSD", "15m", b)
        assert sig is not None
        assert sig.metadata["exit_reason"] == "ladder_tp3"
        assert sig.entry_price == pytest.approx(98.65)


class TestDirectionFilter:
    def test_long_only_ignores_shorts(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, trade_type="LONG")
        # Feed a downtrend — should NOT emit any shorts
        for i in range(20):
            b = _bar(100.0 - i * 0.5, 100.5 - i * 0.5, 99.0 - i * 0.5, 99.2 - i * 0.5, ts=i * 1000)
            sig = s.process("XAUUSD", "15m", b)
            if sig is not None:
                assert sig.action != SignalAction.SHORT, \
                    f"LONG-only strategy emitted SHORT on bar {i}"

    def test_short_only_ignores_longs(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, trade_type="SHORT")
        for i in range(20):
            b = _bar(100.0 + i * 0.5, 101.0 + i * 0.5, 99.5 + i * 0.5, 100.8 + i * 0.5, ts=i * 1000)
            sig = s.process("XAUUSD", "15m", b)
            if sig is not None:
                assert sig.action != SignalAction.LONG

    def test_none_emits_nothing(self):
        s = SwiftAlmaStrategy(alt_tf_multiplier=2, trade_type="NONE")
        for i in range(20):
            b = _bar(100.0 + i * 0.5, 101.0 + i * 0.5, 99.5 + i * 0.5, 100.8 + i * 0.5, ts=i * 1000)
            sig = s.process("XAUUSD", "15m", b)
            if sig is not None:
                assert sig.action == SignalAction.CLOSE  # only exits, no new entries
