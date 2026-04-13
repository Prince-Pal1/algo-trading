"""Tests for intrabar path reconstruction (Brownian bridge + pessimistic)."""

from __future__ import annotations

import pytest

from src.backtest.path import (
    Bar,
    BrownianBridgeModel,
    PessimisticPathModel,
    check_sl_tp_hits,
)


class TestBarDataclass:
    def test_bar_construction(self):
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0, volume=1000.0)
        assert bar.open == 2400.0
        assert bar.high == 2410.0
        assert bar.low == 2395.0
        assert bar.close == 2405.0


class TestBrownianBridgeTouch:
    def _bar(self) -> Bar:
        return Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)

    def test_level_inside_range_is_touched(self):
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar()
        assert model.level_is_touched(bar, 2400.0) is True
        assert model.level_is_touched(bar, 2395.0) is True
        assert model.level_is_touched(bar, 2410.0) is True
        assert model.level_is_touched(bar, 2403.0) is True

    def test_level_outside_range_not_touched(self):
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar()
        assert model.level_is_touched(bar, 2390.0) is False
        assert model.level_is_touched(bar, 2415.0) is False


class TestBrownianBridgeFraction:
    def _bar_up(self) -> Bar:
        """Bar that closes above open — monotonic upward."""
        return Bar(open=2400.0, high=2410.0, low=2398.0, close=2408.0)

    def _bar_down(self) -> Bar:
        """Bar that closes below open — monotonic downward."""
        return Bar(open=2410.0, high=2412.0, low=2400.0, close=2402.0)

    def test_untouched_level_returns_none(self):
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar_up()
        assert model.fraction_into_bar(bar, 2500.0) is None

    def test_linear_interp_between_open_and_close(self):
        """Level at midpoint between open and close → fraction ~0.5."""
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar_up()  # open 2400, close 2408
        frac = model.fraction_into_bar(bar, 2404.0)
        assert frac == pytest.approx(0.5, rel=1e-6)

    def test_level_equal_to_open(self):
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar_up()  # open 2400
        frac = model.fraction_into_bar(bar, 2400.0)
        assert frac == pytest.approx(0.0, abs=1e-6)

    def test_level_equal_to_close(self):
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar_up()  # close 2408
        frac = model.fraction_into_bar(bar, 2408.0)
        assert frac == pytest.approx(1.0, abs=1e-6)

    def test_level_in_spike_zone_deterministic(self):
        """Level above high-of-bar excursion (touched but not on monotonic path)
        gets a deterministic fraction in [0.15, 0.50]."""
        model = BrownianBridgeModel(run_id="test")
        bar = self._bar_up()  # open 2400 → close 2408, high 2410
        # Level 2409.5 is between close and high — touched but off the monotonic path
        frac = model.fraction_into_bar(bar, 2409.5, bar_idx=5)
        assert frac is not None
        assert 0.15 <= frac <= 0.50

    def test_reproducibility(self):
        """Same bar + same bar_idx + same run_id → identical fraction."""
        model_a = BrownianBridgeModel(run_id="runA")
        model_b = BrownianBridgeModel(run_id="runA")
        bar = self._bar_up()
        frac_a = model_a.fraction_into_bar(bar, 2409.5, bar_idx=5)
        frac_b = model_b.fraction_into_bar(bar, 2409.5, bar_idx=5)
        assert frac_a == frac_b

    def test_different_run_ids_produce_different_fractions(self):
        """Different run_ids should give different intrabar spike fractions."""
        model_a = BrownianBridgeModel(run_id="runA")
        model_b = BrownianBridgeModel(run_id="runB")
        bar = self._bar_up()
        # Use a level in the spike zone (not on monotonic path)
        frac_a = model_a.fraction_into_bar(bar, 2409.5, bar_idx=5)
        frac_b = model_b.fraction_into_bar(bar, 2409.5, bar_idx=5)
        # Statistically these should be different (tiny chance of collision)
        # If this flakes, it means the RNG is broken
        assert frac_a != frac_b


class TestBrownianBridgeWorstFavorable:
    def test_long_worst_adverse_is_low(self):
        model = BrownianBridgeModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        assert model.worst_adverse_price(bar, "LONG") == 2395.0

    def test_short_worst_adverse_is_high(self):
        model = BrownianBridgeModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        assert model.worst_adverse_price(bar, "SHORT") == 2410.0

    def test_long_best_favorable_is_high(self):
        model = BrownianBridgeModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        assert model.best_favorable_price(bar, "LONG") == 2410.0

    def test_short_best_favorable_is_low(self):
        model = BrownianBridgeModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        assert model.best_favorable_price(bar, "SHORT") == 2395.0

    def test_invalid_side_raises(self):
        model = BrownianBridgeModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        with pytest.raises(ValueError, match="side must"):
            model.worst_adverse_price(bar, "BUY")


