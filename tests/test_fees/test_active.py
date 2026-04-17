"""Phase A — Active-broker pointer tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import src.fees.active as active_mod
from src.fees.active import (
    get_active_broker,
    get_active_broker_id,
    get_active_broker_for_instrument_class,
    set_active_broker,
)


REAL_CONFIG = active_mod.ACTIVE_CONFIG_PATH


@pytest.fixture
def tmp_active_config(tmp_path, monkeypatch):
    """Redirect active_broker.toml to a tmp location so tests don't touch the real file."""
    tmp_cfg = tmp_path / "active_broker.toml"
    shutil.copy(REAL_CONFIG, tmp_cfg)
    monkeypatch.setattr(active_mod, "ACTIVE_CONFIG_PATH", tmp_cfg)
    yield tmp_cfg


class TestReadDefault:
    def test_get_active_broker_id(self, tmp_active_config):
        assert get_active_broker_id() == "ic_markets_ctrader"

    def test_get_active_broker_returns_broker_instance(self, tmp_active_config):
        b = get_active_broker()
        assert b.id == "ic_markets_ctrader"
        assert b.name == "IC Markets"

    def test_instrument_class_override_falls_back_to_default(self, tmp_active_config):
        # No overrides configured → falls back to default
        b = get_active_broker_for_instrument_class("xauusd_metals")
        assert b.id == "ic_markets_ctrader"

        b = get_active_broker_for_instrument_class("fx_majors")
        assert b.id == "ic_markets_ctrader"


class TestSet:
    def test_set_default(self, tmp_active_config):
        set_active_broker("ic_markets_mt4")
        assert get_active_broker_id() == "ic_markets_mt4"
        assert get_active_broker().id == "ic_markets_mt4"

    def test_set_override_per_instrument_class(self, tmp_active_config):
        # Set an override for xauusd_metals → ic_markets_mt4
        set_active_broker("ic_markets_mt4", instrument_class="xauusd_metals")
        # Default should still be ctrader
        assert get_active_broker_id() == "ic_markets_ctrader"
        # But xauusd_metals now routes to mt4
        assert get_active_broker_for_instrument_class("xauusd_metals").id == "ic_markets_mt4"
        # While fx_majors still goes to default
        assert get_active_broker_for_instrument_class("fx_majors").id == "ic_markets_ctrader"

    def test_set_validates_broker_exists(self, tmp_active_config):
        with pytest.raises(KeyError, match="Unknown broker"):
            set_active_broker("nonexistent_broker")
        # And for an instrument_class override
        with pytest.raises(KeyError, match="Unknown broker"):
            set_active_broker("nope", instrument_class="xauusd_metals")

    def test_set_round_trips_through_file(self, tmp_active_config):
        # Write a mix of default + override
        set_active_broker("ic_markets_mt4")
        set_active_broker("pine_zero_cost", instrument_class="research")
        # Re-read from disk
        assert get_active_broker_id() == "ic_markets_mt4"
        # Load broker for overridden class — note: config/brokers/ must have pine_zero_cost
        # (which the migration script produces)
        b = get_active_broker_for_instrument_class("research")
        assert b.id == "pine_zero_cost"


class TestDefaultBehaviorWhenFileMissing:
    def test_returns_sensible_default(self, tmp_path, monkeypatch):
        """If the file doesn't exist, the reader picks ic_markets_ctrader as the default."""
        monkeypatch.setattr(active_mod, "ACTIVE_CONFIG_PATH", tmp_path / "doesnt_exist.toml")
        assert get_active_broker_id() == "ic_markets_ctrader"
