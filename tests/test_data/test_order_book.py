"""Tests for src/data/order_book.py — the depth-stream sequencing contract.

A book built from diff updates does not fail loudly when sequencing is wrong.
It drifts, keeps answering queries, and every heatmap drawn from it is quietly
wrong. So the load-bearing tests here are the gap-detection ones: a sequence
gap MUST flip the book to DESYNCED and MUST stop it serving snapshots.
"""

from __future__ import annotations

import pytest

from src.data.order_book import ApplyResult, BookState, OrderBookState


def _diff(U: int, u: int, bids=(), asks=(), pu=None, E: int = 0) -> dict:
    event = {"U": U, "u": u, "b": list(bids), "a": list(asks)}
    if pu is not None:
        event["pu"] = pu
    if E:
        event["E"] = E
    return event


def _synced(symbol: str = "BTCUSDT", last_id: int = 100) -> OrderBookState:
    book = OrderBookState(symbol)
    book.apply_snapshot(last_id, [["99", "2"], ["98", "3"]], [["101", "1"], ["102", "4"]])
    return book


class TestInitialState:
    def test_starts_unsynced(self):
        book = OrderBookState("BTCUSDT")
        assert book.state is BookState.UNSYNCED
        assert book.ready is False

    def test_unsynced_book_serves_no_snapshot(self):
        assert OrderBookState("BTCUSDT").snapshot() is None

    def test_empty_book_has_no_best_prices(self):
        book = OrderBookState("BTCUSDT")
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.mid is None
        assert book.spread is None


class TestBuffering:
    def test_diffs_buffered_before_snapshot(self):
        book = OrderBookState("BTCUSDT")
        assert book.apply_diff(_diff(1, 5)) is ApplyResult.BUFFERED
        assert book.buffered == 1

    def test_buffer_is_capped(self):
        book = OrderBookState("BTCUSDT", max_buffer=3)
        for i in range(10):
            book.apply_diff(_diff(i, i))
        assert book.buffered == 3
        assert book.dropped_buffer == 7

    def test_buffer_keeps_newest_events(self):
        book = OrderBookState("BTCUSDT", max_buffer=2)
        for i in range(1, 6):
            book.apply_diff(_diff(i, i))
        book.apply_snapshot(3, [], [])
        # Events 4 and 5 survived the cap and are newer than the snapshot.
        assert book.last_update_id == 5

    def test_stale_buffered_events_dropped_on_snapshot(self):
        book = OrderBookState("BTCUSDT")
        book.apply_diff(_diff(1, 5, bids=[["50", "9"]]))
        applied = book.apply_snapshot(10, [["99", "1"]], [["101", "1"]])
        assert applied == 0
        assert book.best_bid == 99.0  # the stale level never landed

    def test_straddling_buffered_event_is_applied(self):
        book = OrderBookState("BTCUSDT")
        book.apply_diff(_diff(8, 12, bids=[["99.5", "7"]]))
        applied = book.apply_snapshot(10, [["99", "1"]], [["101", "1"]])
        assert applied == 1
        assert book.best_bid == 99.5


class TestSnapshotBootstrap:
    def test_snapshot_sets_synced(self):
        book = _synced()
        assert book.state is BookState.SYNCED
        assert book.ready is True

    def test_snapshot_populates_levels(self):
        book = _synced()
        assert book.depth_counts == (2, 2)
        assert book.best_bid == 99.0
        assert book.best_ask == 101.0
        assert book.mid == 100.0
        assert book.spread == pytest.approx(2.0)

    def test_zero_quantity_levels_excluded_from_snapshot(self):
        book = OrderBookState("BTCUSDT")
        book.apply_snapshot(1, [["99", "2"], ["98", "0"]], [["101", "0"]])
        assert book.depth_counts == (1, 0)

    def test_string_and_float_inputs_both_accepted(self):
        book = OrderBookState("BTCUSDT")
        book.apply_snapshot(1, [[99.0, 2.0]], [["101", "1"]])
        assert book.best_bid == 99.0

    def test_last_update_id_recorded(self):
        assert _synced(last_id=4242).last_update_id == 4242


class TestSequencing:
    def test_contiguous_spot_event_applied(self):
        book = _synced(last_id=100)
        assert book.apply_diff(_diff(101, 105)) is ApplyResult.APPLIED
        assert book.last_update_id == 105

    def test_stale_event_dropped_without_advancing(self):
        book = _synced(last_id=100)
        assert book.apply_diff(_diff(90, 95)) is ApplyResult.STALE
        assert book.last_update_id == 100

    def test_gap_desyncs_the_book(self):
        book = _synced(last_id=100)
        assert book.apply_diff(_diff(150, 160)) is ApplyResult.DESYNC
        assert book.state is BookState.DESYNCED
        assert book.desync_count == 1

    def test_desynced_book_refuses_snapshots(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(150, 160))
        assert book.snapshot() is None
        assert book.ready is False

    def test_desynced_book_rejects_further_diffs(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(150, 160))
        assert book.apply_diff(_diff(161, 162)) is ApplyResult.DESYNC

    def test_overlapping_event_is_tolerated(self):
        """Overlaps are benign — events carry absolute sizes, not deltas."""
        book = _synced(last_id=100)
        assert book.apply_diff(_diff(95, 105)) is ApplyResult.APPLIED
        assert book.state is BookState.SYNCED

    def test_off_by_one_gap_is_caught(self):
        book = _synced(last_id=100)
        # 102 leaves update 101 unaccounted for.
        assert book.apply_diff(_diff(102, 110)) is ApplyResult.DESYNC


