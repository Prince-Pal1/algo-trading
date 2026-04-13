"""M3S mode definitions — 4 modes with per-mode compounding/allocation dials.

Modes are `CONSERVATIVE`, `STANDARD`, `GROWTH`, `CUSTOM`. See the authoritative
spec in docs/planning/m3s_plan_v1.md § v1.1 ADDENDUM (Session 22).

Key invariants (the 5 safety rails for CUSTOM from ADDENDUM §A4):

1. CUSTOM refuses to load unless `i_accept_custom_mode_risk=True` (literal).
2. `compound_every_n_trades >= 1`.
3. `auto_demote_dd_threshold < dd_freeze_threshold` — auto-demote must fire
   before freeze, otherwise it never fires.
4. `kelly_fraction <= 1.0` (values > 0.75 log a WARN).
5. `compound_pace_ceiling >= compound_pace_floor`.

Additionally, `compound_hwm_gate` is always forced to True (variance-drag
math applies to every mode; user cannot turn it off even in CUSTOM — the
loader logs a WARN and overrides).

Presets for CONSERVATIVE/STANDARD/GROWTH are the full-config immutable
defaults. CUSTOM has no preset — it must be loaded from a user-provided dict
(in production, from config/m3s.toml's [m3s.modes.CUSTOM] block).
"""

from __future__ import annotations

import enum

import msgspec

from src.utils.logger import get_logger

log = get_logger("m3s.modes")


class M3SMode(str, enum.Enum):
    CONSERVATIVE = "CONSERVATIVE"
    STANDARD = "STANDARD"
    GROWTH = "GROWTH"
    CUSTOM = "CUSTOM"


class CustomModeValidationError(ValueError):
    """Raised when CUSTOM mode config violates one of the 5 safety rails."""


class ModeConfig(msgspec.Struct, frozen=True):
    """Immutable per-mode configuration.

    Every mode uses the same shape — CUSTOM just differs in that each field
    is user-supplied instead of hard-coded. This lets allocator/compounder
    treat all modes uniformly.
    """

    # Identity
    name: M3SMode

    # ── Capital allocation dials ─────────────────────────────────────────
    vol_target_annual: float
    kelly_fraction: float
    max_per_strategy_cap: float
    max_cluster_cap: float
    allocator_method: str                 # "inverse_vol" | "hrp_lite"
    cold_start_trades_required: int       # equal-weight until this many trades/strategy

    # ── Drawdown safety rails ────────────────────────────────────────────
    dd_freeze_threshold: float            # above this → scalar *= 0.5
    dd_halt_threshold: float              # above this → scalar = 0.0

    # ── Compounding dials ────────────────────────────────────────────────
    compound_cadence: str                 # "per_trade" | "daily" | "weekly"
    compound_every_n_trades: int          # only used if cadence=="per_trade"
    compound_hwm_gate: bool               # always True (force-set in loader)
    compound_rolling_sharpe_window: int   # trades, for pace dial
    compound_pace_floor: float
    compound_pace_ceiling: float

    # ── Auto-demote rule ─────────────────────────────────────────────────
    auto_demote_enabled: bool
    auto_demote_dd_threshold: float
    auto_demote_target_mode: str          # "" means "no demote target"
    auto_demote_cooldown_hours: int
    auto_promote_back_threshold: float    # DD must recover below this to re-promote


# ── Hard-coded presets (CONSERVATIVE/STANDARD/GROWTH) ────────────────────


