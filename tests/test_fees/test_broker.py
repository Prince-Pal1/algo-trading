"""Phase A — Broker registry loader tests.

Covers:
- load_all_brokers() discovers every config/brokers/*.toml
- Broker metadata round-trips (name, regulator, leverage caps)
- BrokerInstrumentProfile exposes scenario + contract spec correctly
- resolve_profile() returns the right FeeProfile and falls back to normal
- Symbol routing (instrument_class_for_symbol, supports_symbol)
- All scenarios the spread sampler + fee manager will need are present
"""

from __future__ import annotations

import pytest

from src.fees.broker import (
    Broker,
    BrokerInstrumentProfile,
    get_broker,
    list_brokers,
    load_all_brokers,
    reload_brokers,
)


@pytest.fixture(autouse=True)
def _reload_registry():
    # Ensure each test sees a freshly-loaded registry.
    reload_brokers()
    yield
    reload_brokers()


class TestRegistryLoading:
    def test_discovers_per_broker_tomls(self):
        brokers = load_all_brokers()
        assert "ic_markets_ctrader" in brokers
        assert "ic_markets_mt4" in brokers
        assert "pine_zero_cost" in brokers

    def test_list_brokers_sorted(self):
        names = list_brokers()
        assert names == sorted(names)
        assert len(names) >= 3

    def test_get_broker_returns_instance(self):
        b = get_broker("ic_markets_ctrader")
        assert isinstance(b, Broker)
        assert b.id == "ic_markets_ctrader"
        assert b.name == "IC Markets"
        assert b.platform == "ctrader"

    def test_unknown_broker_raises(self):
        with pytest.raises(KeyError, match="Unknown broker"):
            get_broker("definitely_not_a_broker")


class TestBrokerMetadata:
    def test_icmarkets_ctrader_metadata(self):
        b = get_broker("ic_markets_ctrader")
        assert b.regulator == "FSA Seychelles"
        assert b.country == "Seychelles"
        assert b.api_type == "ctrader_open_api"
        assert b.min_deposit_usd == 200.0
        assert "USD" in b.base_currencies
        assert b.max_leverage_for("xauusd_metals") == 500.0
        assert b.max_leverage_for("fx_majors") == 500.0

    def test_pine_zero_is_special_baseline(self):
        b = get_broker("pine_zero_cost")
        assert b.regulator == "(n/a)"
        assert b.min_deposit_usd == 0.0
        assert b.leverage_max == {}

    def test_leverage_max_missing_class_returns_1x(self):
        b = get_broker("ic_markets_ctrader")
        assert b.max_leverage_for("unknown_class") == 1.0


class TestInstrumentProfiles:
    def test_xauusd_scenarios_include_normal_news_illiquid_volatile(self):
        b = get_broker("ic_markets_ctrader")
        ip = b.resolve_instrument(instrument_class="xauusd_metals")
        assert ip is not None
        scenarios = set(ip.scenarios_available())
        # These four are the ones the FeeManager's auto-scenario detector
        # will pick between; the migration derives illiquid + volatile when
        # they aren't already defined.
        assert {"normal", "news_active", "illiquid", "volatile"}.issubset(scenarios)

    def test_contract_size_matches_instrument_class(self):
        b = get_broker("ic_markets_ctrader")
        gold = b.resolve_instrument(instrument_class="xauusd_metals")
        fx = b.resolve_instrument(instrument_class="fx_majors")
        assert gold.contract_size == 100.0  # 100 oz per lot
        assert fx.contract_size == 100_000.0  # 100k per standard lot

    def test_min_lot_is_not_zero(self):
        b = get_broker("ic_markets_ctrader")
        for ic in b.instrument_classes():
            ip = b.resolve_instrument(instrument_class=ic)
            assert ip.min_lot > 0
            assert ip.max_lot >= ip.min_lot


class TestProfileResolution:
    def test_resolve_normal_returns_profile(self):
        b = get_broker("ic_markets_ctrader")
        p = b.resolve_profile(instrument_class="xauusd_metals", scenario="normal")
        assert p is not None
        assert p.spread_config.base_spread_pips == 0.30
        assert p.commission_schedule.per_100k_notional_usd == 3.0

    def test_resolve_illiquid_is_wider_than_normal(self):
        b = get_broker("ic_markets_ctrader")
        normal = b.resolve_profile(instrument_class="xauusd_metals", scenario="normal")
        illiquid = b.resolve_profile(instrument_class="xauusd_metals", scenario="illiquid")
        assert illiquid.spread_config.base_spread_pips > normal.spread_config.base_spread_pips
        # Migration multiplier is 3.5× normal.
        assert illiquid.spread_config.base_spread_pips == pytest.approx(normal.spread_config.base_spread_pips * 3.5)

    def test_resolve_volatile_is_wider_than_normal_but_less_than_illiquid(self):
        b = get_broker("ic_markets_ctrader")
        normal = b.resolve_profile(instrument_class="xauusd_metals", scenario="normal")
        volatile = b.resolve_profile(instrument_class="xauusd_metals", scenario="volatile")
        illiquid = b.resolve_profile(instrument_class="xauusd_metals", scenario="illiquid")
        assert volatile.spread_config.base_spread_pips > normal.spread_config.base_spread_pips
        assert volatile.spread_config.base_spread_pips < illiquid.spread_config.base_spread_pips

    def test_unknown_scenario_falls_back_to_normal(self):
        b = get_broker("ic_markets_ctrader")
        p = b.resolve_profile(instrument_class="xauusd_metals", scenario="does_not_exist")
        assert p is not None
        assert p.scenario == "normal"

    def test_unknown_instrument_class_returns_none(self):
        b = get_broker("ic_markets_ctrader")
        p = b.resolve_profile(instrument_class="martian_futures", scenario="normal")
        assert p is None

    def test_pine_zero_cost_resolves_pine_faithful(self):
        b = get_broker("pine_zero_cost")
        p = b.resolve_profile(instrument_class="any", scenario="pine_faithful")
        assert p is not None
        assert p.spread_config.base_spread_pips == 0.0
        # Fallback to 'normal' would miss here (pine_zero has no normal),
        # so the resolver should still find pine_faithful when asked by name.

    def test_pine_zero_cost_unknown_scenario_returns_none_not_wrong_fallback(self):
        # pine_zero_cost has ONLY pine_faithful — no 'normal' fallback.
        b = get_broker("pine_zero_cost")
        p = b.resolve_profile(instrument_class="any", scenario="normal")
        # Falls back to 'normal' key, which doesn't exist → None
        assert p is None


class TestSymbolRouting:
    def test_xauusd_routes_to_gold(self):
        b = get_broker("ic_markets_ctrader")
        assert b.instrument_class_for_symbol("XAUUSD") == "xauusd_metals"
        assert b.instrument_class_for_symbol("xauusd") == "xauusd_metals"

    def test_eurusd_routes_to_fx_majors(self):
        b = get_broker("ic_markets_ctrader")
        assert b.instrument_class_for_symbol("EURUSD") == "fx_majors"

    def test_unknown_symbol_returns_none(self):
        b = get_broker("ic_markets_ctrader")
        assert b.instrument_class_for_symbol("BTCUSDT") is None

    def test_supports_symbol(self):
        b = get_broker("ic_markets_ctrader")
        assert b.supports_symbol("XAUUSD") is True
        assert b.supports_symbol("GBPUSD") is True
        assert b.supports_symbol("BTCUSDT") is False
