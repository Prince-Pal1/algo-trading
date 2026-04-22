from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from src.data.feeds.icmarkets_feed import (
    CTRADER_AVAILABLE,
    ICMarketsConfig,
    ICMarketsFeed,
)

pytestmark = pytest.mark.skipif(
    not CTRADER_AVAILABLE,
    reason="ctrader-open-api not installed",
)


class TestICMarketsConfig:
    def test_from_env_demo(self, monkeypatch):
        monkeypatch.setenv("CTRADER_CLIENT_ID", "test_client_id")
        monkeypatch.setenv("CTRADER_CLIENT_SECRET", "test_secret")
        monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "test_access_token")
        monkeypatch.setenv("CTRADER_ACCOUNT_ID", "5012345")
        monkeypatch.setenv("CTRADER_ENVIRONMENT", "demo")
        monkeypatch.delenv("CTRADER_DEMO_HOST", raising=False)
        monkeypatch.delenv("CTRADER_PORT", raising=False)

        cfg = ICMarketsConfig.from_env()
        assert cfg.client_id == "test_client_id"
        assert cfg.client_secret == "test_secret"
        assert cfg.access_token == "test_access_token"
        assert cfg.account_id == 5012345
        assert cfg.host == "demo.ctraderapi.com"
        assert cfg.port == 5035

    def test_from_env_live(self, monkeypatch):
        monkeypatch.setenv("CTRADER_CLIENT_ID", "x")
        monkeypatch.setenv("CTRADER_CLIENT_SECRET", "y")
        monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "z")
        monkeypatch.setenv("CTRADER_ACCOUNT_ID", "1")
        monkeypatch.setenv("CTRADER_ENVIRONMENT", "live")
        monkeypatch.delenv("CTRADER_LIVE_HOST", raising=False)

        cfg = ICMarketsConfig.from_env()
        assert cfg.host == "live.ctraderapi.com"


class TestICMarketsFeedConstruction:
    def _cfg(self) -> ICMarketsConfig:
        return ICMarketsConfig(
            client_id="test_client",
            client_secret="test_secret",
            access_token="test_token",
            account_id=5012345,
        )

    def test_construction(self):
        feed = ICMarketsFeed(
            self._cfg(),
            symbols=["XAUUSD"],
            timeframes=["5m"],
        )
        assert feed._symbols == ["XAUUSD"]
        assert feed._timeframes == ["5m"]
        assert feed._client is None
        assert feed.on_tick is None
        assert feed.on_candle is None

    def test_default_timeframe(self):
        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD"])
        assert feed._timeframes == ["5m"]

    def test_symbol_name_lookup(self):
        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD"])
        feed._symbol_id_map = {"XAUUSD": 41, "EURUSD": 1}
        assert feed._symbol_name_for_id(41) == "XAUUSD"
        assert feed._symbol_name_for_id(1) == "EURUSD"
        assert feed._symbol_name_for_id(999) is None


class TestICMarketsFeedMessageDispatch:
    def _cfg(self) -> ICMarketsConfig:
        return ICMarketsConfig(
            client_id="x", client_secret="y", access_token="z", account_id=1,
        )

    def test_spot_event_with_no_callback_is_noop(self):
        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD"])
        feed._symbol_id_map = {"XAUUSD": 41}

        event = MagicMock()
        event.symbolId = 41
        event.HasField = lambda field: True
        event.bid = 240000000  # 2400.00 in 5-decimal fixed point
        event.timestamp = 1_700_000_000_000

        # No tick callback set → must not raise
        feed._handle_spot_event(event)

    def test_subscribe_trendbars_skips_unknown_symbol(self):
        feed = ICMarketsFeed(self._cfg(), symbols=["UNKNOWN"])
        feed._symbol_id_map = {"XAUUSD": 41}
        feed._client = MagicMock()
        feed._subscribe_trendbars()
        # Nothing sent because UNKNOWN isn't in the map
        feed._client.send.assert_not_called()


