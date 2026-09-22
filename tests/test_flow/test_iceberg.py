"""Tests for iceberg detection — volume traded at a price vs size ever displayed.

The whole signal rests on one discriminator, and these tests pin it:

  · a real 200-lot order eaten 200 -> 150 -> ... -> 0 has max-displayed 200 and
    traded 200, ratio 1.0, and must NOT be called an iceberg
  · an order that only ever shows 5 while 200 trades through it has ratio 40
    and must be

Using MAX displayed rather than first/last/average is what makes that work. Any
change to that choice should break these tests loudly.
"""

from __future__ import annotations

import pytest

from src.flow.evidence import score_zone
from src.flow.features import ICEBERG_MIN_PRINTS, MAX_PRICE_BUCKETS, ZoneAccumulator
from src.flow.level_registry import Level, LevelSide
from src.flow.zone_state import FlowEngine
from src.utils.types import OrderBookLevel, OrderBookSnapshot, Tick

TS = 1_757_000_000_000
TICK = 0.5


def _support() -> Level:
    return Level("s", "BTCUSDT", 98000.0, 100.0, LevelSide.LONG, note="pdl")


def _resistance() -> Level:
    return Level("r", "BTCUSDT", 98000.0, 100.0, LevelSide.SHORT, note="vah")


def _acc(is_support: bool = True, tick_size: float = TICK, **kw) -> ZoneAccumulator:
    base = dict(level_price=98000.0, is_support=is_support, baseline_range=100.0,
                baseline_trade_size=1.0, zone_width=100.0, tick_size=tick_size)
    base.update(kw)
    return ZoneAccumulator(**base)


def _book(at_price: float, size: float, ts: int = TS, is_bid: bool = True) -> OrderBookSnapshot:
    focus = [OrderBookLevel(price=at_price, quantity=size)]
    filler = [OrderBookLevel(price=at_price + (i + 1) * TICK * (-1 if is_bid else 1),
                             quantity=1.0) for i in range(4)]
    side = focus + filler
    other = [OrderBookLevel(price=at_price + (i + 1) * TICK * (1 if is_bid else -1),
                            quantity=1.0) for i in range(5)]
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=side if is_bid else other,
        asks=other if is_bid else side,
        timestamp=ts,
    )


def _run(displayed_seq, trade_qty, n, is_support=True, **kw) -> ZoneAccumulator:
    acc = _acc(is_support=is_support, **kw)
    ts = TS
    for i in range(n):
        acc.on_book(_book(98000.0, displayed_seq[i % len(displayed_seq)], ts,
                          is_bid=is_support), band=100.0)
        acc.on_tick(Tick("BTCUSDT", 98000.0, trade_qty, ts, is_support))
        ts += 100
    return acc


class TestDiscriminator:
    """The two cases the whole signal turns on."""

    def test_real_order_eaten_down_is_not_an_iceberg(self):
        f = _run([200.0, 150.0, 100.0, 50.0, 10.0], 2.0, 100).features()
        assert f.iceberg_displayed == pytest.approx(200.0)
        assert f.iceberg_ratio == pytest.approx(1.0, rel=0.05)

    def test_refilling_order_is_an_iceberg(self):
        f = _run([5.0, 2.0, 5.0, 1.0, 5.0], 2.0, 100).features()
        assert f.iceberg_displayed == pytest.approx(5.0)
        assert f.iceberg_ratio > 30.0

    def test_max_displayed_used_not_last(self):
        """A big display early must not be forgotten by a small one later."""
        f = _run([100.0, 1.0, 1.0, 1.0], 2.0, 100).features()
        assert f.iceberg_displayed == pytest.approx(100.0)

    def test_max_displayed_used_not_first(self):
        f = _run([1.0, 1.0, 100.0, 1.0], 2.0, 100).features()
        assert f.iceberg_displayed == pytest.approx(100.0)


class TestRequiresBothHalves:
    def test_no_book_means_no_ratio(self):
        acc = _acc()
        for i in range(100):
            acc.on_tick(Tick("BTCUSDT", 98000.0, 2.0, TS + i * 100, True))
        assert acc.features().iceberg_ratio == 0.0

    def test_no_tick_size_means_no_ratio(self):
        """Without a tick there is nothing to bucket prices by."""
        f = _run([5.0], 2.0, 100, tick_size=0.0).features()
        assert f.iceberg_ratio == 0.0

    def test_book_without_trades_means_no_ratio(self):
        acc = _acc()
        for i in range(50):
            acc.on_book(_book(98000.0, 5.0, TS + i * 100), band=100.0)
        assert acc.features().iceberg_ratio == 0.0


class TestVolumeGate:
    def test_trivial_volume_does_not_qualify(self):
        """1 unit against a 0.001 display is a ratio of 1000 and means nothing."""
        acc = _acc(baseline_trade_size=10.0)      # gate = 10 x ICEBERG_MIN_PRINTS
        for i in range(5):
            acc.on_book(_book(98000.0, 0.001, TS + i * 100), band=100.0)
            acc.on_tick(Tick("BTCUSDT", 98000.0, 1.0, TS + i * 100, True))
        assert acc.features().iceberg_ratio == 0.0

    def test_real_volume_qualifies(self):
        acc = _acc(baseline_trade_size=1.0)
        needed = ICEBERG_MIN_PRINTS * 2
        for i in range(int(needed)):
            acc.on_book(_book(98000.0, 1.0, TS + i * 100), band=100.0)
            acc.on_tick(Tick("BTCUSDT", 98000.0, 2.0, TS + i * 100, True))
        assert acc.features().iceberg_ratio > 0.0

    def test_no_baseline_disables_the_gate(self):
        f = _run([5.0], 2.0, 50, baseline_trade_size=0.0).features()
        assert f.iceberg_ratio > 0.0


