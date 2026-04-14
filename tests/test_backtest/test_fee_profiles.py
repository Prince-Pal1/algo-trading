"""Unit tests for the broker fee profile registry."""

from __future__ import annotations

import pytest

from src.backtest.costs import (
    ICMarketsMetalFeeModel,
    SpreadSlippageConfig,
    ZeroCostFeeModel,
)
from src.backtest.fee_profiles import (
    FeeProfile,
    cost_as_pct_of_margin,
    get_profile,
    list_profiles,
    make_fee_model,
    select_profile,
)


class TestListProfiles:
    def test_returns_all_when_no_filter(self):
        profiles = list_profiles()
        # Should have at least the 7 profiles defined in broker_fees.toml
        assert len(profiles) >= 7
        assert "ic_markets_ctrader_xauusd_normal" in profiles
        assert "ic_markets_mt4_xauusd_normal" in profiles
        assert "pine_zero_cost" in profiles

    def test_filter_by_broker(self):
        ic = list_profiles(broker="IC Markets")
        # All IC Markets profiles
        assert all("ic_markets" in name for name in ic)
        # Pine zero cost is "(none)" broker, should be excluded
        assert "pine_zero_cost" not in ic

    def test_filter_by_platform(self):
        ct = list_profiles(platform="ctrader")
        assert all("ctrader" in name for name in ct)
        mt4 = list_profiles(platform="mt4")
        assert all("mt4" in name for name in mt4)
        # No overlap
        assert set(ct).isdisjoint(set(mt4))

    def test_filter_by_instrument_class(self):
        gold = list_profiles(instrument_class="xauusd_metals")
        fx = list_profiles(instrument_class="fx_majors")
        assert all("xauusd" in name for name in gold)
        assert all("fx" in name for name in fx)

    def test_filter_by_scenario(self):
        normal = list_profiles(scenario="normal")
        assert "ic_markets_ctrader_xauusd_normal" in normal
        assert "ic_markets_mt4_xauusd_normal" in normal
        assert "ic_markets_ctrader_xauusd_news_active" not in normal

    def test_combined_filters(self):
        # IC Markets + cTrader + gold + normal → exactly one match
        matches = list_profiles(
            broker="IC Markets",
            platform="ctrader",
            instrument_class="xauusd_metals",
            scenario="normal",
        )
        assert matches == ["ic_markets_ctrader_xauusd_normal"]


class TestGetProfile:
    def test_returns_fee_profile_object(self):
        p = get_profile("ic_markets_ctrader_xauusd_normal")
        assert isinstance(p, FeeProfile)
        assert p.name == "ic_markets_ctrader_xauusd_normal"
        assert p.broker == "IC Markets"
        assert p.platform == "ctrader"

    def test_unknown_profile_raises(self):
        with pytest.raises(KeyError, match="Unknown fee profile"):
            get_profile("nonexistent_broker_xyz")

    def test_ctrader_xauusd_normal_calibration(self):
        """The cTrader gold profile must match the research-calibrated values."""
        p = get_profile("ic_markets_ctrader_xauusd_normal")
        assert p.spread_config.base_spread_pips == 0.30
        assert p.spread_config.normal_slip_pips == 0.30
        assert p.spread_config.atr_vol_mult == 0.0  # FIXED — was 0.5
        assert p.spread_config.news_spread_mult == 5.0
        assert p.commission_schedule.per_100k_notional_usd == 3.0
        assert p.commission_schedule.per_lot_per_side_usd is None  # cTrader is volume-based
        assert p.commission_schedule.contract_size == 100.0

    def test_mt4_xauusd_normal_calibration(self):
        """The MT4 gold profile uses fixed per-lot commission."""
        p = get_profile("ic_markets_mt4_xauusd_normal")
        assert p.commission_schedule.per_lot_per_side_usd == 3.50
        assert p.commission_schedule.per_100k_notional_usd is None
        assert p.commission_schedule.contract_size == 100.0

    def test_stress_profile_doubles_spread_and_slip(self):
        normal = get_profile("ic_markets_ctrader_xauusd_normal")
        stress = get_profile("ic_markets_ctrader_xauusd_stress")
        assert stress.spread_config.base_spread_pips == 2 * normal.spread_config.base_spread_pips
        assert stress.spread_config.normal_slip_pips == 2 * normal.spread_config.normal_slip_pips

    def test_pine_zero_cost_has_zero_everything(self):
        p = get_profile("pine_zero_cost")
        assert p.spread_config.base_spread_pips == 0.0
        assert p.spread_config.normal_slip_pips == 0.0
        assert p.scenario == "pine_faithful"

    def test_profiles_have_sources_unless_pine(self):
        """All real broker profiles must cite at least one source URL."""
        for name in list_profiles():
            p = get_profile(name)
            if p.scenario == "pine_faithful":
                continue  # zero-cost is internal, no broker sources
            assert len(p.sources) >= 1, f"Profile {name} has no sources cited"
            for url in p.sources:
                assert url.startswith("http") or url.startswith("Internal"), (
                    f"Profile {name} source not a URL or 'Internal': {url!r}"
                )


