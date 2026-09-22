"""Tests for src/flow/features.py and src/flow/evidence.py.

The load-bearing test is `TestAbsorptionSign`. At a SUPPORT level the bullish
evidence is aggressive SELLING that fails to move price — not buying. Invert
that and the system buys support only after buyers have already lifted the ask,
i.e. at the worst available price, while calling it "confirmation".
"""

from __future__ import annotations

import pytest

from src.flow.evidence import DEFAULT_THRESHOLD, score_zone
from src.flow.features import MIN_TRADES_FOR_EVIDENCE, ZoneAccumulator
from src.flow.level_registry import Level, LevelSide
from src.utils.types import OrderBookLevel, OrderBookSnapshot, Tick

TS = 1_757_000_000_000


def _tick(price: float, qty: float, maker: bool, ts: int) -> Tick:
    return Tick(symbol="BTCUSDT", price=price, quantity=qty,
                timestamp=ts, is_buyer_maker=maker)


def _support(width: float = 100.0) -> Level:
    return Level("sup", "BTCUSDT", 98000.0, width, LevelSide.LONG, note="pdl")


def _resistance(width: float = 100.0) -> Level:
    return Level("res", "BTCUSDT", 98000.0, width, LevelSide.SHORT, note="vah")


def _fill(acc: ZoneAccumulator, n: int, price_fn, maker: bool, qty: float = 1.0,
          step_ms: int = 100) -> ZoneAccumulator:
    for i in range(n):
        acc.on_tick(_tick(price_fn(i), qty, maker, TS + i * step_ms))
    return acc


