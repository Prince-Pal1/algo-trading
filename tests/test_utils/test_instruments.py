"""Tests for the instrument metadata registry (Phase G.0b of gold plan)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.utils.instruments import (
    Instrument,
    get_instrument,
    is_market_open,
    list_instruments,
    register_instrument,
)


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class TestRegistry:
    def test_btcusdt_is_crypto_default(self):
        inst = get_instrument("BTCUSDT")
        assert inst.asset_class == "crypto"
        assert inst.is_24_7 is True
        assert inst.is_24_5 is False
        assert inst.contract_size == 1.0  # backward compat — quantity = BTC amount

    def test_xauusd_is_metal(self):
        inst = get_instrument("XAUUSD")
        assert inst.asset_class == "metal"
        assert inst.is_24_5 is True
        assert inst.is_24_7 is False
        assert inst.contract_size == 100.0  # 1 standard lot = 100 oz
        assert inst.pip_value_per_std_lot_usd == 10.0
        assert inst.default_leverage_cap == 500.0

    def test_unknown_symbol_falls_back_to_crypto(self):
        inst = get_instrument("DOGEUSDT_FAKE")
        assert inst.asset_class == "crypto"
        assert inst.contract_size == 1.0
        assert inst.symbol == "DOGEUSDT_FAKE"

    def test_list_instruments_returns_sorted(self):
        all_inst = list_instruments()
        syms = [i.symbol for i in all_inst]
        assert syms == sorted(syms)
        assert "BTCUSDT" in syms
        assert "XAUUSD" in syms

    def test_register_instrument_overrides(self):
        custom = Instrument(
            symbol="FAKEUSDT",
            asset_class="crypto",
            quote_currency="USDT",
            tick_size=0.001,
            contract_size=1.0,
            pip_value_per_std_lot_usd=0.001,
            min_lot=0.01,
            max_lot=100.0,
            is_24_7=True,
            is_24_5=False,
            default_leverage_cap=1.0,
        )
        register_instrument(custom)
        assert get_instrument("FAKEUSDT").tick_size == 0.001


class TestMarketHours:
    def test_crypto_always_open(self):
        # Sunday 3 AM UTC — crypto should be open
        assert is_market_open("BTCUSDT", _ts(2026, 4, 12, hour=3)) is True
        # Saturday — crypto should be open
        assert is_market_open("BTCUSDT", _ts(2026, 4, 11, hour=14)) is True

    def test_gold_closed_saturday(self):
        # Saturday 12:00 UTC — gold market closed
        assert is_market_open("XAUUSD", _ts(2026, 4, 11, hour=12)) is False

    def test_gold_closed_friday_evening(self):
        # Friday 23:00 UTC — after weekend close (22:00), closed
        assert is_market_open("XAUUSD", _ts(2026, 4, 10, hour=23)) is False

    def test_gold_open_friday_afternoon(self):
        # Friday 14:00 UTC — London/NY overlap, open
        assert is_market_open("XAUUSD", _ts(2026, 4, 10, hour=14)) is True

    def test_gold_closed_sunday_morning(self):
        # Sunday 10:00 UTC — before weekend open (22:00), closed
        assert is_market_open("XAUUSD", _ts(2026, 4, 12, hour=10)) is False

    def test_gold_open_sunday_evening(self):
        # Sunday 23:00 UTC — after weekend open (22:00), open
        assert is_market_open("XAUUSD", _ts(2026, 4, 12, hour=23)) is True

    def test_gold_open_mid_week(self):
        # Wednesday 12:00 UTC — London session, obviously open
        assert is_market_open("XAUUSD", _ts(2026, 4, 8, hour=12)) is True


class TestInstrumentFieldsImmutable:
    def test_frozen_struct(self):
        inst = get_instrument("BTCUSDT")
        with pytest.raises(AttributeError):
            inst.tick_size = 999.0  # msgspec frozen structs reject writes
