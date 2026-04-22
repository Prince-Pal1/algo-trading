"""Routing helpers in src/main.py — broker selection + strategy filter."""

from __future__ import annotations

import pytest

from src.main import _strategy_belongs_to_broker, _symbol_belongs_to_broker


class TestSymbolBelongsToBroker:
    def test_xauusd_is_ctrader(self):
        assert _symbol_belongs_to_broker("XAUUSD", "ic_markets_ctrader") is True
        assert _symbol_belongs_to_broker("XAUUSD", "binance") is False
        assert _symbol_belongs_to_broker("XAUUSD", "binance_perpetual_futures") is False

    def test_btcusdt_is_binance(self):
        assert _symbol_belongs_to_broker("BTCUSDT", "binance") is True
        assert _symbol_belongs_to_broker("BTCUSDT", "ic_markets_ctrader") is False

    def test_case_insensitive(self):
        assert _symbol_belongs_to_broker("xauusd", "ic_markets_ctrader") is True
        assert _symbol_belongs_to_broker("btcusdt", "binance") is True


class TestStrategyBelongsToBroker:
    def test_explicit_broker_wins(self):
        """`broker =` field in strategy config force-routes regardless of symbol."""
        strat = {"markets": ["BTCUSDT"], "broker": "binance_perpetual_futures"}
        assert _strategy_belongs_to_broker(strat, "binance_perpetual_futures") is True
        assert _strategy_belongs_to_broker(strat, "binance") is False
        assert _strategy_belongs_to_broker(strat, "ic_markets_ctrader") is False

    def test_no_explicit_falls_back_to_symbol(self):
        """Without `broker` field, routes by symbol."""
        strat = {"markets": ["BTCUSDT"]}
        assert _strategy_belongs_to_broker(strat, "binance") is True
        assert _strategy_belongs_to_broker(strat, "ic_markets_ctrader") is False

    def test_no_explicit_never_auto_routes_to_perp(self):
        """Perp engine must ONLY pick up strategies that explicitly opt in.

        This prevents accidental migration of existing crypto strategies
        (which are live-validated on spot fees) onto the perp engine where
        they'd hit real money with different cost structure.
        """
        strat = {"markets": ["BTCUSDT"]}
        assert _strategy_belongs_to_broker(strat, "binance_perpetual_futures") is False

    def test_gold_routes_to_ctrader(self):
        strat = {"markets": ["XAUUSD"]}
        assert _strategy_belongs_to_broker(strat, "ic_markets_ctrader") is True
        assert _strategy_belongs_to_broker(strat, "binance") is False

    def test_empty_markets_rejects_everything(self):
        strat = {"markets": []}
        assert _strategy_belongs_to_broker(strat, "binance") is False
        assert _strategy_belongs_to_broker(strat, "ic_markets_ctrader") is False
        assert _strategy_belongs_to_broker(strat, "binance_perpetual_futures") is False

    def test_explicit_perp_overrides_gold_symbol(self):
        """If you really want to trade gold on Binance perps... the explicit broker
        field lets you. Shouldn't happen in practice (Binance doesn't list XAU),
        but the routing doesn't block it."""
        strat = {"markets": ["XAUUSD"], "broker": "binance_perpetual_futures"}
        assert _strategy_belongs_to_broker(strat, "binance_perpetual_futures") is True
        assert _strategy_belongs_to_broker(strat, "ic_markets_ctrader") is False