MODE_PRESETS: dict[M3SMode, ModeConfig] = {
    M3SMode.CONSERVATIVE: ModeConfig(
        name=M3SMode.CONSERVATIVE,
        vol_target_annual=0.10,
        kelly_fraction=0.25,
        max_per_strategy_cap=0.25,
        max_cluster_cap=0.40,
        allocator_method="inverse_vol",
        cold_start_trades_required=20,
        dd_freeze_threshold=0.05,
        dd_halt_threshold=0.10,
        compound_cadence="weekly",
        compound_every_n_trades=1,
        compound_hwm_gate=True,
        compound_rolling_sharpe_window=30,
        compound_pace_floor=0.20,
        compound_pace_ceiling=0.60,
        auto_demote_enabled=False,          # CONSERVATIVE is the bottom mode
        auto_demote_dd_threshold=0.05,
        auto_demote_target_mode="",
        auto_demote_cooldown_hours=48,
        auto_promote_back_threshold=0.02,
    ),
    M3SMode.STANDARD: ModeConfig(
        name=M3SMode.STANDARD,
        vol_target_annual=0.15,
        kelly_fraction=0.50,
        max_per_strategy_cap=0.40,
        max_cluster_cap=0.65,
        allocator_method="hrp_lite",
        cold_start_trades_required=30,
        dd_freeze_threshold=0.08,
        dd_halt_threshold=0.12,
        compound_cadence="daily",
        compound_every_n_trades=1,
        compound_hwm_gate=True,
        compound_rolling_sharpe_window=30,
        compound_pace_floor=0.25,
        compound_pace_ceiling=1.00,
        auto_demote_enabled=True,
        auto_demote_dd_threshold=0.08,
        auto_demote_target_mode="CONSERVATIVE",
        auto_demote_cooldown_hours=48,
        auto_promote_back_threshold=0.03,
    ),
    M3SMode.GROWTH: ModeConfig(
        name=M3SMode.GROWTH,
        vol_target_annual=0.22,
        kelly_fraction=0.50,
        max_per_strategy_cap=0.50,
        max_cluster_cap=0.75,
        allocator_method="hrp_lite",
        cold_start_trades_required=30,
        dd_freeze_threshold=0.10,
        dd_halt_threshold=0.15,
        compound_cadence="daily",
        compound_every_n_trades=1,
        compound_hwm_gate=True,
        compound_rolling_sharpe_window=30,
        compound_pace_floor=0.30,
        compound_pace_ceiling=1.20,
        auto_demote_enabled=True,
        auto_demote_dd_threshold=0.10,
        auto_demote_target_mode="STANDARD",
        auto_demote_cooldown_hours=48,
        auto_promote_back_threshold=0.04,
    ),
}


# ── CUSTOM defaults (applied when a field is missing from the user dict) ──


_CUSTOM_DEFAULTS: dict[str, object] = {
    "vol_target_annual": 0.20,
    "kelly_fraction": 0.50,
    "max_per_strategy_cap": 0.50,
    "max_cluster_cap": 0.70,
    "allocator_method": "hrp_lite",
    "cold_start_trades_required": 30,
    "dd_freeze_threshold": 0.10,
    "dd_halt_threshold": 0.15,
    "compound_cadence": "daily",
    "compound_every_n_trades": 1,
    "compound_hwm_gate": True,
    "compound_rolling_sharpe_window": 30,
    "compound_pace_floor": 0.25,
    "compound_pace_ceiling": 1.00,
    "auto_demote_enabled": True,
    # Must be strictly below dd_freeze_threshold (rail §A4.3) — otherwise the
    # demote trigger fires at-or-after the freeze and never activates.
    "auto_demote_dd_threshold": 0.08,
    "auto_demote_target_mode": "STANDARD",
    "auto_demote_cooldown_hours": 48,
    "auto_promote_back_threshold": 0.03,
}

_ALLOWED_CADENCES = {"per_trade", "daily", "weekly"}
_KELLY_WARN_THRESHOLD = 0.75


# ── Public loader ────────────────────────────────────────────────────────


def load_mode_from_dict(
    mode_name: str,
    user_overrides: dict | None = None,
) -> ModeConfig:
    """Load a ModeConfig by name, optionally applying user overrides.

    For CONSERVATIVE/STANDARD/GROWTH: overrides are applied on top of the
    preset (any field may be overridden; the result is re-validated).

    For CUSTOM: overrides must include `i_accept_custom_mode_risk=True`,
    and the result is validated against all 5 safety rails in §A4. A
    missing CUSTOM field falls back to _CUSTOM_DEFAULTS.

    Raises CustomModeValidationError on rail violations.
    """
    try:
        mode = M3SMode(mode_name)
    except ValueError as e:
        raise CustomModeValidationError(f"unknown mode: {mode_name}") from e

    overrides = dict(user_overrides or {})

    if mode == M3SMode.CUSTOM:
        return _load_custom(overrides)

    # Preset + optional overrides path
    preset = MODE_PRESETS[mode]
    if not overrides:
        return preset

    # msgspec.structs.replace() — apply only known fields, ignore extras.
    fields = {f for f in preset.__struct_fields__ if f != "name"}
    filtered = {k: v for k, v in overrides.items() if k in fields}
    merged = msgspec.structs.replace(preset, **filtered)

    # Force HWM gate on regardless of override (rail #5).
    if not merged.compound_hwm_gate:
        log.warning(
            "m3s.mode.hwm_gate_force_enabled",
            mode=mode.value,
            reason="variance-drag-protection",
        )
        merged = msgspec.structs.replace(merged, compound_hwm_gate=True)

    _validate_common(merged)
    return merged


