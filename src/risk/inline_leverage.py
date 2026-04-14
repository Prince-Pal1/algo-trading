"""In-process leverage gates — fast path for the leveraged backtest engine."""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.types import Signal


@dataclass(frozen=True)
class InlineLeverageConfig:
    max_per_position_leverage: float = 500.0
    max_aggregate_leverage: float = 100.0
    liquidation_buffer_pct: float = 0.20
    enabled: bool = True


@dataclass(frozen=True)
class InlineRiskSnapshot:
    equity: float
    existing_notional: float


@dataclass(frozen=True)
class InlineGateResult:
    passed: bool
    reason: str = ""


class InlineLeverageGates:
    def __init__(
        self,
        config: InlineLeverageConfig | None = None,
        *,
        profile_overrides: dict[str, InlineLeverageConfig] | None = None,
    ) -> None:
        self._cfg = config or InlineLeverageConfig()
        self._profiles = dict(profile_overrides or {})

    def set_profile(self, name: str, config: InlineLeverageConfig) -> None:
        self._profiles[name] = config

    def _cfg_for(self, profile: str | None) -> InlineLeverageConfig:
        if profile and profile in self._profiles:
            return self._profiles[profile]
        return self._cfg

    def check(
        self,
        signal: Signal,
        snapshot: InlineRiskSnapshot,
        *,
        profile: str | None = None,
    ) -> InlineGateResult:
        cfg = self._cfg_for(profile)
        if not cfg.enabled:
            return InlineGateResult(passed=True)

        lev = float(signal.leverage or 1.0)

        if lev > cfg.max_per_position_leverage:
            return InlineGateResult(
                passed=False,
                reason=f"LEVERAGE_CAP: {lev:.0f}x > {cfg.max_per_position_leverage:.0f}x",
            )

        if snapshot.equity <= 0:
            return InlineGateResult(passed=False, reason="LEVERAGE_CAP: zero equity")

        new_notional = 0.0
        if signal.entry_price and signal.risk_pct and signal.stop_loss:
            stop_dist = abs(float(signal.entry_price) - float(signal.stop_loss))
            if stop_dist > 0:
                risk_amount = snapshot.equity * float(signal.risk_pct)
                qty_est = risk_amount / stop_dist
                new_notional = abs(float(signal.entry_price) * qty_est)

        total_leverage = (snapshot.existing_notional + new_notional) / snapshot.equity
        if total_leverage > cfg.max_aggregate_leverage:
            return InlineGateResult(
                passed=False,
                reason=(
                    f"AGGREGATE_LEVERAGE: {total_leverage:.1f}x > "
                    f"cap {cfg.max_aggregate_leverage:.1f}x"
                ),
            )

        if signal.entry_price and signal.stop_loss and lev > 1.0:
            stop_dist_pct = abs(float(signal.entry_price) - float(signal.stop_loss)) / float(signal.entry_price)
            margin_call_dist_pct = 1.0 / lev
            max_safe_stop = margin_call_dist_pct * (1.0 - cfg.liquidation_buffer_pct)
            if stop_dist_pct > max_safe_stop:
                return InlineGateResult(
                    passed=False,
                    reason=(
                        f"LIQUIDATION_BUFFER: stop {stop_dist_pct:.3%} at {lev:.0f}x "
                        f"inside zone (max_safe={max_safe_stop:.3%})"
                    ),
                )

        return InlineGateResult(passed=True)


# Profile shortcuts
INSTITUTIONAL_PROFILE = InlineLeverageConfig(
    max_per_position_leverage=500.0,
    max_aggregate_leverage=100.0,
    liquidation_buffer_pct=0.20,
    enabled=True,
)

AGGRESSIVE_RETAIL_PROFILE = InlineLeverageConfig(
    max_per_position_leverage=1000.0,
    max_aggregate_leverage=10_000.0,  # effectively disabled
    liquidation_buffer_pct=0.0,
    enabled=False,  # gates globally off for this profile
)
