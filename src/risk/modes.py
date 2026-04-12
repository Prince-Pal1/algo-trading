"""Operating modes — system-wide risk/return dial.

Four modes shift ALL risk thresholds via multipliers on base RiskConfig:
  AGGRESSIVE (default): Profit-first, wide limits, gentle drawdown scaling
  BALANCED:             Optimize Sharpe, standard institutional thresholds
  DEFENSIVE:            Capital preservation, tight limits, harsh scaling
  CUSTOM:               User overrides individual multipliers

NON-NEGOTIABLE fields (max_drawdown, max_monthly_loss, kill_switch) are
never scaled — ModeMultipliers has no fields for them.
"""

from __future__ import annotations

import enum

import msgspec


class RiskMode(str, enum.Enum):
    AGGRESSIVE = "AGGRESSIVE"
    BALANCED = "BALANCED"
    DEFENSIVE = "DEFENSIVE"
    CUSTOM = "CUSTOM"


class ModeMultipliers(msgspec.Struct, frozen=True):
    """Per-mode multipliers applied to base RiskConfig values.

    Multiplier fields (× base): max_risk_per_trade, max_position_pct, etc.
    Absolute fields (used directly): confidence_floor, daily_size_mult,
    drawdown_aggression.
    """

    # Multipliers on base config
    max_risk_per_trade: float = 1.0
    max_position_pct: float = 1.0
    max_portfolio_heat: float = 1.0
    kelly_default_fraction: float = 1.0
    kelly_max_fraction: float = 1.0
    fat_finger_max_value: float = 1.0

    # Absolute values (replace hardcoded constants)
    confidence_floor: float = 0.8
    daily_size_mult: float = 0.5
    drawdown_aggression: float = 1.0  # exponent: <1 gentle, 1 linear, >1 harsh


# ── Presets ──

MODE_PRESETS: dict[RiskMode, ModeMultipliers] = {
    RiskMode.AGGRESSIVE: ModeMultipliers(
        max_risk_per_trade=2.0,
        max_position_pct=1.75,
        max_portfolio_heat=1.5,
        kelly_default_fraction=2.0,
        kelly_max_fraction=1.5,
        fat_finger_max_value=2.0,
        confidence_floor=0.5,
        daily_size_mult=0.7,
        drawdown_aggression=2.0,  # e>1: gentle at low DD, steep near max
    ),
    RiskMode.BALANCED: ModeMultipliers(),  # all defaults (1.0× / 0.8 / 0.5 / 1.0)
    RiskMode.DEFENSIVE: ModeMultipliers(
        max_risk_per_trade=0.5,
        max_position_pct=0.75,
        max_portfolio_heat=0.6,
        kelly_default_fraction=0.5,
        kelly_max_fraction=0.5,
        fat_finger_max_value=0.5,
        confidence_floor=0.9,
        daily_size_mult=0.25,
        drawdown_aggression=0.5,  # e<1: harsh even at low DD
    ),
}


# ── Safety bounds for CUSTOM mode ──

_CUSTOM_BOUNDS: dict[str, tuple[float, float]] = {
    "max_risk_per_trade": (0.25, 3.0),
    "max_position_pct": (0.25, 2.5),
    "max_portfolio_heat": (0.25, 2.0),
    "kelly_default_fraction": (0.25, 3.0),
    "kelly_max_fraction": (0.25, 2.0),
    "fat_finger_max_value": (0.25, 3.0),
    "confidence_floor": (0.3, 0.95),
    "daily_size_mult": (0.1, 1.0),
    "drawdown_aggression": (0.25, 4.0),
}


def clamp_custom(overrides: dict[str, float]) -> dict[str, float]:
    """Clamp custom mode overrides to safety bounds."""
    clamped: dict[str, float] = {}
    for key, val in overrides.items():
        if key in _CUSTOM_BOUNDS:
            lo, hi = _CUSTOM_BOUNDS[key]
            clamped[key] = max(lo, min(hi, val))
    return clamped


def resolve_mode(mode_str: str, custom_overrides: dict[str, float] | None = None) -> ModeMultipliers:
    """Resolve a mode string to ModeMultipliers.

    For CUSTOM mode, starts from BALANCED and applies clamped overrides.
    """
    mode = RiskMode(mode_str)
    if mode == RiskMode.CUSTOM:
        base = msgspec.structs.asdict(MODE_PRESETS[RiskMode.BALANCED])
        if custom_overrides:
            base.update(clamp_custom(custom_overrides))
        return ModeMultipliers(**base)
    return MODE_PRESETS[mode]