def _load_custom(overrides: dict) -> ModeConfig:
    """Build and validate a CUSTOM ModeConfig from a user dict.

    Safety rails (ADDENDUM §A4):
      1. i_accept_custom_mode_risk must be literally True
      2. compound_every_n_trades >= 1
      3. auto_demote_dd_threshold < dd_freeze_threshold
      4. kelly_fraction <= 1.0 (> 0.75 → WARN, not error)
      5. compound_pace_ceiling >= compound_pace_floor
      + hwm_gate is forced to True with a WARN if the user tried to disable it.
    """
    if overrides.get("i_accept_custom_mode_risk") is not True:
        raise CustomModeValidationError(
            "CUSTOM mode refused: i_accept_custom_mode_risk must be literally True. "
            "This mode bypasses mode-level caps — do not use unless you understand "
            "the risk."
        )

    # Merge defaults under overrides (overrides win).
    merged_raw: dict = {**_CUSTOM_DEFAULTS, **{
        k: v for k, v in overrides.items() if k in _CUSTOM_DEFAULTS
    }}

    # Force HWM gate ON regardless of user value (rail #5 / ADDENDUM A3).
    if overrides.get("compound_hwm_gate") is False:
        log.warning(
            "m3s.mode.custom.hwm_gate_force_enabled",
            reason="variance-drag-protection",
            user_value=False,
        )
    merged_raw["compound_hwm_gate"] = True

    cfg = ModeConfig(name=M3SMode.CUSTOM, **merged_raw)
    _validate_common(cfg)
    _validate_custom_extra(cfg)
    return cfg


def _validate_common(cfg: ModeConfig) -> None:
    """Rails that apply to every mode (preset or CUSTOM)."""
    if cfg.compound_cadence not in _ALLOWED_CADENCES:
        raise CustomModeValidationError(
            f"compound_cadence must be one of {sorted(_ALLOWED_CADENCES)}, "
            f"got {cfg.compound_cadence!r}"
        )
    if cfg.compound_every_n_trades < 1:
        raise CustomModeValidationError(
            f"compound_every_n_trades must be >= 1, got {cfg.compound_every_n_trades}"
        )
    if cfg.compound_pace_ceiling < cfg.compound_pace_floor:
        raise CustomModeValidationError(
            f"compound_pace_ceiling ({cfg.compound_pace_ceiling}) must be >= "
            f"compound_pace_floor ({cfg.compound_pace_floor})"
        )
    if cfg.kelly_fraction <= 0.0 or cfg.kelly_fraction > 1.0:
        raise CustomModeValidationError(
            f"kelly_fraction must be in (0.0, 1.0], got {cfg.kelly_fraction}"
        )
    if cfg.kelly_fraction > _KELLY_WARN_THRESHOLD:
        log.warning(
            "m3s.mode.kelly_high",
            mode=cfg.name.value,
            kelly_fraction=cfg.kelly_fraction,
            warn_threshold=_KELLY_WARN_THRESHOLD,
        )
    if cfg.dd_halt_threshold <= cfg.dd_freeze_threshold:
        raise CustomModeValidationError(
            f"dd_halt_threshold ({cfg.dd_halt_threshold}) must be > "
            f"dd_freeze_threshold ({cfg.dd_freeze_threshold})"
        )


def _validate_custom_extra(cfg: ModeConfig) -> None:
    """Additional rails that apply only to CUSTOM."""
    if cfg.auto_demote_enabled:
        if cfg.auto_demote_dd_threshold >= cfg.dd_freeze_threshold:
            raise CustomModeValidationError(
                f"auto_demote_dd_threshold ({cfg.auto_demote_dd_threshold}) must be "
                f"< dd_freeze_threshold ({cfg.dd_freeze_threshold}); otherwise "
                f"auto-demote will never fire before the freeze kicks in."
            )
        if not cfg.auto_demote_target_mode:
            raise CustomModeValidationError(
                "auto_demote_target_mode cannot be empty when auto_demote_enabled=True"
            )
        try:
            target = M3SMode(cfg.auto_demote_target_mode)
        except ValueError as e:
            raise CustomModeValidationError(
                f"auto_demote_target_mode {cfg.auto_demote_target_mode!r} is not a "
                f"known mode"
            ) from e
        if target == M3SMode.CUSTOM:
            raise CustomModeValidationError(
                "auto_demote_target_mode cannot be CUSTOM (would cause a loop)"
            )
