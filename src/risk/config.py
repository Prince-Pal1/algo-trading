"""Typed risk configuration loaded from config/risk.toml."""

from __future__ import annotations

import msgspec


class StrategyRiskProfile(msgspec.Struct, frozen=True):
    """Per-strategy risk overrides. None fields → use mode/global default."""

    strategy_name: str

    # Confidence gating (Conflict 1: trend strategies blocked by 0.8 gate)
    confidence_floor: float | None = None
    use_ev_gate: bool = False       # expected value gate instead of raw confidence
    ev_threshold: float = 0.5       # conf * (avg_win / avg_loss) must exceed

    # Vol-aware sizing (Conflict 2: triple penalty on vol strategies)
    skip_kelly_vol_scaling: bool = False
    drawdown_sensitivity: float = 1.0   # multiplier on drawdown effect
    drawdown_floor: float = 0.0         # minimum drawdown scaler output

    # Position limits (Conflict 3: wide stops → huge notional but small risk)
    use_risk_based_limits: bool = False
    max_risk_pct_per_position: float | None = None
    max_position_pct: float | None = None

    # General overrides
    max_risk_per_trade: float | None = None


class RiskConfig(msgspec.Struct, frozen=True):
    """Typed mirror of config/risk.toml — immutable after load."""

    # [limits]
    max_risk_per_trade: float = 0.02
    max_daily_loss: float = 0.03
    max_weekly_loss: float = 0.06
    max_monthly_loss: float = 0.10
    max_drawdown: float = 0.15
    max_position_pct: float = 0.20
    max_portfolio_heat: float = 0.50
    max_correlated_exposure: float = 0.40

    # [circuit_breakers]
    daily_halt_enabled: bool = True
    weekly_reduce_enabled: bool = True
    weekly_reduce_factor: float = 0.5
    monthly_halt_enabled: bool = True
    max_drawdown_close_all: bool = True

    # [kelly]
    kelly_default_fraction: float = 0.25
    kelly_max_fraction: float = 0.50
    kelly_min_win_rate: float = 0.45
    kelly_min_trades: int = 30

    # [fat_finger]
    fat_finger_max_value: float = 10_000.0
    fat_finger_max_qty_mult: float = 10.0

    # [transport]
    zmq_addr: str = "tcp://127.0.0.1:5555"
    zmq_timeout_ms: int = 2000
    zmq_max_failures: int = 3
    duplicate_cooldown_s: float = 30.0
    vol_target: float = 0.15

    # Per-strategy profiles (keyed by strategy_name)
    strategy_profiles: dict[str, StrategyRiskProfile] = {}

    @classmethod
    def from_toml(cls) -> RiskConfig:
        """Load from the project config/risk.toml via get_config()."""
        from src.utils.config import get_config

        cfg = get_config()
        risk = cfg.risk if hasattr(cfg, "risk") else getattr(cfg, "_data", {}).get("risk", {})
        if isinstance(risk, dict):
            limits = risk.get("limits", {})
            cb = risk.get("circuit_breakers", {})
            kelly = risk.get("kelly", {})
            ff = risk.get("fat_finger", {})
        else:
            limits = cb = kelly = ff = {}

        return cls(
            max_risk_per_trade=limits.get("max_risk_per_trade", 0.02),
            max_daily_loss=limits.get("max_daily_loss", 0.03),
            max_weekly_loss=limits.get("max_weekly_loss", 0.06),
            max_monthly_loss=limits.get("max_monthly_loss", 0.10),
            max_drawdown=limits.get("max_drawdown", 0.15),
            max_position_pct=limits.get("max_position_pct", 0.20),
            max_portfolio_heat=limits.get("max_portfolio_heat", 0.50),
            max_correlated_exposure=limits.get("max_correlated_exposure", 0.40),
            daily_halt_enabled=cb.get("daily_halt_enabled", True),
            weekly_reduce_enabled=cb.get("weekly_reduce_enabled", True),
            weekly_reduce_factor=cb.get("weekly_reduce_factor", 0.5),
            monthly_halt_enabled=cb.get("monthly_halt_enabled", True),
            max_drawdown_close_all=cb.get("max_drawdown_close_all", True),
            kelly_default_fraction=kelly.get("default_fraction", 0.25),
            kelly_max_fraction=kelly.get("max_fraction", 0.50),
            kelly_min_win_rate=kelly.get("min_win_rate_for_kelly", 0.45),
            kelly_min_trades=kelly.get("min_trades_for_kelly", 30),
            fat_finger_max_value=ff.get("max_order_value_usd", 10_000.0),
            fat_finger_max_qty_mult=ff.get("max_quantity_multiplier", 10.0),
            strategy_profiles=cls._parse_profiles(risk),
        )

    @staticmethod
    def _parse_profiles(risk: dict) -> dict[str, StrategyRiskProfile]:
        """Parse [strategy_profiles.*] sections from risk config."""
        raw = risk.get("strategy_profiles", {})
        profiles: dict[str, StrategyRiskProfile] = {}
        for name, section in raw.items():
            if not isinstance(section, dict):
                continue
            profile = StrategyRiskProfile(
                strategy_name=name,
                confidence_floor=section.get("confidence_floor"),
                use_ev_gate=section.get("use_ev_gate", False),
                ev_threshold=section.get("ev_threshold", 0.5),
                skip_kelly_vol_scaling=section.get("skip_kelly_vol_scaling", False),
                drawdown_sensitivity=section.get("drawdown_sensitivity", 1.0),
                drawdown_floor=section.get("drawdown_floor", 0.0),
                use_risk_based_limits=section.get("use_risk_based_limits", False),
                max_risk_pct_per_position=section.get("max_risk_pct_per_position"),
                max_position_pct=section.get("max_position_pct"),
                max_risk_per_trade=section.get("max_risk_per_trade"),
            )
            profiles[name] = profile
        return profiles
