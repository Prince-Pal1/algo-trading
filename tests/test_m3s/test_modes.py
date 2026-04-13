"""Sub-phase 0.1 — tests for src/m3s/modes.py preset loading and CUSTOM rails."""

from __future__ import annotations

import pytest

from src.m3s.modes import (
    MODE_PRESETS,
    CustomModeValidationError,
    M3SMode,
    ModeConfig,
    load_mode_from_dict,
)


class TestPresets:
    def test_all_four_modes_exist(self):
        # CUSTOM is NOT in MODE_PRESETS (it requires user input), but load_mode_from_dict
        # must still recognize it. The three fixed modes have presets.
        assert M3SMode.CONSERVATIVE in MODE_PRESETS
        assert M3SMode.STANDARD in MODE_PRESETS
        assert M3SMode.GROWTH in MODE_PRESETS
        assert M3SMode.CUSTOM not in MODE_PRESETS

    def test_conservative_clamps_match_spec(self):
        cfg = MODE_PRESETS[M3SMode.CONSERVATIVE]
        assert cfg.vol_target_annual == 0.10
        assert cfg.kelly_fraction == 0.25
        assert cfg.compound_cadence == "weekly"
        assert cfg.compound_pace_floor == 0.20
        assert cfg.compound_pace_ceiling == 0.60
        assert cfg.compound_hwm_gate is True
        assert cfg.auto_demote_enabled is False  # bottom mode
        assert cfg.allocator_method == "inverse_vol"

    def test_growth_clamps_match_spec(self):
        cfg = MODE_PRESETS[M3SMode.GROWTH]
        assert cfg.vol_target_annual == 0.22
        assert cfg.kelly_fraction == 0.50
        assert cfg.compound_cadence == "daily"
        assert cfg.compound_pace_floor == 0.30
        assert cfg.compound_pace_ceiling == 1.20
        assert cfg.compound_hwm_gate is True
        assert cfg.auto_demote_enabled is True
        assert cfg.auto_demote_target_mode == "STANDARD"
        assert cfg.allocator_method == "hrp_lite"

    def test_load_preset_by_name_returns_preset(self):
        cfg = load_mode_from_dict("STANDARD")
        assert isinstance(cfg, ModeConfig)
        assert cfg.name == M3SMode.STANDARD
        assert cfg == MODE_PRESETS[M3SMode.STANDARD]


class TestCustomSafetyRails:
    def _base_custom(self, **overrides):
        """Minimal valid CUSTOM override dict — individual tests corrupt one field."""
        base = {
            "i_accept_custom_mode_risk": True,
        }
        base.update(overrides)
        return base

    def test_custom_refuses_without_opt_in(self):
        with pytest.raises(CustomModeValidationError, match="i_accept_custom_mode_risk"):
            load_mode_from_dict("CUSTOM", {"i_accept_custom_mode_risk": False})

        with pytest.raises(CustomModeValidationError, match="i_accept_custom_mode_risk"):
            load_mode_from_dict("CUSTOM", {})

    def test_custom_loads_with_opt_in(self):
        cfg = load_mode_from_dict("CUSTOM", self._base_custom())
        assert cfg.name == M3SMode.CUSTOM
        # Defaults filled in
        assert cfg.kelly_fraction == 0.50
        assert cfg.compound_hwm_gate is True

    def test_custom_force_enables_hwm_gate(self, caplog):
        cfg = load_mode_from_dict(
            "CUSTOM",
            self._base_custom(compound_hwm_gate=False),
        )
        # Override ignored — HWM gate is locked on
        assert cfg.compound_hwm_gate is True

    def test_custom_rejects_compound_every_n_below_one(self):
        with pytest.raises(CustomModeValidationError, match="compound_every_n_trades"):
            load_mode_from_dict(
                "CUSTOM",
                self._base_custom(compound_every_n_trades=0),
            )

    def test_custom_rejects_auto_demote_at_or_above_freeze(self):
        with pytest.raises(CustomModeValidationError, match="auto_demote_dd_threshold"):
            load_mode_from_dict(
                "CUSTOM",
                self._base_custom(
                    dd_freeze_threshold=0.10,
                    auto_demote_dd_threshold=0.10,  # equal is also rejected
                    auto_demote_enabled=True,
                    auto_demote_target_mode="STANDARD",
                ),
            )

    def test_custom_rejects_pace_ceiling_below_floor(self):
        with pytest.raises(CustomModeValidationError, match="compound_pace_ceiling"):
            load_mode_from_dict(
                "CUSTOM",
                self._base_custom(
                    compound_pace_floor=0.8,
                    compound_pace_ceiling=0.5,
                ),
            )

    def test_custom_rejects_kelly_above_one(self):
        with pytest.raises(CustomModeValidationError, match="kelly_fraction"):
            load_mode_from_dict(
                "CUSTOM",
                self._base_custom(kelly_fraction=1.5),
            )

    def test_custom_rejects_auto_demote_target_custom(self):
        with pytest.raises(CustomModeValidationError, match="cannot be CUSTOM"):
            load_mode_from_dict(
                "CUSTOM",
                self._base_custom(
                    auto_demote_target_mode="CUSTOM",
                    auto_demote_enabled=True,
                ),
            )

    def test_custom_rejects_halt_at_or_below_freeze(self):
        with pytest.raises(CustomModeValidationError, match="dd_halt_threshold"):
            load_mode_from_dict(
                "CUSTOM",
                self._base_custom(
                    dd_freeze_threshold=0.12,
                    dd_halt_threshold=0.10,
                    auto_demote_dd_threshold=0.05,
                ),
            )
