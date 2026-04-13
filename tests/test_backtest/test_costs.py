"""Tests for the broker-accurate cost models (spread + slippage + commission).

Validates that the IC Markets cTrader raw XAUUSD cost model matches
the real-world numbers: $3/side per standard lot commission, 0.13 pip
typical spread, 10× spread widening during news events.
"""

from __future__ import annotations

import pytest

from src.backtest.costs import (
    CommissionSchedule,
    ICMarketsMetalFeeModel,
    NewsWindow,
    SpreadSlippageConfig,
    commission_usd,
    fill_price,
    round_trip_spread_cost_usd,
)


# ── NewsWindow ─────────────────────────────────────────────────────────


class TestNewsWindow:
    def test_contains_inclusive(self):
        w = NewsWindow(start_ms=1000, end_ms=2000)
        assert w.contains(1000) is True
        assert w.contains(1500) is True
        assert w.contains(2000) is True
        assert w.contains(999) is False
        assert w.contains(2001) is False


# ── Spread + slippage ───────────────────────────────────────────────────


class TestFillPrice:
    def test_buy_pays_ask(self):
        cfg = SpreadSlippageConfig(base_spread_pips=0.2, normal_slip_pips=0.0)
        fill = fill_price(
            side="BUY", reference_price=2400.00, atr=0.0, ts_ms=0, config=cfg,
        )
        # half_spread_pips = 0.1, slip = 0, pip_size = 0.10
        # adverse = (0.1 + 0) * 0.10 = $0.01
        assert fill == pytest.approx(2400.01)

    def test_sell_pays_bid(self):
        cfg = SpreadSlippageConfig(base_spread_pips=0.2, normal_slip_pips=0.0)
        fill = fill_price(
            side="SELL", reference_price=2400.00, atr=0.0, ts_ms=0, config=cfg,
        )
        assert fill == pytest.approx(2399.99)

    def test_atr_scales_slippage(self):
        cfg = SpreadSlippageConfig(
            base_spread_pips=0.0,  # isolate slippage
            normal_slip_pips=0.0,
            atr_vol_mult=1.0,
        )
        fill_low_vol = fill_price(
            side="BUY", reference_price=2400.0, atr=0.0, ts_ms=0, config=cfg,
        )
        fill_high_vol = fill_price(
            side="BUY", reference_price=2400.0, atr=2.0, ts_ms=0, config=cfg,
        )
        # High-vol fill should be worse
        assert fill_high_vol > fill_low_vol

    def test_news_window_widens_spread(self):
        news = NewsWindow(start_ms=1000, end_ms=2000, label="NFP")
        cfg = SpreadSlippageConfig(
            base_spread_pips=0.2,
            normal_slip_pips=0.0,
            atr_vol_mult=0.0,
            news_windows=(news,),
            news_spread_mult=10.0,
            news_slip_mult=1.0,
        )
        # Outside news window
        fill_normal = fill_price(
            side="BUY", reference_price=2400.0, atr=0.0, ts_ms=500, config=cfg,
        )
        # Inside news window
        fill_news = fill_price(
            side="BUY", reference_price=2400.0, atr=0.0, ts_ms=1500, config=cfg,
        )
        normal_adverse = fill_normal - 2400.0
        news_adverse = fill_news - 2400.0
        # News adverse should be 10× normal adverse
        assert news_adverse == pytest.approx(normal_adverse * 10)

    def test_invalid_side_raises(self):
        cfg = SpreadSlippageConfig()
        with pytest.raises(ValueError, match="side must be BUY or SELL"):
            fill_price(side="LONG", reference_price=2400.0, atr=0.0, ts_ms=0, config=cfg)

    def test_invalid_price_raises(self):
        cfg = SpreadSlippageConfig()
        with pytest.raises(ValueError, match="reference_price must be > 0"):
            fill_price(side="BUY", reference_price=0.0, atr=0.0, ts_ms=0, config=cfg)


# ── Commission ──────────────────────────────────────────────────────────


class TestCommission:
    def test_one_lot_round_trip(self):
        """IC Markets XAUUSD 1 lot = 100 oz = $3/side × 2 = $6 round-trip."""
        schedule = CommissionSchedule(
            per_lot_per_side_usd=3.0, contract_size=100.0, min_commission_usd=0.0,
        )
        # 100 oz = 1 lot
        assert commission_usd(quantity_units=100.0, schedule=schedule) == pytest.approx(6.0)

    def test_half_lot_round_trip(self):
        """0.5 lots = $1.50/side × 2 = $3 round-trip."""
        schedule = CommissionSchedule(per_lot_per_side_usd=3.0, contract_size=100.0)
        assert commission_usd(quantity_units=50.0, schedule=schedule) == pytest.approx(3.0)

    def test_five_lots_round_trip(self):
        """5 lots = $15/side × 2 = $30 round-trip."""
        schedule = CommissionSchedule(per_lot_per_side_usd=3.0, contract_size=100.0)
        assert commission_usd(quantity_units=500.0, schedule=schedule) == pytest.approx(30.0)

    def test_micro_lot_min_commission(self):
        """With min commission, tiny positions still pay the floor."""
        schedule = CommissionSchedule(
            per_lot_per_side_usd=3.0,
            contract_size=100.0,
            min_commission_usd=0.10,  # 10 cents per side floor
        )
        # 1 oz = 0.01 lot = 1 cent/side commission < 10 cent floor
        # → floor applies, per leg = $0.10, round-trip = $0.20
        assert commission_usd(quantity_units=1.0, schedule=schedule) == pytest.approx(0.20)

    def test_zero_quantity_no_commission(self):
        schedule = CommissionSchedule()
        assert commission_usd(quantity_units=0.0, schedule=schedule) == 0.0

    def test_negative_quantity_no_commission(self):
        schedule = CommissionSchedule()
        assert commission_usd(quantity_units=-1.0, schedule=schedule) == 0.0


