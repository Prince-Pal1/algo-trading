"""Tests for ZeroCostFeeModel — used for Pine-faithful backtests."""

from __future__ import annotations

import pytest

from src.backtest.costs import ZeroCostFeeModel


class TestZeroCostFeeModel:
    def test_fill_price_returns_reference_exactly(self):
        m = ZeroCostFeeModel()
        assert m.fill_price(side="BUY", reference_price=2400.0, atr=3.0, ts_ms=1000) == 2400.0
        assert m.fill_price(side="SELL", reference_price=2400.0, atr=3.0, ts_ms=1000) == 2400.0

    def test_fill_price_no_bid_ask_spread(self):
        """Buy and sell prices at the same reference are identical — no spread."""
        m = ZeroCostFeeModel()
        buy = m.fill_price(side="BUY", reference_price=2400.0, atr=3.0, ts_ms=1000)
        sell = m.fill_price(side="SELL", reference_price=2400.0, atr=3.0, ts_ms=1000)
        assert buy == sell

    def test_commission_is_zero(self):
        m = ZeroCostFeeModel()
        assert m.commission_usd(quantity_units=1.0) == 0.0
        assert m.commission_usd(quantity_units=100.0) == 0.0
        assert m.commission_usd(quantity_units=0.0) == 0.0

    def test_round_trip_spread_cost_is_zero(self):
        m = ZeroCostFeeModel()
        assert m.round_trip_spread_cost_usd(quantity_units=10.0, in_news=False) == 0.0
        assert m.round_trip_spread_cost_usd(quantity_units=10.0, in_news=True) == 0.0

    def test_news_window_always_false(self):
        m = ZeroCostFeeModel()
        assert m.is_in_news_window(ts_ms=0) is False
        assert m.is_in_news_window(ts_ms=1_000_000_000_000) is False

    def test_interface_matches_ic_markets_model(self):
        """Duck-typed drop-in: same method signatures as ICMarketsMetalFeeModel."""
        from src.backtest.costs import ICMarketsMetalFeeModel
        ic = ICMarketsMetalFeeModel()
        zc = ZeroCostFeeModel()

        # Both should accept the same kwargs
        ic_fill = ic.fill_price(side="BUY", reference_price=2400.0, atr=3.0, ts_ms=1000)
        zc_fill = zc.fill_price(side="BUY", reference_price=2400.0, atr=3.0, ts_ms=1000)
        assert isinstance(ic_fill, float)
        assert isinstance(zc_fill, float)

        # cTrader (default) needs reference_price; both should accept it
        ic_comm = ic.commission_usd(quantity_units=20.833, reference_price=2400.0)
        zc_comm = zc.commission_usd(quantity_units=20.833, reference_price=2400.0)
        assert isinstance(ic_comm, float)
        assert isinstance(zc_comm, float)

    def test_ic_markets_has_cost_zero_cost_has_none(self):
        """Sanity: the real model DOES charge, the zero model does NOT."""
        from src.backtest.costs import ICMarketsMetalFeeModel
        ic = ICMarketsMetalFeeModel()
        zc = ZeroCostFeeModel()

        # IC Markets has a nonzero spread on fill
        ic_buy = ic.fill_price(side="BUY", reference_price=2400.0, atr=3.0, ts_ms=1000)
        ic_sell = ic.fill_price(side="SELL", reference_price=2400.0, atr=3.0, ts_ms=1000)
        assert ic_buy > 2400.0  # pays the ask
        assert ic_sell < 2400.0  # receives the bid

        # Zero cost: both at 2400 exactly
        assert zc.fill_price(side="BUY", reference_price=2400.0, atr=3.0, ts_ms=1000) == 2400.0
        assert zc.fill_price(side="SELL", reference_price=2400.0, atr=3.0, ts_ms=1000) == 2400.0

        # IC Markets has nonzero commission (cTrader default needs reference_price)
        assert ic.commission_usd(quantity_units=100.0, reference_price=2400.0) > 0
        assert zc.commission_usd(quantity_units=100.0, reference_price=2400.0) == 0.0