class TestSides:
    def test_support_tracks_bids(self):
        assert _run([5.0], 2.0, 100, is_support=True).features().iceberg_ratio > 0

    def test_resistance_tracks_asks(self):
        assert _run([5.0], 2.0, 100, is_support=False).features().iceberg_ratio > 0


class TestBounds:
    def test_out_of_band_levels_ignored(self):
        acc = _acc()
        for i in range(50):
            acc.on_book(_book(98000.0, 5.0, TS + i * 100), band=0.1)
            acc.on_tick(Tick("BTCUSDT", 98000.0, 2.0, TS + i * 100, True))
        # Only the level exactly at the price falls inside a 0.1 band.
        assert acc.features().iceberg_displayed == pytest.approx(5.0)

    def test_bucket_dict_is_capped(self):
        acc = _acc()
        for i in range(MAX_PRICE_BUCKETS + 500):
            acc.on_tick(Tick("BTCUSDT", 98000.0 + i * TICK, 1.0, TS + i, True))
        assert len(acc._traded_at) <= MAX_PRICE_BUCKETS

    def test_reported_price_matches_bucket(self):
        f = _run([5.0], 2.0, 100).features()
        assert f.iceberg_price == pytest.approx(98000.0, abs=TICK)


class TestEvidenceRule:
    def test_iceberg_counts_for(self):
        f = _run([5.0, 2.0, 5.0], 2.0, 100).features()
        report = score_zone(_support(), f)
        assert "iceberg" in [i.name for i in report.supporting]

    def test_real_order_does_not_fire_the_rule(self):
        f = _run([200.0, 150.0, 100.0], 2.0, 100).features()
        report = score_zone(_support(), f)
        assert "iceberg" not in [i.name for i in report.supporting]

    def test_threshold_respected(self):
        f = _run([5.0, 2.0, 5.0], 2.0, 100).features()
        loose = score_zone(_support(), f, iceberg_threshold=3.0)
        strict = score_zone(_support(), f, iceberg_threshold=1e9)
        assert "iceberg" in [i.name for i in loose.supporting]
        assert "iceberg" not in [i.name for i in strict.supporting]

    def test_detail_reports_both_numbers(self):
        f = _run([5.0, 2.0, 5.0], 2.0, 100).features()
        item = next(i for i in score_zone(_support(), f).supporting if i.name == "iceberg")
        assert "displayed" in item.detail
        assert "hidden" in item.detail

    def test_resistance_iceberg_counts_for(self):
        f = _run([5.0, 2.0, 5.0], 2.0, 100, is_support=False).features()
        assert "iceberg" in [i.name for i in score_zone(_resistance(), f).supporting]


class TestEngineTickInference:
    def _engine(self) -> FlowEngine:
        from src.flow.level_registry import LevelRegistry
        reg = LevelRegistry()
        reg.levels = {"s": _support()}
        eng = FlowEngine(registry=reg, symbol="BTCUSDT")
        eng.sync_levels(now_ms=TS)
        return eng

    def test_tick_inferred_from_book_spacing(self):
        eng = self._engine()
        eng.on_book(_book(98000.0, 5.0))
        assert eng._tick_size == pytest.approx(TICK)

    def test_tick_latched_not_reinferred(self):
        """One odd book must not shrink an already-known tick."""
        eng = self._engine()
        eng.on_book(_book(98000.0, 5.0))
        weird = OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=[OrderBookLevel(price=98000.0, quantity=1.0),
                  OrderBookLevel(price=97999.999, quantity=1.0)],
            asks=[], timestamp=TS,
        )
        eng.on_book(weird)
        assert eng._tick_size == pytest.approx(TICK)

    def test_no_tick_before_any_book(self):
        assert self._engine()._tick_size == 0.0

    def test_tick_reaches_the_accumulator(self):
        eng = self._engine()
        eng.on_book(_book(98000.0, 5.0))
        eng.on_tick(Tick("BTCUSDT", 97950.0, 1.0, TS, True), local_ms=TS)
        assert eng.monitors["s"]._acc.tick_size == pytest.approx(TICK)

    def test_tick_in_engine_snapshot(self):
        eng = self._engine()
        eng.on_book(_book(98000.0, 5.0))
        assert eng.snapshot(local_ms=TS)["tick_size"] == pytest.approx(TICK)

    def test_float_noise_is_snapped_away(self):
        """A real BTCUSDT book gives 0.00999999999476131 for a 0.01 tick —
        differencing two binary floats does not produce a round number. The
        bucketing error is tiny, but a tick that prints like that reads as
        broken and fails any equality check against the real tick."""
        eng = self._engine()
        prices = [98000.0 + i * 0.01 for i in range(6)]
        book = OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=[OrderBookLevel(price=p, quantity=1.0) for p in reversed(prices)],
            asks=[], timestamp=TS,
        )
        raw = abs(prices[1] - prices[0])
        assert raw != 0.01, "expected float noise in the fixture itself"
        eng.on_book(book)
        assert eng._tick_size == 0.01

    def test_snapping_keeps_a_genuinely_small_tick(self):
        """8 significant digits, not 8 decimal places — a 1e-8 tick survives."""
        eng = self._engine()
        book = OrderBookSnapshot(
            symbol="SHIBUSDT",
            bids=[OrderBookLevel(price=1.0 + i * 1e-8, quantity=1.0) for i in range(3)],
            asks=[], timestamp=TS,
        )
        eng.on_book(book)
        assert eng._tick_size == pytest.approx(1e-8, rel=1e-3)