class TestTrendbarPartialBarFix:
    """Regression tests for the 2026-04-22 partial-bar bug.

    cTrader emits multiple ProtoOATrendbar updates per bar with the same
    utcTimestampInMinutes. The feed must ONLY emit a Candle when a NEW
    bar timestamp arrives (meaning the previous bar is finalized).
    """

    def _cfg(self) -> ICMarketsConfig:
        return ICMarketsConfig(
            client_id="x", client_secret="y", access_token="z", account_id=1,
        )

    def _feed_with_loop(self, captured_candles: list):
        """Construct a feed wired to an AsyncMock that captures emitted candles."""
        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD"], timeframes=["1h"])
        feed._symbol_id_map = {"XAUUSD": 41}
        # Stub loop + on_candle
        loop = MagicMock()
        loop.is_closed = lambda: False

        def _capture(coro, _):
            # We don't actually run the coro; we just capture the candle
            # that was passed to on_candle when asyncio.run_coroutine_threadsafe
            # was called. The on_candle callable was invoked to PRODUCE the
            # coroutine. Here we bypass the coro mechanics and rely on the
            # spy below.
            return MagicMock(result=lambda timeout=None: None)

        feed._loop = loop
        # Make on_candle a sync spy that collects candles.
        async def on_candle(c):
            captured_candles.append(c)
        feed.on_candle = on_candle
        return feed, loop

    def _mock_trendbar(self, tb_ts_min: int, period: int = 9, volume: int = 100):
        """Build a mock ProtoOATrendbar with controllable timestamp/volume."""
        tb = MagicMock()
        tb.period = period  # H1 = 9
        tb.low = 250_000_000  # 2500.00
        tb.deltaHigh = 500_000   # +5.00 → high 2505
        tb.deltaOpen = 100_000   # +1.00 → open 2501
        tb.deltaClose = 400_000  # +4.00 → close 2504
        tb.volume = volume
        tb.utcTimestampInMinutes = tb_ts_min
        return tb

    def _mock_spot_event(self, trendbars: list):
        event = MagicMock()
        event.symbolId = 41
        event.HasField = lambda field: field in ("bid", "ask", "timestamp")
        event.bid = 0
        event.ask = 0
        event.timestamp = 0
        event.trendbar = trendbars
        return event

    def test_partial_bar_updates_do_not_emit_candle(self):
        """Two trendbar updates for the same bar → on_candle never called."""
        captured = []
        feed, loop = self._feed_with_loop(captured)

        # Two updates for the same 1h bar at minute 28,800,000 (example)
        tb_a = self._mock_trendbar(tb_ts_min=28_800_000, volume=100)
        tb_b = self._mock_trendbar(tb_ts_min=28_800_000, volume=250)
        feed._handle_spot_event(self._mock_spot_event([tb_a]))
        feed._handle_spot_event(self._mock_spot_event([tb_b]))

        # No emission (both updates belong to the same in-progress bar)
        assert loop.call_args_list == []  # Not called via run_coroutine_threadsafe
        # The in-progress bar should be tracked
        assert ("XAUUSD", "1h") in feed._in_progress_bar

    def test_new_bar_emits_previous_finalized(self):
        """Feed bar N, then N+1 → on_candle called ONCE with bar N."""
        captured_calls = []

        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD"], timeframes=["1h"])
        feed._symbol_id_map = {"XAUUSD": 41}

        async def fake_on_candle(c):
            captured_calls.append(c)
        feed.on_candle = fake_on_candle

        loop = MagicMock()
        feed._loop = loop

        def _run_coro_threadsafe(coro, _loop):
            # Run the coroutine synchronously via asyncio.run for the test
            import asyncio
            asyncio.get_event_loop().run_until_complete(coro)
            return MagicMock()

        with patch(
            "src.data.feeds.icmarkets_feed.asyncio.run_coroutine_threadsafe",
            side_effect=_run_coro_threadsafe,
        ):
            # Bar N at minute 28,800,000
            tb_n = self._mock_trendbar(tb_ts_min=28_800_000, volume=100)
            feed._handle_spot_event(self._mock_spot_event([tb_n]))
            # No emission yet — this is the first bar, so in-progress only.
            assert captured_calls == []

            # Bar N+1 (+60 min) at minute 28,800,060
            tb_n1 = self._mock_trendbar(tb_ts_min=28_800_060, volume=150)
            feed._handle_spot_event(self._mock_spot_event([tb_n1]))
            # Previous bar (N) should have been emitted once — finalized.
            assert len(captured_calls) == 1
            # Timestamp of emitted bar is close-of-N = 28,800,000 + 60 = 28,800,060 minutes
            # in ms: 28,800,060 * 60 * 1000
            expected_ts_ms = (28_800_000 + 60) * 60 * 1000
            assert captured_calls[0].timestamp == expected_ts_ms
            assert captured_calls[0].volume == 100  # bar N's final volume

    def test_third_bar_emits_second(self):
        """N → N+1 → N+2: total emitted should be 2 (N and N+1)."""
        captured = []

        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD"], timeframes=["1h"])
        feed._symbol_id_map = {"XAUUSD": 41}

        async def on_candle(c):
            captured.append(c)
        feed.on_candle = on_candle
        feed._loop = MagicMock()

        def _run(coro, _loop):
            import asyncio
            asyncio.get_event_loop().run_until_complete(coro)
            return MagicMock()

        with patch(
            "src.data.feeds.icmarkets_feed.asyncio.run_coroutine_threadsafe",
            side_effect=_run,
        ):
            for mins in (28_800_000, 28_800_060, 28_800_120):
                feed._handle_spot_event(self._mock_spot_event([
                    self._mock_trendbar(tb_ts_min=mins, volume=100),
                ]))
            # After 3 bars arriving, the FIRST TWO should have been emitted
            # (bar 3 is still in-progress).
            assert len(captured) == 2

    def test_different_symbols_are_isolated(self):
        """A bar on XAUUSD shouldn't trigger emission on a different symbol."""
        captured = []

        feed = ICMarketsFeed(self._cfg(), symbols=["XAUUSD", "EURUSD"],
                             timeframes=["1h"])
        feed._symbol_id_map = {"XAUUSD": 41, "EURUSD": 1}

        async def on_candle(c):
            captured.append(c)
        feed.on_candle = on_candle
        feed._loop = MagicMock()

        def _run(coro, _loop):
            import asyncio
            asyncio.get_event_loop().run_until_complete(coro)
            return MagicMock()

        with patch(
            "src.data.feeds.icmarkets_feed.asyncio.run_coroutine_threadsafe",
            side_effect=_run,
        ):
            # XAUUSD bar N (in-progress — should not emit)
            evt_a = MagicMock()
            evt_a.symbolId = 41
            evt_a.HasField = lambda f: f in ("bid", "ask", "timestamp")
            evt_a.bid = evt_a.ask = evt_a.timestamp = 0
            evt_a.trendbar = [self._mock_trendbar(tb_ts_min=28_800_000, volume=100)]
            feed._handle_spot_event(evt_a)
            assert captured == []

            # EURUSD bar M (different symbol — separate in-progress slot,
            # also should not emit on first bar)
            evt_b = MagicMock()
            evt_b.symbolId = 1
            evt_b.HasField = lambda f: f in ("bid", "ask", "timestamp")
            evt_b.bid = evt_b.ask = evt_b.timestamp = 0
            evt_b.trendbar = [self._mock_trendbar(tb_ts_min=28_800_000, volume=200)]
            feed._handle_spot_event(evt_b)
            assert captured == []

            # Now XAUUSD bar N+1 — emits bar N for XAUUSD only
            evt_a2 = MagicMock()
            evt_a2.symbolId = 41
            evt_a2.HasField = lambda f: f in ("bid", "ask", "timestamp")
            evt_a2.bid = evt_a2.ask = evt_a2.timestamp = 0
            evt_a2.trendbar = [self._mock_trendbar(tb_ts_min=28_800_060, volume=150)]
            feed._handle_spot_event(evt_a2)
            assert len(captured) == 1
            assert captured[0].symbol == "XAUUSD"
