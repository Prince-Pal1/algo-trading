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
