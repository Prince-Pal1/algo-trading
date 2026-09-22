"""Tests for src/flow/tape.py — the rolling picture the chart is drawn from.

Two things carry real risk here:
  · the aggressor mapping. `is_buyer_maker` means the SELLER crossed the spread.
    Getting it backwards inverts CVD, the candle colours and the footprint at
    once, and every one of them would still look plausible.
  · the footprint row height. Every column's volume is keyed by
    `round(price / bucket_size)`, so changing it silently re-points the history
    at different prices.
"""

from __future__ import annotations

import pytest

from src.flow.tape import DEFAULT_INTERVAL_MS, MAX_BUCKETS_PER_COLUMN, FlowTape
from src.utils.types import OrderBookLevel, OrderBookSnapshot, Tick

TS = 1_757_000_000_000


def _tape(**kw) -> FlowTape:
    kw.setdefault("bucket_size", 1.0)
    return FlowTape(**kw)


def _tick(price: float, qty: float, maker: bool, ts: int) -> Tick:
    return Tick("BTCUSDT", price, qty, ts, maker)


def _book(mid: float, size: float, ts: int) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=[OrderBookLevel(price=mid - i, quantity=size) for i in range(3)],
        asks=[OrderBookLevel(price=mid + 1 + i, quantity=size) for i in range(3)],
        timestamp=ts,
    )


class TestAggressor:
    def test_buyer_maker_is_an_aggressive_sell(self):
        """The same rule as src/data/cvd.py. Backwards, everything inverts."""
        t = _tape()
        t.on_tick(_tick(100.0, 5.0, True, TS))
        col = t.columns[-1]
        assert (col.sell, col.buy) == (5.0, 0.0)
        assert t.cvd == -5.0

    def test_buyer_taker_is_an_aggressive_buy(self):
        t = _tape()
        t.on_tick(_tick(100.0, 5.0, False, TS))
        assert (t.columns[-1].buy, t.columns[-1].sell) == (5.0, 0.0)
        assert t.cvd == 5.0

    def test_cvd_is_cumulative_across_columns(self):
        t = _tape(interval_ms=1000)
        t.on_tick(_tick(100.0, 5.0, False, TS))
        t.on_tick(_tick(100.0, 2.0, True, TS + 1500))
        assert len(t.columns) == 2
        assert t.columns[0].cvd == 5.0
        assert t.columns[1].cvd == 3.0