# ── Round-trip spread cost ──────────────────────────────────────────────


class TestRoundTripSpreadCost:
    def test_one_lot_normal(self):
        """1 lot at 0.13 spread + 0.2 slip = (0.065 + 0.2) * 0.10 * 100 = $2.65/leg,
        $5.30 round-trip."""
        cfg = SpreadSlippageConfig(
            base_spread_pips=0.13,
            normal_slip_pips=0.2,
        )
        cost = round_trip_spread_cost_usd(
            quantity_units=100.0, config=cfg, contract_size=100.0, in_news=False,
        )
        # half_spread = 0.065 pips
        # slip = 0.2 pips (no ATR contribution in static estimate)
        # adverse per leg = (0.065 + 0.2) * 0.10 = $0.0265/oz
        # cost per leg = $0.0265 * 100 = $2.65
        # round-trip = $5.30
        assert cost == pytest.approx(5.30)

    def test_news_widening(self):
        """News event widens spread + slippage by the configured multipliers."""
        cfg = SpreadSlippageConfig(
            base_spread_pips=0.13,
            normal_slip_pips=0.2,
            news_spread_mult=10.0,
            news_slip_mult=8.0,
        )
        normal = round_trip_spread_cost_usd(
            quantity_units=100.0, config=cfg, contract_size=100.0, in_news=False,
        )
        news = round_trip_spread_cost_usd(
            quantity_units=100.0, config=cfg, contract_size=100.0, in_news=True,
        )
        # News cost should be substantially larger
        assert news > normal * 5


# ── ICMarketsMetalFeeModel ──────────────────────────────────────────────


class TestICMarketsMetalFeeModel:
    def test_defaults_match_ic_markets(self):
        """Model defaults should match Brokerchooser-verified IC Markets numbers."""
        model = ICMarketsMetalFeeModel()
        assert model.spread_config.base_spread_pips == 0.13
        assert model.commission_schedule.per_lot_per_side_usd == 3.0
        assert model.commission_schedule.contract_size == 100.0

    def test_one_lot_commission(self):
        """1 lot round-trip = $6 at IC Markets defaults."""
        model = ICMarketsMetalFeeModel()
        assert model.commission_usd(quantity_units=100.0) == pytest.approx(6.0)

    def test_fill_price_buy_sell_symmetric(self):
        """BUY and SELL fills should be symmetric around the reference."""
        model = ICMarketsMetalFeeModel()
        ref = 2400.0
        buy = model.fill_price(side="BUY", reference_price=ref, atr=1.0, ts_ms=0)
        sell = model.fill_price(side="SELL", reference_price=ref, atr=1.0, ts_ms=0)
        # Buy should fill above ref, sell below ref
        assert buy > ref
        assert sell < ref
        # Symmetric around the reference
        assert (buy - ref) == pytest.approx(ref - sell)

    def test_news_window_active(self):
        """is_in_news_window reports correctly."""
        news = NewsWindow(start_ms=1000, end_ms=2000, label="NFP")
        model = ICMarketsMetalFeeModel(
            spread_config=SpreadSlippageConfig(news_windows=(news,))
        )
        assert model.is_in_news_window(1500) is True
        assert model.is_in_news_window(500) is False
        assert model.is_in_news_window(2500) is False

    def test_realistic_xauusd_scalp_cost(self):
        """End-to-end: 1 lot XAUUSD M5 scalp, normal market conditions.
        Expected total cost: ~$5.30 spread + $6.00 commission = ~$11.30 round-trip.
        """
        model = ICMarketsMetalFeeModel()
        spread_cost = model.round_trip_spread_cost_usd(
            quantity_units=100.0, in_news=False,
        )
        commission = model.commission_usd(quantity_units=100.0)
        total = spread_cost + commission
        # Documented IC Markets cost: ~$7-10/round-trip per 1-lot XAUUSD
        # (spread $1-3 + commission $6). Our model with conservative slip
        # should come in at ~$11 which is acceptable for a backtest
        # (slightly pessimistic vs real).
        assert 8.0 < total < 15.0
        assert commission == pytest.approx(6.0)
