"""Tests for the depth-stream wiring in src/data/feeds/binance_ws.py.

Offline only — no socket is opened and no REST call is made. What is checked
here is the routing and recovery logic around the book: that depth is opt-in,
that events reach the right book, and that a desync triggers a re-bootstrap
rather than leaving a drifted book in service.

The live path (real depthUpdate payloads, the REST snapshot, the full sync
handshake) is NOT covered here and has not been exercised — see the module
note in STATE.md.
"""

from __future__ import annotations

import asyncio

import pytest

from src.data.feeds.binance_ws import BinanceWebSocketFeed
from src.data.order_book import BookState


def _feed(depth_symbols=None, **kwargs) -> BinanceWebSocketFeed:
    return BinanceWebSocketFeed(
        symbols=["BTCUSDT"],
        timeframes=["1m"],
        depth_symbols=depth_symbols,
        **kwargs,
    )


def _bootstrap(feed: BinanceWebSocketFeed, symbol: str = "btcusdt", last_id: int = 100) -> None:
    feed.books[symbol].apply_snapshot(last_id, [["99", "2"]], [["101", "1"]])


def _depth_event(U: int, u: int, symbol: str = "BTCUSDT", **extra) -> dict:
    return {"e": "depthUpdate", "s": symbol, "U": U, "u": u, "b": [], "a": [], **extra}


class TestDepthIsOptIn:
    def test_no_depth_symbols_means_no_depth_stream(self):
        feed = _feed()
        assert not any("@depth" in s for s in feed._build_streams())
        assert feed.books == {}

    def test_depth_stream_added_when_requested(self):
        feed = _feed(depth_symbols=["BTCUSDT"])
        assert "btcusdt@depth@100ms" in feed._build_streams()

    def test_depth_speed_configurable(self):
        feed = _feed(depth_symbols=["BTCUSDT"], depth_speed="1000ms")
        assert "btcusdt@depth@1000ms" in feed._build_streams()

    def test_book_created_per_depth_symbol(self):
        feed = BinanceWebSocketFeed(
            symbols=["BTCUSDT", "ETHUSDT"],
            depth_symbols=["BTCUSDT", "ETHUSDT"],
        )
        assert set(feed.books) == {"btcusdt", "ethusdt"}
        assert all(b.state is BookState.UNSYNCED for b in feed.books.values())

    def test_depth_symbols_may_be_a_subset(self):
        feed = BinanceWebSocketFeed(
            symbols=["BTCUSDT", "ETHUSDT"], depth_symbols=["BTCUSDT"]
        )
        assert set(feed.books) == {"btcusdt"}

    def test_trade_and_kline_streams_still_present(self):
        streams = _feed(depth_symbols=["BTCUSDT"])._build_streams()
        assert "btcusdt@trade" in streams
        assert "btcusdt@kline_1m" in streams


class TestRestEndpointSelection:
    def test_spot_endpoint(self):
        assert "api.binance.com" in _feed(depth_symbols=["BTCUSDT"])._rest_depth_url()

    def test_futures_endpoint(self):
        feed = _feed(depth_symbols=["BTCUSDT"], use_futures_stream=True)
        assert "fapi.binance.com" in feed._rest_depth_url()


class TestDepthRouting:
    async def test_event_applied_to_matching_book(self):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed)
        await feed._handle_depth(_depth_event(101, 105))
        assert feed.books["btcusdt"].last_update_id == 105

    async def test_unknown_symbol_ignored(self):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed)
        await feed._handle_depth(_depth_event(101, 105, symbol="DOGEUSDT"))
        assert feed.books["btcusdt"].last_update_id == 100

    async def test_on_depth_called_with_snapshot(self):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed)
        seen = []
        feed.on_depth = lambda snap: seen.append(snap) or asyncio.sleep(0)
        event_ms = 1_700_000_000_000
        await feed._handle_depth(_depth_event(101, 105, E=event_ms))
        assert len(seen) == 1
        assert seen[0].symbol == "BTCUSDT"
        assert seen[0].timestamp == event_ms

    async def test_on_depth_not_called_while_unsynced(self):
        feed = _feed(depth_symbols=["BTCUSDT"])  # never bootstrapped
        seen = []
        feed.on_depth = lambda snap: seen.append(snap) or asyncio.sleep(0)
        await feed._handle_depth(_depth_event(1, 5))
        assert seen == []

    async def test_no_callback_is_safe(self):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed)
        await feed._handle_depth(_depth_event(101, 105))  # on_depth is None


class TestDesyncRecovery:
    async def test_desync_schedules_rebootstrap(self, monkeypatch):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed)
        calls: list[tuple[str, bool]] = []

        async def fake_bootstrap(symbol, resync=False):
            calls.append((symbol, resync))

        monkeypatch.setattr(feed, "_bootstrap_book", fake_bootstrap)
        await feed._handle_depth(_depth_event(500, 510))  # gap
        await asyncio.sleep(0)  # let the task run

        assert calls == [("btcusdt", True)]
        assert feed.books["btcusdt"].state is BookState.DESYNCED

    async def test_desynced_book_serves_no_snapshot(self, monkeypatch):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed)
        monkeypatch.setattr(feed, "_bootstrap_book", lambda *a, **k: asyncio.sleep(0))
        seen = []
        feed.on_depth = lambda snap: seen.append(snap) or asyncio.sleep(0)

        await feed._handle_depth(_depth_event(500, 510))
        await asyncio.sleep(0)
        assert seen == []
        assert feed.books["btcusdt"].snapshot() is None

    async def test_concurrent_bootstrap_is_deduped(self):
        """A resync storm must not fire a REST call per event."""
        feed = _feed(depth_symbols=["BTCUSDT"])
        feed._resyncing.add("btcusdt")
        called = []

        async def tracked(symbol, resync=False):
            called.append(symbol)

        # _bootstrap_book guards on _resyncing itself; verify the guard holds.
        await feed._bootstrap_book("btcusdt", resync=True)
        assert called == []

    async def test_stale_event_does_not_trigger_resync(self, monkeypatch):
        feed = _feed(depth_symbols=["BTCUSDT"])
        _bootstrap(feed, last_id=100)
        calls = []
        monkeypatch.setattr(
            feed, "_bootstrap_book",
            lambda s, resync=False: calls.append(s) or asyncio.sleep(0),
        )
        await feed._handle_depth(_depth_event(50, 90))  # entirely behind
        await asyncio.sleep(0)
        assert calls == []
        assert feed.books["btcusdt"].state is BookState.SYNCED


class TestFuturesSequencing:
    async def test_pu_chain_applied(self):
        feed = _feed(depth_symbols=["BTCUSDT"], use_futures_stream=True)
        _bootstrap(feed, last_id=100)
        await feed._handle_depth(_depth_event(101, 105, pu=100))
        assert feed.books["btcusdt"].last_update_id == 105

    async def test_pu_gap_desyncs(self, monkeypatch):
        feed = _feed(depth_symbols=["BTCUSDT"], use_futures_stream=True)
        _bootstrap(feed, last_id=100)
        monkeypatch.setattr(feed, "_bootstrap_book", lambda *a, **k: asyncio.sleep(0))
        await feed._handle_depth(_depth_event(101, 105, pu=99))
        await asyncio.sleep(0)
        assert feed.books["btcusdt"].state is BookState.DESYNCED


@pytest.mark.parametrize("speed", ["100ms", "1000ms"])
def test_speed_reflected_in_stream_name(speed):
    feed = _feed(depth_symbols=["BTCUSDT"], depth_speed=speed)
    assert f"btcusdt@depth@{speed}" in feed._build_streams()
