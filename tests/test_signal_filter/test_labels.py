"""Tests for src/m3s/signal_filter/labels.py — triple barrier + meta label."""

from __future__ import annotations

import pytest

from src.m3s.signal_filter.labels import (
    BarrierHit,
    meta_label_from_outcome,
    triple_barrier_label,
)


# ══════════════════════════════════════════════════════════════════════
# triple_barrier_label — LONG direction
# ══════════════════════════════════════════════════════════════════════


class TestTripleBarrierLong:
    def test_profit_target_hit_first(self):
        """LONG: price rises past PT before touching SL."""
        # entry=100, vol=0.02, pt_mult=2, sl_mult=1
        # PT = 100 * (1 + 2*0.02) = 104
        # SL = 100 * (1 - 1*0.02) = 98
        future = [101.0, 102.5, 104.5, 103.0]  # PT hit at index 2
        result = triple_barrier_label(
            entry_price=100.0, direction=1, future_prices=future,
            pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.label == 1
        assert result.barrier_hit == BarrierHit.PROFIT_TARGET
        assert result.exit_index == 2

    def test_stop_loss_hit_first(self):
        """LONG: price drops past SL before touching PT."""
        future = [99.0, 97.5, 98.5, 99.0]  # SL (98) hit at index 1
        result = triple_barrier_label(
            entry_price=100.0, direction=1, future_prices=future,
            pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.label == -1
        assert result.barrier_hit == BarrierHit.STOP_LOSS
        assert result.exit_index == 1

    def test_time_expiry_positive_residual(self):
        """LONG: barriers not hit, but exit price > entry → label +1."""
        future = [100.5, 101.0, 101.5, 102.0]  # never hits 104 or 98
        result = triple_barrier_label(
            entry_price=100.0, direction=1, future_prices=future,
            pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.label == 1
        assert result.barrier_hit == BarrierHit.TIME
        assert result.exit_index == 3

    def test_time_expiry_negative_residual(self):
        """LONG: barriers not hit, exit < entry → label −1."""
        future = [99.5, 99.0, 98.5, 99.0]  # never hits 104 or 98 (98.5 > 98)
        result = triple_barrier_label(
            entry_price=100.0, direction=1, future_prices=future,
            pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.label == -1
        assert result.barrier_hit == BarrierHit.TIME


# ══════════════════════════════════════════════════════════════════════
# triple_barrier_label — SHORT direction
# ══════════════════════════════════════════════════════════════════════


class TestTripleBarrierShort:
    def test_short_profit_target_hit(self):
        """SHORT: price drops past PT (lower band)."""
        # entry=100, vol=0.02, pt_mult=2, sl_mult=1
        # For SHORT: pt_level = 100 * (1 - 2*0.02) = 96
        # sl_level = 100 * (1 + 1*0.02) = 102
        future = [99.0, 97.0, 95.5, 96.5]  # PT (96) hit at index 2
        result = triple_barrier_label(
            entry_price=100.0, direction=-1, future_prices=future,
            pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.label == 1
        assert result.barrier_hit == BarrierHit.PROFIT_TARGET
        assert result.exit_index == 2

    def test_short_stop_loss_hit(self):
        """SHORT: price rises past SL (upper band)."""
        future = [101.0, 102.5, 103.0]  # SL (102) hit at index 1
        result = triple_barrier_label(
            entry_price=100.0, direction=-1, future_prices=future,
            pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.label == -1
        assert result.barrier_hit == BarrierHit.STOP_LOSS
        assert result.exit_index == 1


# ══════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════


class TestValidation:
    def test_invalid_direction_rejected(self):
        with pytest.raises(ValueError, match="direction"):
            triple_barrier_label(
                entry_price=100.0, direction=0,
                future_prices=[100.0], pt_mult=2.0, sl_mult=1.0, volatility=0.02,
            )

    def test_zero_entry_rejected(self):
        with pytest.raises(ValueError, match="entry_price"):
            triple_barrier_label(
                entry_price=0.0, direction=1,
                future_prices=[100.0], pt_mult=2.0, sl_mult=1.0, volatility=0.02,
            )

    def test_zero_volatility_rejected(self):
        with pytest.raises(ValueError, match="volatility"):
            triple_barrier_label(
                entry_price=100.0, direction=1,
                future_prices=[100.0], pt_mult=2.0, sl_mult=1.0, volatility=0.0,
            )

    def test_empty_future_returns_time_barrier(self):
        result = triple_barrier_label(
            entry_price=100.0, direction=1,
            future_prices=[], pt_mult=2.0, sl_mult=1.0, volatility=0.02,
        )
        assert result.barrier_hit == BarrierHit.TIME
        assert result.label == 0
        assert result.exit_price == 100.0


# ══════════════════════════════════════════════════════════════════════
# meta_label_from_outcome
# ══════════════════════════════════════════════════════════════════════


class TestMetaLabelFromOutcome:
    def test_long_pt_hit(self):
        tb, meta = meta_label_from_outcome(
            direction=1, entry_price=100.0, exit_price=104.0, barrier_hit="pt",
        )
        assert tb == 1
        assert meta == 1

    def test_long_sl_hit(self):
        tb, meta = meta_label_from_outcome(
            direction=1, entry_price=100.0, exit_price=98.0, barrier_hit="sl",
        )
        assert tb == -1
        assert meta == 0

    def test_short_pt_hit(self):
        tb, meta = meta_label_from_outcome(
            direction=-1, entry_price=100.0, exit_price=96.0, barrier_hit="pt",
        )
        assert tb == 1
        assert meta == 1

    def test_time_exit_positive_residual_long(self):
        tb, meta = meta_label_from_outcome(
            direction=1, entry_price=100.0, exit_price=101.0, barrier_hit="time",
        )
        assert tb == 1
        assert meta == 1

    def test_time_exit_negative_residual_long(self):
        tb, meta = meta_label_from_outcome(
            direction=1, entry_price=100.0, exit_price=99.0, barrier_hit="time",
        )
        assert tb == -1
        assert meta == 0

    def test_signal_exit_interpreted_as_time(self):
        """Strategy-driven CLOSE uses residual sign, same as time expiry."""
        tb, meta = meta_label_from_outcome(
            direction=1, entry_price=100.0, exit_price=100.5, barrier_hit="signal",
        )
        assert tb == 1
        assert meta == 1

    def test_invalid_direction_rejected(self):
        with pytest.raises(ValueError, match="direction"):
            meta_label_from_outcome(
                direction=0, entry_price=100.0, exit_price=100.0, barrier_hit="pt",
            )
