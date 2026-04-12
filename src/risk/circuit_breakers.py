"""3-level circuit breaker system.

Level 1 (Trade):     Single trade risk exceeds max → reject
Level 2 (Strategy):  Strategy rolling loss exceeds threshold → pause strategy
Level 3 (Portfolio):
  3a: Daily loss > 3%    → reduce all sizes by 50%
  3b: Weekly loss > 6%   → only high-confidence signals pass
  3c: Monthly loss > 10% → halt all trading
  3d: Drawdown > 15%     → close all + kill switch
"""

from __future__ import annotations

from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

from .config import RiskConfig, StrategyRiskProfile
from .modes import ModeMultipliers
from .state import RiskState

log = get_logger("circuit_breakers")


class CircuitBreakers:
    """3-level circuit breaker system."""

    def __init__(self, config: RiskConfig, state: RiskState):
        self._cfg = config
        self._state = state
        self._size_multiplier: float = 1.0
        self._active_level: int = 0
        self._kill_switch_callback = None

    def set_kill_switch_callback(self, callback) -> None:
        """Set callback to activate kill switch on Level 3d."""
        self._kill_switch_callback = callback

    def check(self, signal: Signal, *,
             mode_mults: ModeMultipliers | None = None,
             profile: StrategyRiskProfile | None = None,
             stats=None) -> tuple[str | None, float]:
        """Run all circuit breaker levels.

        Returns (rejection_reason, size_multiplier).
        rejection_reason is None if signal passes.
        size_multiplier is 1.0 normally, reduced under stress.
        """
        if signal.action == SignalAction.CLOSE:
            return None, 1.0  # Always allow exits

        # Reset multiplier each check
        self._size_multiplier = 1.0
        self._active_level = 0

        # Level 1: Trade-level (mode-resolved risk cap)
        reason = self._check_trade_level(signal, mode_mults, profile)
        if reason:
            return reason, 0.0

        # Level 2: Strategy-level
        reason = self._check_strategy_level(signal)
        if reason:
            return reason, 0.0

        # Level 3: Portfolio-level (ordered by severity)
        reason = self._check_portfolio_level(signal, mode_mults, profile, stats)
        if reason:
            return reason, 0.0

        return None, self._size_multiplier

    def _check_trade_level(self, signal: Signal,
                           mode_mults: ModeMultipliers | None = None,
                           profile: StrategyRiskProfile | None = None) -> str | None:
        """Level 1: Reject if single trade risk exceeds max."""
        # Profile override > mode-scaled > base config
        if profile and profile.max_risk_per_trade is not None:
            cap = profile.max_risk_per_trade
        elif mode_mults:
            cap = self._cfg.max_risk_per_trade * mode_mults.max_risk_per_trade
        else:
            cap = self._cfg.max_risk_per_trade

        if signal.risk_pct is not None and signal.risk_pct > cap:
            self._active_level = max(self._active_level, 1)
            return f"CB_L1_TRADE: risk_pct {signal.risk_pct:.3f} > max {cap:.3f}"
        return None

    def _check_strategy_level(self, signal: Signal) -> str | None:
        """Level 2: Pause strategy if its rolling loss exceeds threshold."""
        if signal.strategy_name in self._state.strategy_paused:
            self._active_level = max(self._active_level, 2)
            return f"CB_L2_STRATEGY: {signal.strategy_name} is paused"

        strategy_loss = self._state.strategy_pnl.get(signal.strategy_name, 0.0)
        if strategy_loss < 0 and self._state.current_equity > 0:
            loss_pct = abs(strategy_loss) / self._state.current_equity
            if loss_pct > self._cfg.max_daily_loss:
                self._state.strategy_paused.add(signal.strategy_name)
                self._active_level = max(self._active_level, 2)
                log.warning("strategy_paused", strategy=signal.strategy_name,
                            loss_pct=round(loss_pct, 4))
                return f"CB_L2_STRATEGY: {signal.strategy_name} loss {loss_pct:.2%} > threshold"
        return None

    def _check_portfolio_level(self, signal: Signal,
                               mode_mults: ModeMultipliers | None = None,
                               profile: StrategyRiskProfile | None = None,
                               stats=None) -> str | None:
        """Level 3: Portfolio-level escalating circuit breakers."""
        # 3d: Max drawdown → close all + kill switch (NON-NEGOTIABLE, never mode-scaled)
        dd = self._state.get_drawdown_pct()
        if dd >= self._cfg.max_drawdown and self._cfg.max_drawdown_close_all:
            self._active_level = 3
            if self._kill_switch_callback:
                self._kill_switch_callback(f"CB_L3D: drawdown {dd:.2%} >= {self._cfg.max_drawdown:.2%}")
            return f"CB_L3D_DRAWDOWN: {dd:.2%} >= max {self._cfg.max_drawdown:.2%}"

        # 3c: Monthly loss → halt all trading (NON-NEGOTIABLE)
        monthly = self._state.get_monthly_loss_pct()
        if monthly >= self._cfg.max_monthly_loss and self._cfg.monthly_halt_enabled:
            self._active_level = 3
            return f"CB_L3C_MONTHLY: loss {monthly:.2%} >= max {self._cfg.max_monthly_loss:.2%}"

        # 3b: Weekly loss → confidence/EV gate
        weekly = self._state.get_weekly_loss_pct()
        if weekly >= self._cfg.max_weekly_loss and self._cfg.weekly_reduce_enabled:
            self._active_level = max(self._active_level, 3)

            # Resolve confidence floor: profile > mode > default
            if profile and profile.confidence_floor is not None:
                floor = profile.confidence_floor
            elif mode_mults:
                floor = mode_mults.confidence_floor
            else:
                floor = 0.8

            # EV gate for trend strategies (uses expected value instead of raw confidence)
            if profile and profile.use_ev_gate and stats and getattr(stats, 'avg_loss', 0) > 0:
                ev = signal.confidence * (stats.avg_win / stats.avg_loss)
                if ev < profile.ev_threshold:
                    return (f"CB_L3B_WEEKLY: loss {weekly:.2%}, "
                            f"EV {ev:.2f} < {profile.ev_threshold}")
            elif signal.confidence < floor:
                return (f"CB_L3B_WEEKLY: loss {weekly:.2%}, "
                        f"confidence {signal.confidence:.2f} < {floor} required")

            # High confidence/EV passes but with reduced size
            self._size_multiplier = min(self._size_multiplier, self._cfg.weekly_reduce_factor)

        # 3a: Daily loss → reduce position sizes (mode-resolved multiplier)
        daily = self._state.get_daily_loss_pct()
        if daily >= self._cfg.max_daily_loss and self._cfg.daily_halt_enabled:
            self._active_level = max(self._active_level, 3)
            daily_mult = mode_mults.daily_size_mult if mode_mults else 0.5
            self._size_multiplier = min(self._size_multiplier, daily_mult)
            log.info("daily_cb_active", daily_loss=round(daily, 4),
                     size_mult=self._size_multiplier)

        return None

    @property
    def size_multiplier(self) -> float:
        return self._size_multiplier

    @property
    def active_level(self) -> int:
        return self._active_level
