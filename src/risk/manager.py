"""Risk Manager — orchestrates all risk checks in sequence.

The central risk class that every signal must pass through before execution.
Checks run in priority order; any rejection short-circuits the chain.

Check order:
1. Kill switch         (highest priority — immediate reject)
2. Circuit breakers    (3-level escalating defense)
3. Fat finger guard    (order sanity)
4. Position limits     (exposure caps)
5. Duplicate filter    (dedup)
6. Kelly sizer         (compute position size)
7. Drawdown scaler     (reduce size based on drawdown)
"""

from __future__ import annotations

import sqlite3
import time

import orjson

from src.utils.logger import get_logger
from src.utils.types import Fill, RiskDecision, Signal, SignalAction

from .circuit_breakers import CircuitBreakers
from .config import RiskConfig
from .drawdown_scaler import DrawdownScaler
from .duplicate_filter import DuplicateFilter
from .fat_finger import FatFingerGuard
from .kelly_sizer import KellySizer, StrategyStats
from .kill_switch import KillSwitch
from .modes import ModeMultipliers, resolve_mode
from .position_limits import PositionLimits
from .state import RiskState

log = get_logger("risk_manager")


class RiskManager:
    """Orchestrates all risk checks in sequence."""

    def __init__(self, config: RiskConfig, state: RiskState):
        self.config = config
        self.state = state

        # Initialize all check modules
        self.kill_switch = KillSwitch(state)
        self.circuit_breakers = CircuitBreakers(config, state)
        self.fat_finger = FatFingerGuard(config, state)
        self.position_limits = PositionLimits(config, state)
        self.duplicate_filter = DuplicateFilter(config.duplicate_cooldown_s)
        self.kelly_sizer = KellySizer(config)
        self.drawdown_scaler = DrawdownScaler(config)

        # Wire circuit breaker L3d to kill switch
        self.circuit_breakers.set_kill_switch_callback(self.kill_switch.activate)

        # Strategy stats cache (populated from DB or updated live)
        self._strategy_stats: dict[str, StrategyStats] = {}

    def evaluate(self, signal: Signal) -> RiskDecision:
        """Run all risk checks on a signal.

        Returns RiskDecision with approved/rejected status, reason, and
        adjusted quantity if approved.
        """
        # Reset period counters if needed (use signal timestamp for backtests)
        self.state.check_period_resets(signal.timestamp)

        checks_passed: list[str] = []

        # Resolve mode + strategy profile
        mode_mults = self._resolve_mode()
        profile = self.config.strategy_profiles.get(signal.strategy_name)
        stats = self._strategy_stats.get(signal.strategy_name)

        # Track latest price for fat finger deviation check (always, even if rejected)
        if signal.entry_price:
            self.fat_finger.update_price(signal.symbol, signal.entry_price)

        # 1. Kill switch
        reason = self.kill_switch.check(signal)
        if reason:
            self._log_decision(signal, False, reason, checks_passed)
            return RiskDecision(approved=False, reason=reason, checks_passed=checks_passed)
        checks_passed.append("kill_switch")

        # CLOSE signals skip remaining checks (always allow exits)
        if signal.action == SignalAction.CLOSE:
            return RiskDecision(
                approved=True, reason="CLOSE_ALLOWED",
                checks_passed=checks_passed,
            )

        # 2. Circuit breakers — mode + profile + stats
        cb_reason, size_mult = self.circuit_breakers.check(
            signal, mode_mults=mode_mults, profile=profile, stats=stats)
        if cb_reason:
            self._log_decision(signal, False, cb_reason, checks_passed)
            return RiskDecision(approved=False, reason=cb_reason, checks_passed=checks_passed)
        checks_passed.append("circuit_breakers")

        # 3. Fat finger guard
        resolved_ff = self.config.fat_finger_max_value * mode_mults.fat_finger_max_value
        reason = self.fat_finger.check(signal, resolved_max_value=resolved_ff)
        if reason:
            self._log_decision(signal, False, reason, checks_passed)
            return RiskDecision(approved=False, reason=reason, checks_passed=checks_passed)
        checks_passed.append("fat_finger")

        # 4. Position limits — mode-resolved + profile
        resolved_pos = self.config.max_position_pct * mode_mults.max_position_pct
        resolved_heat = self.config.max_portfolio_heat * mode_mults.max_portfolio_heat
        reason = self.position_limits.check(
            signal, profile=profile,
            resolved_max_position=resolved_pos, resolved_max_heat=resolved_heat)
        if reason:
            self._log_decision(signal, False, reason, checks_passed)
            return RiskDecision(approved=False, reason=reason, checks_passed=checks_passed)
        checks_passed.append("position_limits")

        # 5. Duplicate filter
        reason = self.duplicate_filter.check(signal)
        if reason:
            self._log_decision(signal, False, reason, checks_passed)
            return RiskDecision(approved=False, reason=reason, checks_passed=checks_passed)
        checks_passed.append("duplicate_filter")

        # 5b. Leverage gates (G.0c) — hard rejects, M3S cannot override.
        # Only fire when Signal.leverage is set AND gates are enabled. Crypto
        # strategies leave leverage=None so these gates are no-ops for them.
        if self.config.leverage_gates_enabled and signal.leverage is not None:
            reason = self._check_leverage_gates(signal)
            if reason:
                self._log_decision(signal, False, reason, checks_passed)
                return RiskDecision(approved=False, reason=reason, checks_passed=checks_passed)
            checks_passed.append("leverage_gates")

        # 6. Kelly sizer — mode-resolved fractions + profile
        risk_cap = self.config.max_risk_per_trade * mode_mults.max_risk_per_trade
        kelly_frac = self.config.kelly_default_fraction * mode_mults.kelly_default_fraction
        kelly_max = self.config.kelly_max_fraction * mode_mults.kelly_max_fraction
        risk_pct = self.kelly_sizer.compute(
            signal, stats, profile=profile,
            kelly_frac=kelly_frac, kelly_max=kelly_max, risk_cap=risk_cap)
        checks_passed.append("kelly_sizer")

        # 7. Drawdown scaler — profile + mode aggression
        dd = self.state.get_drawdown_pct()
        risk_pct = self.drawdown_scaler.adjust(
            risk_pct, dd, profile=profile, aggression=mode_mults.drawdown_aggression)
        checks_passed.append("drawdown_scaler")

        # Apply circuit breaker size multiplier
        risk_pct *= size_mult

        # Compute final quantity
        adjusted_qty = self._compute_quantity(signal, risk_pct)

        if adjusted_qty <= 0:
            reason = "ZERO_QUANTITY: risk checks reduced size to zero"
            self._log_decision(signal, False, reason, checks_passed)
            return RiskDecision(approved=False, reason=reason, checks_passed=checks_passed)

        self._log_decision(signal, True, "APPROVED", checks_passed, adjusted_qty, risk_pct)

        return RiskDecision(
            approved=True,
            reason="APPROVED",
            adjusted_quantity=adjusted_qty,
            adjusted_risk_pct=risk_pct,
            checks_passed=checks_passed,
            size_multiplier=size_mult,
        )

    def update_fill(self, fill: Fill, strategy_name: str = "") -> None:
        """Update state after a trade executes."""
        self.state.update_equity(self.state.current_equity)
        self.fat_finger.update_avg_trade_size(fill.quantity)

        # Track position
        if fill.side.value == "BUY":
            self.state.add_position(
                fill.symbol, strategy_name, "LONG", fill.quantity, fill.price,
            )
        elif fill.side.value == "SELL":
            self.state.remove_position(fill.symbol)

    def update_trade_close(self, strategy: str, pnl: float, symbol: str) -> None:
        """Update state when a trade closes."""
        self.state.record_trade_pnl(strategy, pnl)
        self.state.remove_position(symbol)
        self.state.persist()

    def set_strategy_stats(self, strategy: str, stats: StrategyStats) -> None:
        """Update cached stats for a strategy."""
        self._strategy_stats[strategy] = stats

    def _check_leverage_gates(self, signal: Signal) -> str | None:
        """G.0c leverage-first safety gates.

        Three hard rejects, in order:
        1. Per-position leverage > max_per_position_leverage (absolute cap)
        2. Aggregate portfolio leverage would exceed max_aggregate_leverage
        3. Stop loss is inside the liquidation buffer zone

        Returns a rejection reason string, or None to pass.
        """
        cfg = self.config
        lev = signal.leverage or 1.0

        # Gate 1: per-position absolute cap. Hard limit regardless of strategy.
        if lev > cfg.max_per_position_leverage:
            return (
                f"LEVERAGE_CAP: requested leverage {lev:.0f}x exceeds "
                f"max_per_position_leverage {cfg.max_per_position_leverage:.0f}x"
            )

        # Gate 2: aggregate portfolio leverage.
        # Current effective leverage = sum of (position_notional / equity)
        # across open positions. Adding this new signal must not push the
        # total above max_aggregate_leverage.
        equity = self.state.current_equity
        if equity <= 0:
            return "LEVERAGE_CAP: zero equity"
        # Sum notionals of existing open positions
        existing_notional = 0.0
        for pos in self.state.open_positions.values():
            price = float(getattr(pos, "entry_price", 0.0) or 0.0)
            qty = float(getattr(pos, "quantity", 0.0) or 0.0)
            existing_notional += abs(price * qty)
        # Estimate notional of the new signal
        new_notional = 0.0
        if signal.entry_price and signal.risk_pct and signal.stop_loss:
            stop_dist = abs(signal.entry_price - signal.stop_loss)
            if stop_dist > 0:
                risk_amount = equity * float(signal.risk_pct)
                qty_est = risk_amount / stop_dist
                new_notional = abs(signal.entry_price * qty_est)
        total_leverage = (existing_notional + new_notional) / equity
        if total_leverage > cfg.max_aggregate_leverage:
            return (
                f"AGGREGATE_LEVERAGE: total {total_leverage:.1f}x would exceed "
                f"cap {cfg.max_aggregate_leverage:.1f}x "
                f"(existing ${existing_notional:.0f} + new ${new_notional:.0f} "
                f"vs equity ${equity:.0f})"
            )

        # Gate 3: liquidation buffer.
        # At leverage L, margin_call distance ≈ equity / (notional × L).
        # Reject if the stop is closer to entry than `liquidation_buffer_pct`
        # of the margin-call distance — i.e., if a small slip past the stop
        # would blow the account.
        if signal.entry_price and signal.stop_loss and lev > 1.0:
            stop_dist_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
            # Margin-call distance at leverage L is roughly 1/L of price
            # (a 1/L move wipes the margin). Require stop to be WITHIN that
            # distance by at least liquidation_buffer_pct margin.
            margin_call_dist_pct = 1.0 / lev
            buffer = cfg.liquidation_buffer_pct
            # Stop must be closer than (1 - buffer) × margin_call_dist
            max_safe_stop = margin_call_dist_pct * (1.0 - buffer)
            if stop_dist_pct > max_safe_stop:
                return (
                    f"LIQUIDATION_BUFFER: stop distance {stop_dist_pct:.3%} at "
                    f"{lev:.0f}x leverage is inside liquidation zone "
                    f"(max_safe={max_safe_stop:.3%}, buffer={buffer:.0%})"
                )

        return None

    def set_mode(self, mode: str, custom: dict[str, float] | None = None) -> None:
        """Change operating mode at runtime."""
        self.state.active_mode = mode
        if custom:
            self.state.custom_multipliers = custom
        self.state.persist()
        log.info("risk_mode_changed", mode=mode)

    def _resolve_mode(self) -> ModeMultipliers:
        """Resolve current mode to multipliers."""
        return resolve_mode(self.state.active_mode, self.state.custom_multipliers)

    def get_status(self) -> dict:
        """Return current risk state for monitoring."""
        return {
            "kill_switch": self.kill_switch.is_active,
            "circuit_breaker_level": self.circuit_breakers.active_level,
            "size_multiplier": self.circuit_breakers.size_multiplier,
            "equity": self.state.current_equity,
            "peak_equity": self.state.peak_equity,
            "drawdown_pct": round(self.state.get_drawdown_pct(), 4),
            "daily_loss_pct": round(self.state.get_daily_loss_pct(), 4),
            "weekly_loss_pct": round(self.state.get_weekly_loss_pct(), 4),
            "monthly_loss_pct": round(self.state.get_monthly_loss_pct(), 4),
            "open_positions": len(self.state.open_positions),
            "total_exposure": round(self.state.get_total_exposure(), 2),
            "strategies_paused": list(self.state.strategy_paused),
            "mode": self.state.active_mode,
        }

    def _compute_quantity(self, signal: Signal, risk_pct: float) -> float:
        """Convert risk_pct to actual quantity using signal's SL."""
        equity = self.state.current_equity
        entry = signal.entry_price or 0.0

        if entry <= 0 or equity <= 0:
            return 0.0

        risk_amount = equity * risk_pct

        if signal.stop_loss and signal.stop_loss != entry:
            risk_per_unit = abs(entry - signal.stop_loss)
            if risk_per_unit > 0:
                return risk_amount / risk_per_unit

        # Fallback: fixed fraction of equity
        return risk_amount / entry

    def _log_decision(self, signal: Signal, approved: bool, reason: str,
                      checks: list[str], quantity: float | None = None,
                      risk_pct: float | None = None) -> None:
        """Log risk decision to SQLite audit table."""
        if self.state.db_path == ":memory:":
            return
        try:
            conn = sqlite3.connect(self.state.db_path)
            conn.execute(
                """INSERT INTO risk_decisions
                   (timestamp, signal_symbol, signal_strategy, signal_action,
                    approved, reason, original_risk_pct, adjusted_quantity,
                    checks_json, equity_at_decision, drawdown_pct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(time.time() * 1000),
                    signal.symbol,
                    signal.strategy_name,
                    signal.action.value,
                    approved,
                    reason,
                    signal.risk_pct,
                    quantity,
                    orjson.dumps(checks).decode(),
                    self.state.current_equity,
                    self.state.get_drawdown_pct(),
                ),
            )
            conn.commit()
            conn.close()
        except sqlite3.OperationalError:
            pass  # Don't let logging failure block trading

        level = "info" if approved else "warning"
        getattr(log, level)(
            "risk_decision",
            approved=approved,
            reason=reason,
            symbol=signal.symbol,
            strategy=signal.strategy_name,
            action=signal.action.value,
            quantity=quantity,
        )