class TestMakeFeeModel:
    def test_real_broker_returns_ic_markets_model(self):
        fm = make_fee_model("ic_markets_ctrader_xauusd_normal")
        assert isinstance(fm, ICMarketsMetalFeeModel)

    def test_pine_zero_returns_zero_cost_model(self):
        fm = make_fee_model("pine_zero_cost")
        assert isinstance(fm, ZeroCostFeeModel)

    def test_ctrader_gold_one_lot_at_4500(self):
        """1 lot at $4500/oz → $27 round-trip on cTrader (volume-based)."""
        fm = make_fee_model("ic_markets_ctrader_xauusd_normal")
        cost = fm.commission_usd(quantity_units=100.0, reference_price=4500.0)
        assert cost == pytest.approx(27.0)

    def test_mt4_gold_one_lot(self):
        """1 lot MT4 → $7 round-trip (fixed per lot)."""
        fm = make_fee_model("ic_markets_mt4_xauusd_normal")
        cost = fm.commission_usd(quantity_units=100.0)
        assert cost == pytest.approx(7.0)

    def test_ctrader_vs_mt4_gold_ratio(self):
        """cTrader is ~3.86× more expensive than MT4 for gold at $4500/oz."""
        ct = make_fee_model("ic_markets_ctrader_xauusd_normal")
        mt4 = make_fee_model("ic_markets_mt4_xauusd_normal")
        ct_cost = ct.commission_usd(quantity_units=100.0, reference_price=4500.0)
        mt4_cost = mt4.commission_usd(quantity_units=100.0)
        ratio = ct_cost / mt4_cost
        assert 3.5 < ratio < 4.0


class TestSelectProfile:
    def test_select_default_broker_platform(self):
        name = select_profile()
        assert name == "ic_markets_ctrader_xauusd_normal"

    def test_select_mt4_gold(self):
        name = select_profile(platform="mt4")
        assert name == "ic_markets_mt4_xauusd_normal"

    def test_select_fx_majors(self):
        name = select_profile(instrument_class="fx_majors")
        # cTrader by default
        assert name == "ic_markets_ctrader_fx_majors_normal"

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="No fee profile matches"):
            select_profile(broker="Made-Up Broker")


class TestCostAsPctOfMargin:
    def test_one_lot_gold_at_various_leverage(self):
        """The leverage interaction story — costs as % of margin."""
        # 1 lot XAUUSD @ $4500 = $450k notional, $94/trade cost
        cost = 94.0
        notional = 450_000.0
        # 1x: $94 / $450k = 0.0209%
        assert cost_as_pct_of_margin(cost, notional, 1) == pytest.approx(0.02089, rel=1e-3)
        # 100x: $94 / $4.5k = 2.089%
        assert cost_as_pct_of_margin(cost, notional, 100) == pytest.approx(2.089, rel=1e-3)
        # 1000x: $94 / $450 = 20.89%
        assert cost_as_pct_of_margin(cost, notional, 1000) == pytest.approx(20.889, rel=1e-3)

    def test_zero_notional_returns_zero(self):
        assert cost_as_pct_of_margin(100.0, 0.0, 10.0) == 0.0

    def test_invalid_leverage_raises(self):
        with pytest.raises(ValueError, match="leverage must be > 0"):
            cost_as_pct_of_margin(100.0, 100_000.0, 0)
        with pytest.raises(ValueError, match="leverage must be > 0"):
            cost_as_pct_of_margin(100.0, 100_000.0, -1)

    def test_higher_leverage_higher_cost_ratio(self):
        """Monotonic: cost-to-margin ratio strictly increases with leverage."""
        c = 50.0
        n = 100_000.0
        ratios = [cost_as_pct_of_margin(c, n, lev) for lev in (1, 5, 10, 50, 100)]
        assert all(ratios[i] < ratios[i+1] for i in range(len(ratios) - 1))


class TestFeeProfileIntegrationWithEngine:
    """End-to-end: load profile → use in LeveragedBacktestEngine."""

    def test_profile_can_be_used_with_engine(self):
        from src.backtest.leveraged_engine import LeveragedBacktestEngine
        fm = make_fee_model("ic_markets_ctrader_xauusd_normal")
        engine = LeveragedBacktestEngine(
            initial_institutional_cash=10_000.0,
            initial_aggressive_cash=0.0,
            fee_model=fm,
        )
        # Just verify construction works; we're not testing engine logic here
        assert engine._fee_model is fm