class TestIntrabarEvents:
    def test_events_sorted_by_fraction(self):
        model = BrownianBridgeModel(run_id="test")
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2408.0)
        # Multiple levels: 2404 at 0.5, 2408 at 1.0, 2400 at 0.0
        levels = [
            (2404.0, "tp", 1),
            (2408.0, "sl", 2),
            (2400.0, "margin_call", 3),
        ]
        events = model.intrabar_events(bar, levels, bar_idx=0)
        assert len(events) == 3
        # Should be sorted by fraction_into_bar ascending
        fracs = [e.fraction_into_bar for e in events]
        assert fracs == sorted(fracs)

    def test_untouched_levels_excluded(self):
        model = BrownianBridgeModel(run_id="test")
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2408.0)
        levels = [
            (2500.0, "tp_far", 1),   # untouched
            (2404.0, "tp", 2),        # touched
        ]
        events = model.intrabar_events(bar, levels, bar_idx=0)
        assert len(events) == 1
        assert events[0].label == "tp"


class TestCheckSlTpHits:
    def test_neither_hit(self):
        bar = Bar(open=2400.0, high=2405.0, low=2395.0, close=2400.0)
        model = BrownianBridgeModel(run_id="test")
        label, price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=2390.0, take_profit=2420.0,
            path_model=model, bar_idx=0,
        )
        assert label is None
        assert price is None

    def test_long_sl_hit_only(self):
        bar = Bar(open=2400.0, high=2401.0, low=2385.0, close=2395.0)
        model = BrownianBridgeModel(run_id="test")
        label, price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=2390.0, take_profit=2420.0,
            path_model=model, bar_idx=0,
        )
        assert label == "sl"
        assert price == 2390.0

    def test_long_tp_hit_only(self):
        bar = Bar(open=2400.0, high=2425.0, low=2398.0, close=2422.0)
        model = BrownianBridgeModel(run_id="test")
        label, price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=2380.0, take_profit=2420.0,
            path_model=model, bar_idx=0,
        )
        assert label == "tp"
        assert price == 2420.0

    def test_both_hit_bridge_picks_earlier(self):
        """Bar where both SL and TP are inside the range — the model picks
        whichever is closer to the monotonic open→close path."""
        bar = Bar(open=2400.0, high=2420.0, low=2380.0, close=2405.0)
        model = BrownianBridgeModel(run_id="test")
        # SL at 2390 (below open-to-close path's linear interpolation)
        # TP at 2418 (above open-to-close path)
        # Both touched; both OFF the monotonic path so both use spike fractions
        label, price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=2390.0, take_profit=2418.0,
            path_model=model, bar_idx=0,
        )
        # Should pick one (deterministically given bar_idx and run_id)
        assert label in ("sl", "tp")
        assert price in (2390.0, 2418.0)

    def test_short_sl_hit(self):
        bar = Bar(open=2400.0, high=2415.0, low=2399.0, close=2410.0)
        model = BrownianBridgeModel(run_id="test")
        label, price = check_sl_tp_hits(
            bar=bar, side="SHORT", stop_loss=2410.0, take_profit=2380.0,
            path_model=model, bar_idx=0,
        )
        assert label == "sl"

    def test_no_sl_no_tp_returns_none(self):
        bar = Bar(open=2400.0, high=2405.0, low=2395.0, close=2402.0)
        model = BrownianBridgeModel(run_id="test")
        label, price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=None, take_profit=None,
            path_model=model, bar_idx=0,
        )
        assert label is None

    def test_invalid_side_raises(self):
        bar = Bar(open=2400.0, high=2405.0, low=2395.0, close=2402.0)
        model = BrownianBridgeModel(run_id="test")
        with pytest.raises(ValueError, match="side must"):
            check_sl_tp_hits(
                bar=bar, side="BUY", stop_loss=2390.0, take_profit=None,
                path_model=model, bar_idx=0,
            )


class TestPessimisticPathModel:
    def test_long_sl_adverse_first(self):
        """Pessimistic: for a LONG, SL at adverse side hits early (fraction 0.25)."""
        model = PessimisticPathModel()
        bar = Bar(open=2400.0, high=2410.0, low=2390.0, close=2405.0)
        # SL below open → adverse → early
        frac = model.fraction_into_bar(bar, 2395.0, side="LONG")
        assert frac == 0.25

    def test_long_tp_favorable_late(self):
        model = PessimisticPathModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        # TP above open → favorable → late
        frac = model.fraction_into_bar(bar, 2408.0, side="LONG")
        assert frac == 0.75

    def test_untouched_level_returns_none(self):
        model = PessimisticPathModel()
        bar = Bar(open=2400.0, high=2410.0, low=2395.0, close=2405.0)
        assert model.fraction_into_bar(bar, 2500.0, side="LONG") is None