class TestFuturesSequencing:
    """USD-M futures carry `pu` and are checked against it, not against U."""

    def test_matching_pu_applied(self):
        book = _synced(last_id=100)
        assert book.apply_diff(_diff(101, 105, pu=100)) is ApplyResult.APPLIED

    def test_mismatched_pu_desyncs(self):
        book = _synced(last_id=100)
        assert book.apply_diff(_diff(101, 105, pu=99)) is ApplyResult.DESYNC
        assert book.state is BookState.DESYNCED

    def test_pu_takes_precedence_over_u_check(self):
        book = _synced(last_id=100)
        # U would pass the spot rule, but pu says an event was missed.
        assert book.apply_diff(_diff(95, 105, pu=94)) is ApplyResult.DESYNC

    def test_futures_chain_advances(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 105, pu=100))
        assert book.apply_diff(_diff(106, 110, pu=105)) is ApplyResult.APPLIED
        assert book.last_update_id == 110


class TestLevelUpdates:
    def test_zero_quantity_removes_level(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 102, bids=[["99", "0"]]))
        assert book.best_bid == 98.0
        assert book.depth_counts == (1, 2)

    def test_new_level_inserted(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 102, bids=[["99.5", "5"]]))
        assert book.best_bid == 99.5

    def test_existing_level_resized(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 102, bids=[["99", "42"]]))
        snap = book.snapshot()
        assert snap.bids[0].price == 99.0
        assert snap.bids[0].quantity == 42.0

    def test_removing_absent_level_is_a_noop(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 102, bids=[["12.34", "0"]]))
        assert book.depth_counts == (2, 2)

    def test_both_sides_updated_in_one_event(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 102, bids=[["99.5", "1"]], asks=[["100.5", "1"]]))
        assert book.best_bid == 99.5
        assert book.best_ask == 100.5

    def test_event_time_recorded(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(101, 102, E=1_700_000_000_000))
        assert book.snapshot().timestamp == 1_700_000_000_000


class TestSnapshotView:
    def test_bids_descend_and_asks_ascend(self):
        book = OrderBookState("BTCUSDT")
        book.apply_snapshot(
            1,
            [["97", "1"], ["99", "1"], ["98", "1"]],
            [["103", "1"], ["101", "1"], ["102", "1"]],
        )
        snap = book.snapshot()
        assert [lv.price for lv in snap.bids] == [99.0, 98.0, 97.0]
        assert [lv.price for lv in snap.asks] == [101.0, 102.0, 103.0]

    def test_depth_limit_keeps_levels_nearest_the_touch(self):
        book = OrderBookState("BTCUSDT")
        book.apply_snapshot(
            1,
            [["97", "1"], ["99", "1"], ["98", "1"]],
            [["103", "1"], ["101", "1"], ["102", "1"]],
        )
        snap = book.snapshot(depth=2)
        assert [lv.price for lv in snap.bids] == [99.0, 98.0]
        assert [lv.price for lv in snap.asks] == [101.0, 102.0]

    def test_explicit_timestamp_overrides_event_time(self):
        book = _synced()
        assert book.snapshot(timestamp_ms=555).timestamp == 555

    def test_symbol_carried_through(self):
        assert _synced("ETHUSDT").snapshot().symbol == "ETHUSDT"


class TestRecovery:
    def test_reset_returns_to_unsynced(self):
        book = _synced()
        book.reset()
        assert book.state is BookState.UNSYNCED
        assert book.depth_counts == (0, 0)
        assert book.last_update_id == 0

    def test_rebootstrap_after_desync_restores_service(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(150, 160))
        assert book.state is BookState.DESYNCED

        book.reset()
        book.apply_snapshot(200, [["99", "1"]], [["101", "1"]])
        assert book.ready is True
        assert book.apply_diff(_diff(201, 202)) is ApplyResult.APPLIED

    def test_resnapshot_without_reset_also_recovers(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(150, 160))
        book.apply_snapshot(300, [["99", "1"]], [["101", "1"]])
        assert book.ready is True
        assert book.last_update_id == 300

    def test_desync_count_accumulates(self):
        book = _synced(last_id=100)
        book.apply_diff(_diff(150, 160))
        book.apply_snapshot(200, [["99", "1"]], [["101", "1"]])
        book.apply_diff(_diff(500, 510))
        assert book.desync_count == 2
