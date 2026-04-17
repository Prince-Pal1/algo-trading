"""Phase B — FeeManager entry point tests.

Covers:
- resolve() picks the right FeeModel for active broker × symbol × scenario
- resolve() auto-detects scenario from timestamp
- resolve_profile() returns the FeeProfile (inspection)
- project_cost() produces correct decomposition (spread + commission + swap)
- project_cost() respects style (scalping/intraday don't accrue swap)
- explain() returns the introspection dict
- Broker override via broker_id kwarg
- Symbol-not-recognized raises
- pine fallback when no match
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.backtest.costs import ICMarketsMetalFeeModel, ZeroCostFeeModel
from src.fees import FeeManager, reload_brokers
from src.fees.scenario import reset_news_cache


@pytest.fixture(autouse=True)
def _reload():
    reload_brokers()
    reset_news_cache()
    yield


def _iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


class TestResolve:
    def test_resolves_xauusd_to_ctrader_metal_model(self):
        fm = FeeManager.resolve(symbol="XAUUSD", style="swing")
        assert isinstance(fm, ICMarketsMetalFeeModel)

    def test_resolves_with_forced_scenario(self):
        fm_normal = FeeManager.resolve(symbol="XAUUSD", scenario="normal")
        fm_illiquid = FeeManager.resolve(symbol="XAUUSD", scenario="illiquid")
        # The FeeModel doesn't expose its config readably from here, but the
        # profile lookup should differ — verify via resolve_profile
        p_normal = FeeManager.resolve_profile(symbol="XAUUSD", scenario="normal")
        p_illiquid = FeeManager.resolve_profile(symbol="XAUUSD", scenario="illiquid")
        assert p_illiquid.spread_config.base_spread_pips > p_normal.spread_config.base_spread_pips

    def test_resolves_auto_scenario_from_timestamp(self):
        # 03:00 UTC → illiquid for XAUUSD
        ts = _iso("2026-04-17T03:00:00Z")
        p = FeeManager.resolve_profile(symbol="XAUUSD", timestamp_ms=ts)
        assert p.scenario == "illiquid"

    def test_resolves_auto_normal_at_overlap(self):
        ts = _iso("2026-04-17T14:00:00Z")  # overlap
        p = FeeManager.resolve_profile(symbol="XAUUSD", timestamp_ms=ts)
        assert p.scenario == "normal"

    def test_broker_override_via_kwarg(self):
        p = FeeManager.resolve_profile(
            symbol="XAUUSD", scenario="normal", broker_id="ic_markets_mt4"
        )
        assert p.broker == "IC Markets"
        # MT4 uses fixed_per_lot commission, not volume_based
        assert p.commission_schedule.per_lot_per_side_usd is not None

    def test_unrecognized_symbol_raises(self):
        with pytest.raises(ValueError, match="not recognized"):
            FeeManager.resolve(symbol="MARTIAN_FUTURES")

    def test_pine_baseline_via_broker_id(self):
        fm = FeeManager.resolve(
            symbol="XAUUSD",  # any symbol — pine profile is instrument-agnostic
            scenario="pine_faithful",
            broker_id="pine_zero_cost",
        )
        assert isinstance(fm, ZeroCostFeeModel)


class TestProjectCost:
    def test_xauusd_swing_cost_breakdown(self):
        # At ~$4865 mid, 1 lot, 48h hold (2 nights) → expect:
        #   spread (rt) = 0.6 pips × $0.10 × 100 oz × 1 × 2 = $12
        #   commission (rt) = ($486,500 / $100k) × $3 × 2 = $29.19
        #   swap long = -58.14 pips × $0.10 × 100 oz × 2 nights = -$1,162.80
        # Total ≈ -$1,121.61 (mostly swap)
        cp = FeeManager.project_cost(
            symbol="XAUUSD",
            qty_lots=1.0,
            style="swing",
            hold_hours=48.0,
            mid_price=4865.0,
            scenario="normal",
            side="long",
        )
        assert cp.broker_id == "ic_markets_ctrader"
        assert cp.scenario == "normal"
        # Spread: 0.30+0.30 = 0.60 pips; pip_size 0.10; contract 100
        assert cp.spread == pytest.approx(12.0, rel=1e-3)
        # Commission: (4865 * 100 / 100_000) * 3 * 2
        assert cp.commission == pytest.approx(29.19, rel=1e-3)
        # Swap: 2 nights × -58.14 × 0.10 × 100 = -1162.80
        assert cp.swap == pytest.approx(-1162.80, rel=1e-3)
        assert cp.total == pytest.approx(12.0 + 29.19 + -1162.80, rel=1e-3)

    def test_scalping_does_not_accrue_swap(self):
        cp = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="scalping",
            mid_price=4865.0, scenario="normal",
        )
        assert cp.swap == 0.0
        assert "scalping" in " ".join(cp.notes)

    def test_intraday_does_not_accrue_swap(self):
        cp = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="intraday",
            mid_price=4865.0, scenario="normal",
        )
        assert cp.swap == 0.0

    def test_position_style_uses_default_hold_and_accrues_swap(self):
        # Position default hold = 480h = 20 days → 20 swap nights
        cp = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="position",
            mid_price=4865.0, scenario="normal", side="long",
        )
        assert cp.swap != 0.0
        # 20 nights × -58.14 × 0.10 × 100 = -11628
        assert cp.swap == pytest.approx(-11628.0, rel=1e-3)

    def test_short_side_uses_short_swap_rate(self):
        cp_long = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="swing",
            hold_hours=48.0, mid_price=4865.0, scenario="normal", side="long",
        )
        cp_short = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="swing",
            hold_hours=48.0, mid_price=4865.0, scenario="normal", side="short",
        )
        # Long swap is negative (cost), short is positive (credit)
        assert cp_long.swap < 0
        assert cp_short.swap > 0

    def test_scale_with_qty(self):
        cp1 = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="scalping",
            mid_price=4865.0, scenario="normal",
        )
        cp5 = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=5.0, style="scalping",
            mid_price=4865.0, scenario="normal",
        )
        assert cp5.spread == pytest.approx(cp1.spread * 5, rel=1e-6)
        assert cp5.commission == pytest.approx(cp1.commission * 5, rel=1e-6)

    def test_illiquid_scenario_is_more_expensive(self):
        cp_normal = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="intraday",
            mid_price=4865.0, scenario="normal",
        )
        cp_illiquid = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="intraday",
            mid_price=4865.0, scenario="illiquid",
        )
        # Illiquid = normal × 3.5 on spread
        assert cp_illiquid.spread == pytest.approx(cp_normal.spread * 3.5, rel=1e-3)
        # Commission unchanged
        assert cp_illiquid.commission == pytest.approx(cp_normal.commission, rel=1e-6)

    def test_mid_price_required(self):
        with pytest.raises(ValueError, match="mid_price"):
            FeeManager.project_cost(
                symbol="XAUUSD", qty_lots=1.0, style="intraday",
            )

    def test_as_pct_of_notional(self):
        cp = FeeManager.project_cost(
            symbol="XAUUSD", qty_lots=1.0, style="scalping",
            mid_price=4865.0, scenario="normal",
        )
        notional = 4865.0 * 100  # 486,500
        pct = cp.as_pct_of_notional(notional)
        assert 0 < pct < 0.001  # scalping costs are small fraction of notional


class TestExplain:
    def test_explain_returns_full_context(self):
        info = FeeManager.explain(symbol="XAUUSD", style="swing")
        assert info["symbol"] == "XAUUSD"
        assert info["style"] == "swing"
        assert info["broker_id"] == "ic_markets_ctrader"
        assert info["instrument_class"] == "xauusd_metals"
        assert info["scenario"] == "normal"  # no timestamp → defaults to normal
        assert info["max_leverage"] == 500.0
        assert info["incurs_swap"] is True
        assert info["default_hold_hours"] == 48.0

    def test_explain_detects_illiquid_from_timestamp(self):
        ts = _iso("2026-04-17T03:00:00Z")
        info = FeeManager.explain(symbol="XAUUSD", timestamp_ms=ts)
        assert info["scenario"] == "illiquid"

    def test_explain_scalping_no_swap(self):
        info = FeeManager.explain(symbol="XAUUSD", style="scalping")
        assert info["incurs_swap"] is False
        assert info["default_hold_hours"] == 0.25
