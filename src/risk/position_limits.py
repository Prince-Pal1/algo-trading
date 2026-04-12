"""Position limits — per-symbol and total exposure caps.

Prevents concentration risk:
- No single position > max_position_pct of equity (20%)
- Total portfolio heat (all positions) < max_portfolio_heat (50%)
"""

from __future__ import annotations

from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

from .config import RiskConfig, StrategyRiskProfile
from .state import RiskState

log = get_logger("position_limits")


class PositionLimits:
    """Position size and exposure limit checks."""

    def __init__(self, config: RiskConfig, state: RiskState):
        self._cfg = config
        self._state = state

    def check(self, signal: Signal, *,
             profile: StrategyRiskProfile | None = None,
             resolved_max_position: float | None = None,
             resolved_max_heat: float | None = None) -> str | None:
        """Check if new position would breach limits.

        Args:
            profile: Per-strategy overrides (risk-based limits, custom caps).
            resolved_max_position: Mode-resolved max_position_pct.
            resolved_max_heat: Mode-resolved max_portfolio_heat.

        Returns rejection reason if breached, None if OK.
        """
        if signal.action in (SignalAction.CLOSE, SignalAction.HOLD):
            return None

        equity = self._state.current_equity
        if equity <= 0:
            return "POSITION_LIMIT: zero equity"

        entry = signal.entry_price
        if entry is None or entry <= 0:
            return None

        # Estimate new position notional
        risk_pct = signal.risk_pct or 0.01
        if signal.stop_loss and signal.stop_loss != entry:
            risk_per_unit = abs(entry - signal.stop_loss)
            if risk_per_unit > 0:
                est_quantity = (equity * risk_pct) / risk_per_unit
                est_notional = est_quantity * entry
            else:
                return None
        else:
            est_notional = equity * risk_pct
            if entry > 0:
                est_notional = (equity * risk_pct / entry) * entry

        # Risk-based limits for strategies with wide stops (Conflict 3 fix)
        if (profile and profile.use_risk_based_limits
                and signal.stop_loss and signal.stop_loss != entry):
            max_risk = (profile.max_risk_pct_per_position
                        or self._cfg.max_risk_per_trade)
            if risk_pct > max_risk:
                return (f"POSITION_LIMIT_RISK: {signal.symbol} risk "
                        f"{risk_pct:.2%} > max {max_risk:.2%}")
            # Risk is within bounds — skip notional check for this strategy
        else:
            # Original notional-based check with mode-resolved limit
            effective_max = (profile.max_position_pct if profile and profile.max_position_pct
                             else resolved_max_position or self._cfg.max_position_pct)
            position_pct = est_notional / equity
            if position_pct > effective_max:
                return (f"POSITION_LIMIT_SYMBOL: {signal.symbol} would be "
                        f"{position_pct:.1%} of equity > max {effective_max:.1%}")

        # Check total portfolio heat (GLOBAL, not per-strategy)
        effective_heat = resolved_max_heat or self._cfg.max_portfolio_heat
        current_exposure = self._state.get_total_exposure()
        new_total = current_exposure + est_notional
        heat_pct = new_total / equity
        if heat_pct > effective_heat:
            return (f"POSITION_LIMIT_HEAT: total exposure would be "
                    f"{heat_pct:.1%} of equity > max {effective_heat:.1%}")

        return None