class TestAccumulatorBasics:
    def test_delta_signs_follow_aggressor(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        acc.on_tick(_tick(98000.0, 5.0, True, TS))    # aggressive sell
        acc.on_tick(_tick(98000.0, 2.0, False, TS))   # aggressive buy
        f = acc.features()
        assert f.sell_volume == 5.0
        assert f.buy_volume == 2.0
        assert f.delta == pytest.approx(-3.0)

    def test_delta_ratio_bounds(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 10, lambda i: 98000.0, True)
        assert acc.features().delta_ratio == pytest.approx(-1.0)

    def test_dwell_measured_from_first_tick(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        acc.on_tick(_tick(98000.0, 1.0, True, TS))
        acc.on_tick(_tick(98000.0, 1.0, True, TS + 45_000))
        assert acc.features().dwell_ms == 45_000

    def test_high_low_tracked(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        for px in (98000.0, 98040.0, 97960.0, 98010.0):
            acc.on_tick(_tick(px, 1.0, True, TS))
        f = acc.features()
        assert (f.high, f.low) == (98040.0, 97960.0)
        assert f.excursion == pytest.approx(80.0)

    def test_empty_accumulator_is_safe(self):
        f = ZoneAccumulator(98000.0, True, 100.0).features()
        assert f.trade_count == 0
        assert f.delta_ratio == 0.0
        assert f.sufficient is False

    def test_sufficiency_threshold(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0),
                    MIN_TRADES_FOR_EVIDENCE - 1, lambda i: 98000.0, True)
        assert acc.features().sufficient is False
        acc.on_tick(_tick(98000.0, 1.0, True, TS))
        assert acc.features().sufficient is True


class TestAbsorptionMath:
    def test_one_sided_flow_with_no_movement_scores_high(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 50,
                    lambda i: 98000.0 + (i % 3), True)
        f = acc.features()
        assert f.absorption > 0.9

    def test_movement_beyond_baseline_scores_zero(self):
        """Aggression that moved price a full normal range is not absorption."""
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 50,
                    lambda i: 98000.0 + i * 3, True)   # excursion 147 vs baseline 100
        f = acc.features()
        assert f.range_ratio > 1.0
        assert f.absorption == pytest.approx(0.0)

    def test_balanced_flow_scores_zero(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        for i in range(50):
            acc.on_tick(_tick(98000.0, 1.0, i % 2 == 0, TS + i * 100))
        assert acc.features().absorption == pytest.approx(0.0, abs=1e-9)

    def test_unknown_baseline_disables_absorption(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 0.0), 50, lambda i: 98000.0, True)
        f = acc.features()
        assert f.range_ratio == 1.0
        assert f.absorption == pytest.approx(0.0)


class TestAdverseExcursion:
    def test_support_measures_downside(self):
        acc = ZoneAccumulator(98000.0, is_support=True, baseline_range=100.0)
        acc.on_tick(_tick(97950.0, 1.0, True, TS))
        assert acc.features().adverse_excursion == pytest.approx(50.0)

    def test_resistance_measures_upside(self):
        acc = ZoneAccumulator(98000.0, is_support=False, baseline_range=100.0)
        acc.on_tick(_tick(98070.0, 1.0, False, TS))
        assert acc.features().adverse_excursion == pytest.approx(70.0)

    def test_never_negative(self):
        acc = ZoneAccumulator(98000.0, is_support=True, baseline_range=100.0)
        acc.on_tick(_tick(98090.0, 1.0, True, TS))
        assert acc.features().adverse_excursion == 0.0


class TestLateDelta:
    def test_reflects_recent_window_only(self):
        acc = ZoneAccumulator(98000.0, True, 100.0, late_window_ms=10_000)
        for i in range(40):                       # old: aggressive selling
            acc.on_tick(_tick(98000.0, 1.0, True, TS + i * 100))
        for i in range(40):                       # recent: aggressive buying
            acc.on_tick(_tick(98000.0, 1.0, False, TS + 60_000 + i * 100))
        f = acc.features()
        assert f.delta_ratio == pytest.approx(0.0, abs=1e-9)   # net flat overall
        assert f.late_delta_ratio == pytest.approx(1.0)        # but turning up

    def test_zero_when_no_recent_trades(self):
        assert ZoneAccumulator(98000.0, True, 100.0).features().late_delta_ratio == 0.0


class TestBookFeatures:
    def _book(self, bid_size: float, ask_size: float = 10.0) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=[OrderBookLevel(price=98000.0 - i, quantity=bid_size) for i in range(5)],
            asks=[OrderBookLevel(price=98001.0 + i, quantity=ask_size) for i in range(5)],
            timestamp=TS,
        )

    def test_no_book_flag(self):
        assert ZoneAccumulator(98000.0, True, 100.0).features().has_book is False

    def test_book_marks_present(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        acc.on_book(self._book(10.0), band=100.0)
        assert acc.features().has_book is True

    def test_support_aggregates_bids(self):
        acc = ZoneAccumulator(98000.0, is_support=True, baseline_range=100.0)
        acc.on_book(self._book(10.0), band=100.0)
        assert acc.features().book_size_at_level == pytest.approx(50.0)

    def test_resistance_aggregates_asks(self):
        acc = ZoneAccumulator(98000.0, is_support=False, baseline_range=100.0)
        acc.on_book(self._book(10.0, ask_size=7.0), band=100.0)
        assert acc.features().book_size_at_level == pytest.approx(35.0)

    def test_refill_detected_after_depletion(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        acc.on_book(self._book(20.0), band=100.0)   # big
        acc.on_book(self._book(2.0), band=100.0)    # eaten down
        acc.on_book(self._book(20.0), band=100.0)   # replenished
        assert acc.features().book_refills >= 1

    def test_no_refill_when_size_steady(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        for _ in range(5):
            acc.on_book(self._book(20.0), band=100.0)
        assert acc.features().book_refills == 0

    def test_imbalance_sign(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        acc.on_book(self._book(30.0, ask_size=10.0), band=100.0)
        assert acc.features().book_imbalance > 0

    def test_none_snapshot_ignored(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        acc.on_book(None, band=100.0)
        assert acc.features().has_book is False


class TestAbsorptionSign:
    """THE load-bearing tests. Inverting these inverts the whole system."""

    def test_support_confirmed_by_absorbed_selling(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 60,
                    lambda i: 98000.0 + (i % 3), maker=True)   # aggressive SELLS
        report = score_zone(_support(), acc.features())
        names = [i.name for i in report.supporting]
        assert "absorption" in names
        assert report.score > DEFAULT_THRESHOLD

    def test_support_not_confirmed_by_buying_alone(self):
        """Buyers lifting the ask at support is chasing, not absorption."""
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 60,
                    lambda i: 98000.0 + (i % 3), maker=False)  # aggressive BUYS
        report = score_zone(_support(), acc.features())
        assert "absorption" not in [i.name for i in report.supporting]
        assert "absorption_wrong_side" in [i.name for i in report.opposing]

    def test_resistance_confirmed_by_absorbed_buying(self):
        acc = _fill(ZoneAccumulator(98000.0, is_support=False, baseline_range=100.0),
                    60, lambda i: 98000.0 - (i % 3), maker=False)  # aggressive BUYS
        report = score_zone(_resistance(), acc.features())
        assert "absorption" in [i.name for i in report.supporting]
        assert report.score > DEFAULT_THRESHOLD

    def test_resistance_not_confirmed_by_selling_alone(self):
        acc = _fill(ZoneAccumulator(98000.0, is_support=False, baseline_range=100.0),
                    60, lambda i: 98000.0 - (i % 3), maker=True)
        report = score_zone(_resistance(), acc.features())
        assert "absorption" not in [i.name for i in report.supporting]


class TestEvidenceRules:
    def test_breaking_counts_against(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 60,
                    lambda i: 98000.0 - i * 3, maker=True)   # selling AND falling
        report = score_zone(_support(), acc.features())
        assert "breaking" in [i.name for i in report.opposing]

    def test_adverse_excursion_counts_against(self):
        acc = ZoneAccumulator(98000.0, True, 100.0)
        for i in range(40):
            acc.on_tick(_tick(97900.0, 1.0, True, TS + i * 100))
        report = score_zone(_support(width=100.0), acc.features())
        assert "adverse_excursion" in [i.name for i in report.opposing]

    def test_small_adverse_counts_for(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 40, lambda i: 98010.0, True)
        report = score_zone(_support(), acc.features())
        assert "level_holding" in [i.name for i in report.supporting]

    def test_repeated_test_counts_against(self):
        acc = ZoneAccumulator(98000.0, True, 100.0, test_count=3)
        _fill(acc, 40, lambda i: 98000.0, True)
        report = score_zone(_support(), acc.features())
        assert "repeated_test" in [i.name for i in report.opposing]

    def test_late_flow_turn_counts_for(self):
        acc = ZoneAccumulator(98000.0, True, 100.0, late_window_ms=10_000)
        for i in range(40):
            acc.on_tick(_tick(98000.0, 1.0, True, TS + i * 100))
        for i in range(40):
            acc.on_tick(_tick(98000.0, 1.0, False, TS + 60_000 + i * 100))
        report = score_zone(_support(), acc.features())
        assert "late_flow_turn" in [i.name for i in report.supporting]


class TestScoring:
    def test_score_bounded(self):
        for maker in (True, False):
            acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 80,
                        lambda i: 98000.0 - i * 5, maker)
            s = score_zone(_support(), acc.features()).score
            assert 0.0 <= s <= 1.0

    def test_thin_zone_does_not_score_confidently(self):
        """Three prints must not produce a confident-looking score."""
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 3, lambda i: 98000.0, True)
        f = acc.features()
        assert f.absorption < 0.2          # confidence ramp, not a perfect 1.0
        report = score_zone(_support(), f)
        assert report.score < DEFAULT_THRESHOLD
        assert report.sufficient is False

    def test_insufficient_activity_blocks_confirmation(self):
        """A high score on three trades must never confirm."""
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 3, lambda i: 98000.0, True)
        report = score_zone(_support(), acc.features())
        assert report.sufficient is False
        assert report.confirmed is False

    def test_report_always_carries_both_sides(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 60,
                    lambda i: 98000.0 - i * 3, True)
        report = score_zone(_support(), acc.features())
        assert isinstance(report.supporting, list)
        assert isinstance(report.opposing, list)

    def test_tape_outweighs_book(self):
        """Book evidence must not be able to overturn a tape read."""
        from src.flow.evidence import (
            W_ABSORPTION,
            W_BOOK_IMBALANCE,
            W_BOOK_REFILL,
            W_BOOK_SIZE,
            W_BREAK,
        )
        book_total = W_BOOK_SIZE + W_BOOK_REFILL + W_BOOK_IMBALANCE
        assert W_ABSORPTION > book_total
        assert W_BREAK > book_total

    def test_summary_readable(self):
        acc = _fill(ZoneAccumulator(98000.0, True, 100.0), 60, lambda i: 98000.0, True)
        assert "score" in score_zone(_support(), acc.features()).summary()
