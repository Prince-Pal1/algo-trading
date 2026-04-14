"""Tests for intrabar path reconstruction (Brownian bridge + M1 + pessimistic)."""

from __future__ import annotations

import pytest

from src.backtest.path import (
    Bar,
    BrownianBridgeModel,
    M1PathModel,
    PessimisticPathModel,
    check_sl_tp_hits,
    get_or_build_m1_path_model,
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


class TestM1PathModel:
    """M1PathModel uses M1 sub-bars as ground truth for intrabar ordering.

    For a bar spanning ts_ms = T with 5 M1 sub-bars at T, T+60k, T+120k,
    T+180k, T+240k, the fraction_into_bar for a level that first appears
    in sub-bar N is (N + 0.5) / 5.
    """

    def _simple_m5(self) -> Bar:
        """M5 bar at ts_ms=1000, open=100, high=110, low=90, close=95."""
        return Bar(open=100.0, high=110.0, low=90.0, close=95.0, ts_ms=1000)

    def _build_model(self, sub_bars: list[tuple[float, float]]) -> M1PathModel:
        """Build an M1 model with 5 sub-bars at ts=1000, 61000, ..., 241000."""
        lookup = {}
        for i, (lo, hi) in enumerate(sub_bars):
            lookup[1000 + i * 60_000] = (lo, hi)
        return M1PathModel(m1_lookup=lookup, sub_bar_count=5, sub_bar_duration_ms=60_000)

    def test_level_in_first_sub_bar_returns_0_1(self):
        # Sub-bar 0 low/high = (95, 105) contains the level 100
        sub_bars = [(95.0, 105.0), (92.0, 100.0), (90.0, 99.0), (91.0, 97.0), (93.0, 95.0)]
        model = self._build_model(sub_bars)
        bar = self._simple_m5()
        frac = model.fraction_into_bar(bar, 100.0)
        # First touch is in sub-bar 0 → (0 + 0.5) / 5 = 0.1
        assert frac == pytest.approx(0.1)

    def test_level_in_third_sub_bar_returns_0_5(self):
        # Level = 92 hits first at sub-bar 2 (index 2, fraction = 2.5/5 = 0.5)
        sub_bars = [(95.0, 105.0), (94.0, 100.0), (90.0, 98.0), (91.0, 97.0), (93.0, 95.0)]
        model = self._build_model(sub_bars)
        bar = self._simple_m5()
        frac = model.fraction_into_bar(bar, 92.0)
        assert frac == pytest.approx(0.5)

    def test_level_in_last_sub_bar_returns_0_9(self):
        # Level = 96 only appears in sub-bar 4 (the close)
        sub_bars = [(98.0, 105.0), (97.5, 100.0), (97.0, 99.0), (97.0, 98.0), (94.0, 97.0)]
        # Level 96 is in sub-bar 4 only
        model = self._build_model(sub_bars)
        bar = Bar(open=100.0, high=105.0, low=94.0, close=95.0, ts_ms=1000)
        frac = model.fraction_into_bar(bar, 96.0)
        assert frac == pytest.approx(0.9)

    def test_level_outside_range_returns_none(self):
        sub_bars = [(95.0, 105.0), (92.0, 100.0), (90.0, 99.0), (91.0, 97.0), (93.0, 95.0)]
        model = self._build_model(sub_bars)
        bar = self._simple_m5()
        assert model.fraction_into_bar(bar, 200.0) is None
        assert model.fraction_into_bar(bar, 50.0) is None

    def test_missing_m1_data_falls_back_to_bridge(self):
        """No M1 data → delegates to BrownianBridgeModel fallback."""
        model = M1PathModel(m1_lookup={}, sub_bar_count=5, run_id="test")
        bar = Bar(open=100.0, high=110.0, low=90.0, close=105.0, ts_ms=9999)
        # Level inside range, no M1 data — should return a bridge estimate
        frac = model.fraction_into_bar(bar, 102.0)
        assert frac is not None
        assert 0.0 <= frac <= 1.0
        # Fallback counter ticked
        assert model.stats["bridge_fallback_count"] == 1
        assert model.stats["m1_hits"] == 0

    def test_m1_coverage_ratio_1_0_when_all_hits(self):
        sub_bars = [(95.0, 105.0), (92.0, 100.0), (90.0, 99.0), (91.0, 97.0), (93.0, 95.0)]
        model = self._build_model(sub_bars)
        bar = self._simple_m5()
        # 3 queries, all hit M1
        model.fraction_into_bar(bar, 100.0)
        model.fraction_into_bar(bar, 92.0)
        model.fraction_into_bar(bar, 94.0)
        assert model.m1_coverage_ratio() == pytest.approx(1.0)

    def test_m1_coverage_ratio_mixed(self):
        sub_bars = [(95.0, 105.0), (92.0, 100.0), (90.0, 99.0), (91.0, 97.0), (93.0, 95.0)]
        lookup = {}
        for i, (lo, hi) in enumerate(sub_bars):
            lookup[1000 + i * 60_000] = (lo, hi)
        model = M1PathModel(m1_lookup=lookup, sub_bar_count=5)
        # Bar with M1 coverage
        bar_good = Bar(open=100.0, high=110.0, low=90.0, close=95.0, ts_ms=1000)
        # Bar WITHOUT M1 coverage (ts_ms=99999 not in lookup)
        bar_bad = Bar(open=100.0, high=110.0, low=90.0, close=95.0, ts_ms=99999)
        model.fraction_into_bar(bar_good, 100.0)  # m1 hit
        model.fraction_into_bar(bar_bad, 100.0)   # bridge fallback
        # 1/2 = 50% M1 coverage
        assert model.m1_coverage_ratio() == pytest.approx(0.5)

    def test_untouched_level_does_not_increment_fallback(self):
        sub_bars = [(95.0, 105.0), (92.0, 100.0), (90.0, 99.0), (91.0, 97.0), (93.0, 95.0)]
        model = self._build_model(sub_bars)
        bar = self._simple_m5()
        model.fraction_into_bar(bar, 200.0)  # outside range
        # Doesn't count as a bridge fallback — counts as level_outside_range
        assert model.stats["bridge_fallback_count"] == 0
        assert model.stats["level_outside_range"] == 1

    def test_level_is_touched_matches_bar_range(self):
        model = self._build_model([(95.0, 105.0)] * 5)
        bar = Bar(open=100.0, high=110.0, low=90.0, close=105.0)
        assert model.level_is_touched(bar, 100.0) is True
        assert model.level_is_touched(bar, 85.0) is False
        assert model.level_is_touched(bar, 115.0) is False
        assert model.level_is_touched(bar, 110.0) is True  # edge
        assert model.level_is_touched(bar, 90.0) is True   # edge

    def test_worst_adverse_price_matches_bridge(self):
        model = self._build_model([(95.0, 105.0)] * 5)
        bar = Bar(open=100.0, high=110.0, low=85.0, close=102.0)
        assert model.worst_adverse_price(bar, "LONG") == 85.0
        assert model.worst_adverse_price(bar, "SHORT") == 110.0
        with pytest.raises(ValueError):
            model.worst_adverse_price(bar, "INVALID")

    def test_best_favorable_price(self):
        model = self._build_model([(95.0, 105.0)] * 5)
        bar = Bar(open=100.0, high=110.0, low=85.0, close=102.0)
        assert model.best_favorable_price(bar, "LONG") == 110.0
        assert model.best_favorable_price(bar, "SHORT") == 85.0

    def test_intrabar_events_ordering(self):
        """Two levels in different sub-bars should emit events in time order."""
        # sub-bar 0 has (95, 105); level 100 in sub-bar 0 → frac 0.1
        # sub-bar 3 has (85, 90); level 88 in sub-bar 3 → frac 0.7
        sub_bars = [(95.0, 105.0), (97.0, 103.0), (96.0, 102.0), (85.0, 90.0), (86.0, 88.0)]
        model = self._build_model(sub_bars)
        bar = Bar(open=100.0, high=105.0, low=85.0, close=87.0, ts_ms=1000)
        events = model.intrabar_events(bar, [
            (100.0, "tp_a", 1),
            (88.0, "sl_b", 2),
        ])
        assert len(events) == 2
        # TP at 100 hit first (sub-bar 0, frac 0.1)
        assert events[0].label == "tp_a"
        assert events[0].fraction_into_bar == pytest.approx(0.1)
        # SL at 88 hit later (sub-bar 3, frac 0.7)
        assert events[1].label == "sl_b"
        assert events[1].fraction_into_bar == pytest.approx(0.7)

    def test_check_sl_tp_hits_uses_m1_ordering(self):
        """check_sl_tp_hits correctly identifies SL-first via M1 sub-bars."""
        # LONG position. SL at 91, TP at 104.
        # Sub-bar 0 (95,105) touches TP (104) → frac 0.1
        # Sub-bar 1 (96,100) → neither
        # Sub-bar 2 (91,98) touches SL (91) → frac 0.5
        # Because TP hit first (0.1 < 0.5), the trade closes at TP.
        sub_bars = [(95.0, 105.0), (96.0, 100.0), (91.0, 98.0), (95.0, 99.0), (96.0, 99.0)]
        lookup = {1000 + i * 60_000: sb for i, sb in enumerate(sub_bars)}
        model = M1PathModel(m1_lookup=lookup, sub_bar_count=5)
        bar = Bar(open=100.0, high=105.0, low=91.0, close=99.0, ts_ms=1000)
        hit_label, hit_price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=91.0, take_profit=104.0,
            path_model=model,
        )
        assert hit_label == "tp"
        assert hit_price == 104.0

    def test_check_sl_tp_hits_sl_first(self):
        """Reverse ordering: SL hit first in M1 sub-bar 0, TP later."""
        # Sub-bar 0 (91, 95) touches SL (92)
        # Sub-bar 3 (95, 106) touches TP (104)
        sub_bars = [(91.0, 95.0), (92.0, 97.0), (94.0, 100.0), (95.0, 106.0), (98.0, 105.0)]
        lookup = {1000 + i * 60_000: sb for i, sb in enumerate(sub_bars)}
        model = M1PathModel(m1_lookup=lookup, sub_bar_count=5)
        bar = Bar(open=100.0, high=106.0, low=91.0, close=103.0, ts_ms=1000)
        hit_label, hit_price = check_sl_tp_hits(
            bar=bar, side="LONG", stop_loss=92.0, take_profit=104.0,
            path_model=model,
        )
        assert hit_label == "sl"
        assert hit_price == 92.0

    def test_constructor_from_dataframe(self):
        """Build M1PathModel from a pandas DataFrame."""
        import pandas as pd
        df = pd.DataFrame({
            "timestamp": [1000, 61000, 121000, 181000, 241000],
            "open":      [100.0, 102.0, 101.0, 99.0, 98.0],
            "high":      [105.0, 104.0, 102.0, 100.0, 99.0],
            "low":       [98.0,  100.0, 97.0,  96.0, 94.0],
            "close":     [102.0, 101.0, 99.0,  98.0, 97.0],
        })
        model = M1PathModel(m1_df=df, sub_bar_count=5)
        bar = Bar(open=100.0, high=105.0, low=94.0, close=97.0, ts_ms=1000)
        # Level 103 appears in sub-bar 0 (98-105) → fraction 0.1
        assert model.fraction_into_bar(bar, 103.0) == pytest.approx(0.1)

    def test_constructor_requires_lookup_or_df(self):
        with pytest.raises(ValueError, match="needs either m1_lookup or m1_df"):
            M1PathModel()

    def test_custom_sub_bar_count_for_h1_backtest(self):
        """H1 backtest: 60 M1 sub-bars per H1 bar."""
        # Sub-bar 30 (= 50% into bar) touches level 100
        lookup = {1000 + i * 60_000: (99.0, 101.0) for i in range(60) if i == 30}
        model = M1PathModel(m1_lookup=lookup, sub_bar_count=60)
        bar = Bar(open=100.0, high=105.0, low=90.0, close=100.5, ts_ms=1000)
        # Only sub-bar 30 is present, but sub_bars_for iterates 0..59
        # and only sub-bar 30 exists in the lookup
        sub_bars = model._sub_bars_for(1000)
        assert len(sub_bars) == 1  # only the one we added


class TestM1PathModelFactory:
    def test_missing_parquet_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="M1 parquet not found"):
            get_or_build_m1_path_model(str(tmp_path / "missing.parquet"))

    def test_factory_returns_m1_model_from_real_parquet(self, tmp_path):
        import pandas as pd
        path = tmp_path / "small_m1.parquet"
        df = pd.DataFrame({
            "timestamp": [1000, 61000, 121000, 181000, 241000],
            "open":  [100.0, 101.0, 102.0, 103.0, 104.0],
            "high":  [105.0, 103.0, 104.0, 105.0, 106.0],
            "low":   [99.0,  100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0],
        })
        df.to_parquet(path)
        model = get_or_build_m1_path_model(str(path))
        assert isinstance(model, M1PathModel)
        # Cached identity on second call
        model2 = get_or_build_m1_path_model(str(path))
        assert model is model2

    def test_factory_rejects_malformed_parquet(self, tmp_path):
        import pandas as pd
        path = tmp_path / "bad_m1.parquet"
        pd.DataFrame({"wrong_col": [1, 2, 3]}).to_parquet(path)
        with pytest.raises(ValueError, match="missing required columns"):
            get_or_build_m1_path_model(str(path))
