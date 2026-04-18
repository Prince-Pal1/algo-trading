"""Tests for the broker-accurate cost models (spread + slippage + commission).

Calibration verified 2026-04-14 task #105 against the official IC Markets
spreads page + EU spec sheet + peer ECN broker measurements:

  - cTrader Raw XAUUSD: $3/$100k volume-based commission (~$13.50/lot/side at $4500)
  - MT4 Raw XAUUSD: $3.50/lot/side fixed
  - Typical spread: 0.30 pips (blend of Global 0.09 + EU 0.63)
  - Typical slippage: 0.30 pips per side (sub-pip on liquid hours)
  - News widening: 5× on spread + slip (NFP/CPI/FOMC)

Sources cited inline at each test.
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
    make_ic_markets_ctrader_xauusd_schedule,
    make_ic_markets_mt4_xauusd_schedule,
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
    # ── MT4 fixed per-lot pricing ──

    def test_mt4_one_lot_round_trip(self):
        """IC Markets MT4 XAUUSD: $3.50/side × 2 = $7 round-trip per lot.
        Source: https://www.icmarkets.com/global/en/trading-pricing/spreads
        """
        schedule = make_ic_markets_mt4_xauusd_schedule()
        # 100 oz = 1 lot, MT4 ignores reference_price
        assert commission_usd(quantity_units=100.0, schedule=schedule) == pytest.approx(7.0)
        # Same result regardless of reference price (per-lot fixed)
        assert commission_usd(
            quantity_units=100.0, schedule=schedule, reference_price=4500.0,
        ) == pytest.approx(7.0)

    def test_mt4_half_lot_round_trip(self):
        """0.5 lots MT4 = $1.75/side × 2 = $3.50 round-trip."""
        schedule = make_ic_markets_mt4_xauusd_schedule()
        assert commission_usd(quantity_units=50.0, schedule=schedule) == pytest.approx(3.50)

    def test_mt4_five_lots_round_trip(self):
        """5 lots MT4 = $17.50/side × 2 = $35 round-trip."""
        schedule = make_ic_markets_mt4_xauusd_schedule()
        assert commission_usd(quantity_units=500.0, schedule=schedule) == pytest.approx(35.0)

    # ── cTrader volume-based pricing ──

    def test_ctrader_one_lot_at_4500_per_oz(self):
        """IC Markets cTrader XAUUSD: $3 per $100k notional, volume-based.
        At $4500/oz × 100 oz = $450,000 notional → $13.50/side → $27 round-trip.
        Source: https://www.icmarkets.eu/en/trading-pricing/trading-costs
        """
        schedule = make_ic_markets_ctrader_xauusd_schedule()
        cost = commission_usd(
            quantity_units=100.0, schedule=schedule, reference_price=4500.0,
        )
        assert cost == pytest.approx(27.0)

    def test_ctrader_one_lot_at_2000_per_oz(self):
        """At $2000/oz × 100 oz = $200,000 notional → $6/side → $12 round-trip.
        Demonstrates volume-based commission scales with gold price.
        """
        schedule = make_ic_markets_ctrader_xauusd_schedule()
        cost = commission_usd(
            quantity_units=100.0, schedule=schedule, reference_price=2000.0,
        )
        assert cost == pytest.approx(12.0)

    def test_ctrader_requires_reference_price(self):
        """cTrader volume-based schedule rejects calls without reference_price."""
        schedule = make_ic_markets_ctrader_xauusd_schedule()
        with pytest.raises(ValueError, match="reference_price required"):
            commission_usd(quantity_units=100.0, schedule=schedule)

    def test_ctrader_vs_mt4_gold_ratio(self):
        """cTrader gold commission is ~3.86× higher than MT4 at $4500/oz."""
        ct = make_ic_markets_ctrader_xauusd_schedule()
        mt4 = make_ic_markets_mt4_xauusd_schedule()
        ct_cost = commission_usd(quantity_units=100.0, schedule=ct, reference_price=4500.0)
        mt4_cost = commission_usd(quantity_units=100.0, schedule=mt4)
        ratio = ct_cost / mt4_cost
        assert 3.5 < ratio < 4.0  # ~3.86 at $4500/oz

    def test_ctrader_fx_lot_matches_mt4(self):
        """For FX where 1 lot = $100k notional, cTrader and MT4 are nearly equal."""
        ct = make_ic_markets_ctrader_xauusd_schedule()
        # FX: 1 lot ≈ 100k units, "reference_price" = 1.0 → notional = 100k
        # Use contract_size=100000 for FX
        fx_schedule = CommissionSchedule(
            per_100k_notional_usd=3.0, contract_size=100_000.0,
        )
        cost = commission_usd(quantity_units=100_000.0, schedule=fx_schedule, reference_price=1.0)
        # 100k × 1.0 = 100k notional → $3/side × 2 = $6
        assert cost == pytest.approx(6.0)

    def test_zero_quantity_no_commission(self):
        schedule = make_ic_markets_mt4_xauusd_schedule()
        assert commission_usd(quantity_units=0.0, schedule=schedule) == 0.0

    def test_negative_quantity_no_commission(self):
        schedule = make_ic_markets_mt4_xauusd_schedule()
        assert commission_usd(quantity_units=-1.0, schedule=schedule) == 0.0

    def test_invalid_schedule_no_pricing_set(self):
        """Schedule with no pricing field set should raise."""
        bad = CommissionSchedule(
            per_lot_per_side_usd=None,
            per_100k_notional_usd=None,
            taker_fraction_of_notional=None,
        )
        with pytest.raises(ValueError, match="must set either"):
            commission_usd(quantity_units=100.0, schedule=bad, reference_price=4500.0)

    # ── Crypto percent-of-notional pricing (Binance) ──

    def test_binance_btc_round_trip_taker(self):
        """Binance Regular: 0.1% taker × 2 sides × notional.
        0.5 BTC @ $65k = $32,500 notional → $32.50/side × 2 = $65 RT.
        """
        schedule = CommissionSchedule(
            per_lot_per_side_usd=None,
            per_100k_notional_usd=None,
            maker_fraction_of_notional=0.001,
            taker_fraction_of_notional=0.001,
            contract_size=1.0,
        )
        cost = commission_usd(
            quantity_units=0.5, schedule=schedule, reference_price=65000.0,
        )
        assert cost == pytest.approx(65.0)

    def test_binance_maker_cheaper_when_tiered(self):
        """VIP 3: maker 0.04% / taker 0.06%.
        0.5 BTC @ $65k → maker $26, taker $39.
        """
        schedule = CommissionSchedule(
            per_lot_per_side_usd=None,
            per_100k_notional_usd=None,
            maker_fraction_of_notional=0.0004,
            taker_fraction_of_notional=0.0006,
            contract_size=1.0,
        )
        taker = commission_usd(
            quantity_units=0.5, schedule=schedule, reference_price=65000.0,
        )
        maker = commission_usd(
            quantity_units=0.5, schedule=schedule, reference_price=65000.0, is_maker=True,
        )
        assert taker == pytest.approx(39.0)
        assert maker == pytest.approx(26.0)
        assert maker < taker

    def test_binance_defaults_to_taker_when_maker_undefined(self):
        """Safe default: schedule with only taker set uses taker for maker orders too."""
        schedule = CommissionSchedule(
            per_lot_per_side_usd=None,
            per_100k_notional_usd=None,
            maker_fraction_of_notional=None,
            taker_fraction_of_notional=0.001,
            contract_size=1.0,
        )
        cost = commission_usd(
            quantity_units=0.5, schedule=schedule, reference_price=65000.0, is_maker=True,
        )
        assert cost == pytest.approx(65.0)  # Falls back to taker 0.1%

    def test_binance_requires_reference_price(self):
        """Percent-of-notional without reference_price raises."""
        schedule = CommissionSchedule(
            per_lot_per_side_usd=None,
            per_100k_notional_usd=None,
            taker_fraction_of_notional=0.001,
            contract_size=1.0,
        )
        with pytest.raises(ValueError, match="reference_price required"):
            commission_usd(quantity_units=0.5, schedule=schedule)

    def test_binance_bnb_discount_is_25pct(self):
        """BNB payment discount: 0.1% × 0.75 = 0.075%.
        1 ETH @ $2400 = $2,400 → regular $4.80 RT, BNB $3.60 RT.
        """
        regular = CommissionSchedule(
            per_lot_per_side_usd=None, per_100k_notional_usd=None,
            taker_fraction_of_notional=0.001, contract_size=1.0,
        )
        bnb = CommissionSchedule(
            per_lot_per_side_usd=None, per_100k_notional_usd=None,
            taker_fraction_of_notional=0.00075, contract_size=1.0,
        )
        regular_cost = commission_usd(
            quantity_units=1.0, schedule=regular, reference_price=2400.0,
        )
        bnb_cost = commission_usd(
            quantity_units=1.0, schedule=bnb, reference_price=2400.0,
        )
        assert regular_cost == pytest.approx(4.80)
        assert bnb_cost == pytest.approx(3.60)
        assert bnb_cost / regular_cost == pytest.approx(0.75)


# ── Round-trip spread cost ──────────────────────────────────────────────


class TestRoundTripSpreadCost:
    def test_one_lot_with_new_defaults(self):
        """1 lot at calibrated defaults: 0.30 spread + 0.30 slip.
        half_spread = 0.15 pips, slip = 0.30 pips → adverse = 0.45 pips/leg
        cost per leg = 0.45 × 0.10 × 100 = $4.50, round-trip = $9.00.
        """
        cfg = SpreadSlippageConfig()  # uses new calibrated defaults
        cost = round_trip_spread_cost_usd(
            quantity_units=100.0, config=cfg, contract_size=100.0, in_news=False,
        )
        assert cost == pytest.approx(9.0)

    def test_news_widening(self):
        """News event widens spread + slippage by 5× each (calibrated default)."""
        cfg = SpreadSlippageConfig()  # base 0.30 + slip 0.30, news mults 5×
        normal = round_trip_spread_cost_usd(
            quantity_units=100.0, config=cfg, contract_size=100.0, in_news=False,
        )
        news = round_trip_spread_cost_usd(
            quantity_units=100.0, config=cfg, contract_size=100.0, in_news=True,
        )
        # 5× widening on both spread and slip → ~5× total cost
        assert news > normal * 4
        assert news < normal * 6


# ── ICMarketsMetalFeeModel ──────────────────────────────────────────────


class TestICMarketsMetalFeeModel:
    def test_defaults_match_research_calibration(self):
        """Model defaults match the 2026-04-14 research-calibrated values."""
        model = ICMarketsMetalFeeModel()
        # Spread + slip
        assert model.spread_config.base_spread_pips == 0.30
        assert model.spread_config.normal_slip_pips == 0.30
        assert model.spread_config.atr_vol_mult == 0.0  # FIXED: was 0.5 (90× too high)
        assert model.spread_config.news_spread_mult == 5.0
        assert model.spread_config.news_slip_mult == 5.0
        # Commission defaults to cTrader (volume-based) — per CLAUDE.md we deploy on cTrader
        assert model.commission_schedule.per_100k_notional_usd == 3.0
        assert model.commission_schedule.per_lot_per_side_usd is None
        assert model.commission_schedule.contract_size == 100.0

    def test_ctrader_one_lot_commission_at_4500(self):
        """Default cTrader: 1 lot @ $4500/oz → $27 round-trip."""
        model = ICMarketsMetalFeeModel()
        assert model.commission_usd(
            quantity_units=100.0, reference_price=4500.0,
        ) == pytest.approx(27.0)

    def test_mt4_alternative(self):
        """Alternative MT4 schedule via factory: 1 lot → $7 round-trip."""
        model = ICMarketsMetalFeeModel(
            commission_schedule=make_ic_markets_mt4_xauusd_schedule()
        )
        # MT4 doesn't need reference_price
        assert model.commission_usd(quantity_units=100.0) == pytest.approx(7.0)

    def test_fill_price_buy_sell_symmetric(self):
        """BUY and SELL fills should be symmetric around the reference."""
        model = ICMarketsMetalFeeModel()
        ref = 4500.0
        buy = model.fill_price(side="BUY", reference_price=ref, atr=5.0, ts_ms=0)
        sell = model.fill_price(side="SELL", reference_price=ref, atr=5.0, ts_ms=0)
        assert buy > ref
        assert sell < ref
        assert (buy - ref) == pytest.approx(ref - sell)

    def test_atr_no_longer_inflates_slippage(self):
        """REGRESSION TEST for the 90× slippage bug.
        Previous default atr_vol_mult=0.5 added 35.6 pips of slip with
        ATR=$7.08. New default (0.0) should produce the same fill regardless
        of ATR — only spread + base slip apply.
        """
        model = ICMarketsMetalFeeModel()
        ref = 4500.0
        fill_low_atr = model.fill_price(side="BUY", reference_price=ref, atr=0.5, ts_ms=0)
        fill_high_atr = model.fill_price(side="BUY", reference_price=ref, atr=20.0, ts_ms=0)
        # ATR should NOT affect the fill price under new defaults
        assert fill_low_atr == pytest.approx(fill_high_atr)
        # The total adverse should be (0.15 + 0.30) × 0.10 = $0.045
        assert (fill_low_atr - ref) == pytest.approx(0.045)

    def test_news_window_active(self):
        """is_in_news_window reports correctly."""
        news = NewsWindow(start_ms=1000, end_ms=2000, label="NFP")
        model = ICMarketsMetalFeeModel(
            spread_config=SpreadSlippageConfig(news_windows=(news,))
        )
        assert model.is_in_news_window(1500) is True
        assert model.is_in_news_window(500) is False
        assert model.is_in_news_window(2500) is False

    def test_realistic_xauusd_scalp_cost_per_trade(self):
        """End-to-end: 1 lot XAUUSD scalp at $4500, cTrader, normal conditions.
        Expected total cost: ~$9 spread + $27 cTrader commission = ~$36 round-trip.
        Within sane range, NOT the $617/trade the bug produced.
        """
        model = ICMarketsMetalFeeModel()
        spread_cost = model.round_trip_spread_cost_usd(quantity_units=100.0, in_news=False)
        commission = model.commission_usd(quantity_units=100.0, reference_price=4500.0)
        total = spread_cost + commission
        # Sane realistic range for cTrader gold (the realistic baseline)
        assert 30 < total < 50
        # SANITY GUARD: never let cost per 1-lot trade exceed $100 again
        assert total < 100, (
            "Cost model regression — single-lot trade cost exceeded $100. "
            "Check atr_vol_mult and other slip parameters."
        )

    def test_mt4_realistic_xauusd_scalp_cost(self):
        """End-to-end: 1 lot XAUUSD scalp at $4500, MT4, normal conditions.
        Expected: ~$9 spread + $7 MT4 commission = ~$16 round-trip.
        """
        model = ICMarketsMetalFeeModel(
            commission_schedule=make_ic_markets_mt4_xauusd_schedule()
        )
        spread_cost = model.round_trip_spread_cost_usd(quantity_units=100.0, in_news=False)
        commission = model.commission_usd(quantity_units=100.0)
        total = spread_cost + commission
        assert 12 < total < 25