class TestColumns:
    def test_candle_ohlc(self):
        t = _tape(interval_ms=10_000)
        for p in (100.0, 104.0, 98.0, 101.0):
            t.on_tick(_tick(p, 1.0, False, TS))
        c = t.columns[-1]
        assert (c.open, c.high, c.low, c.close) == (100.0, 104.0, 98.0, 101.0)

    def test_a_new_interval_opens_a_column(self):
        t = _tape(interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        t.on_tick(_tick(101.0, 1.0, False, TS + 1000))
        assert len(t.columns) == 2
        assert t.columns[1].open == 101.0

    def test_columns_are_bounded(self):
        t = _tape(interval_ms=1000, max_columns=5)
        for i in range(30):
            t.on_tick(_tick(100.0, 1.0, False, TS + i * 1000))
        assert len(t.columns) == 5

    def test_out_of_order_ticks_are_dropped(self):
        """A reconnect can replay; the past is closed and must not be rewritten."""
        t = _tape(interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS + 5000))
        t.on_tick(_tick(50.0, 99.0, False, TS))          # from the past
        assert len(t.columns) == 1
        assert t.columns[0].low == 100.0

    def test_seq_advances_only_when_a_column_closes(self):
        t = _tape(interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        assert t.seq == 0
        t.on_tick(_tick(100.0, 1.0, False, TS + 200))     # same column
        assert t.seq == 0
        t.on_tick(_tick(100.0, 1.0, False, TS + 1200))    # closes the first
        assert t.seq == 1


class TestFootprint:
    def test_volume_lands_in_the_right_row(self):
        t = _tape(bucket_size=10.0)
        t.on_tick(_tick(104.0, 3.0, False, TS))    # -> bucket 10
        t.on_tick(_tick(96.0, 2.0, True, TS))      # -> bucket 10 as well
        col = t.columns[-1]
        assert col.buy_at == {10: 3.0}
        assert col.sell_at == {10: 2.0}

    def test_rows_split_by_aggressor(self):
        t = _tape(bucket_size=1.0)
        t.on_tick(_tick(100.0, 3.0, False, TS))
        t.on_tick(_tick(100.0, 4.0, True, TS))
        col = t.columns[-1]
        assert col.buy_at[100] == 3.0 and col.sell_at[100] == 4.0

    def test_no_bucket_size_means_no_footprint(self):
        t = FlowTape(bucket_size=0.0)
        t.on_tick(_tick(100.0, 3.0, False, TS))
        assert t.columns[-1].buy_at == {}
        assert t.columns[-1].buy == 3.0          # the candle still works

    def test_bucket_dict_is_capped(self):
        t = _tape(bucket_size=0.01, interval_ms=10_000_000)
        for i in range(MAX_BUCKETS_PER_COLUMN + 300):
            t.on_tick(_tick(100.0 + i * 0.01, 1.0, False, TS))
        assert len(t.columns[-1].buy_at) <= MAX_BUCKETS_PER_COLUMN


class TestBucketSizeChange:
    def test_changing_the_row_height_clears_the_history(self):
        """Old indices mean a different price under a new divisor — mixing two
        scales in one picture is worse than showing less history."""
        t = _tape(bucket_size=1.0, interval_ms=1000)
        for i in range(5):
            t.on_tick(_tick(100.0, 1.0, False, TS + i * 1000))
        assert len(t.columns) == 5
        assert t.set_bucket_size(10.0) is True
        assert len(t.columns) == 0
        assert t.bucket_size == 10.0

    def test_setting_the_same_size_is_a_no_op(self):
        t = _tape(bucket_size=2.0, interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        assert t.set_bucket_size(2.0) is False
        assert len(t.columns) == 1

    def test_a_zero_size_is_refused(self):
        t = _tape(bucket_size=2.0)
        assert t.set_bucket_size(0.0) is False
        assert t.bucket_size == 2.0


class TestBook:
    def test_book_is_sampled_when_the_column_closes(self):
        t = _tape(bucket_size=1.0, interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        t.on_book(_book(100.0, 7.0, TS))
        assert t.columns[-1].book_at == {}        # not yet — the column is open
        t.on_tick(_tick(100.0, 1.0, False, TS + 1500))
        assert t.columns[0].book_at              # sampled as it closed

    def test_no_book_leaves_it_empty(self):
        t = _tape(bucket_size=1.0, interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        t.on_tick(_tick(100.0, 1.0, False, TS + 1500))
        assert t.columns[0].book_at == {}


class TestPublish:
    def test_live_column_is_the_forming_one(self):
        t = _tape(interval_ms=1000)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        t.on_tick(_tick(101.0, 1.0, False, TS + 1500))
        assert t.live_column()["o"] == 101.0

    def test_live_column_is_none_before_any_tick(self):
        assert _tape().live_column() is None

    def test_footprint_serializes_as_flat_pairs(self):
        t = _tape(bucket_size=1.0)
        t.on_tick(_tick(100.0, 3.0, False, TS))
        d = t.columns[-1].to_dict()
        assert d["ba"] == [100, 3.0]
        assert len(d["ba"]) % 2 == 0

    def test_to_dict_carries_what_the_client_needs(self):
        t = _tape(interval_ms=2000, bucket_size=4.0)
        t.on_tick(_tick(100.0, 1.0, False, TS))
        d = t.to_dict()
        assert d["interval_ms"] == 2000 and d["bucket_size"] == 4.0
        assert d["has_book"] is False
        assert len(d["columns"]) == 1
