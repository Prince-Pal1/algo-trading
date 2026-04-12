"""Tests for 3-level circuit breaker system."""

import pytest

from src.risk.circuit_breakers import CircuitBreakers
from src.risk.config import RiskConfig
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction

from tests.test_risk.test_kill_switch import _make_state


def _make_signal(
    action: SignalAction = SignalAction.LONG,
    risk_pct: float = 0.01,
    confidence: float = 0.9,
    strategy_name: str = "test_strat",
) -> Signal:
    return Signal(
        symbol="BTCUSDT", action=action, confidence=confidence,
        strategy_name=strategy_name, timeframe="1h",
        entry_price=50000.0, stop_loss=49000.0, risk_pct=risk_pct,
    )


class TestCircuitBreakers:
    def test_clean_state_passes(self):
        cfg = RiskConfig()
        state = _make_state()
        cb = CircuitBreakers(cfg, state)
        reason, mult = cb.check(_make_signal())
        assert reason is None
        assert mult == 1.0

    def test_close_always_passes(self):
        cfg = RiskConfig()
        state = _make_state()
        state.kill_switch_active = True  # even in worst case
        cb = CircuitBreakers(cfg, state)
        reason, mult = cb.check(_make_signal(action=SignalAction.CLOSE))
        assert reason is None
        assert mult == 1.0

    def test_level1_trade_risk_too_high(self):
        cfg = RiskConfig(max_risk_per_trade=0.02)
        state = _make_state()
        cb = CircuitBreakers(cfg, state)
        reason, mult = cb.check(_make_signal(risk_pct=0.05))
        assert reason is not None
        assert "CB_L1_TRADE" in reason
        assert cb.active_level >= 1

    def test_level1_passes_within_limit(self):
        cfg = RiskConfig(max_risk_per_trade=0.02)
        state = _make_state()
        cb = CircuitBreakers(cfg, state)
        reason, _ = cb.check(_make_signal(risk_pct=0.015))
        assert reason is None

    def test_level2_paused_strategy_rejected(self):
        cfg = RiskConfig()
        state = _make_state()
        state.strategy_paused.add("paused_strat")
        cb = CircuitBreakers(cfg, state)
        reason, _ = cb.check(_make_signal(strategy_name="paused_strat"))
        assert reason is not None
        assert "CB_L2_STRATEGY" in reason

    def test_level2_strategy_loss_pauses(self):
        cfg = RiskConfig(max_daily_loss=0.03)
        state = _make_state()
        state.current_equity = 10_000
        # Strategy lost more than 3% of equity
        state.strategy_pnl["losing_strat"] = -400.0
        cb = CircuitBreakers(cfg, state)
        reason, _ = cb.check(_make_signal(strategy_name="losing_strat"))
        assert reason is not None
        assert "CB_L2_STRATEGY" in reason
        assert "losing_strat" in state.strategy_paused

    def test_level3a_daily_loss_reduces_size(self):
        cfg = RiskConfig(max_daily_loss=0.03, daily_halt_enabled=True)
        state = _make_state()
        state.daily_pnl = -400.0  # 4% of 10K
        state._daily_start_equity = 10_000.0
        cb = CircuitBreakers(cfg, state)
        reason, mult = cb.check(_make_signal())
        assert reason is None  # Doesn't reject, just reduces
        assert mult == 0.5

    def test_level3b_weekly_loss_blocks_low_confidence(self):
        cfg = RiskConfig(max_weekly_loss=0.06, weekly_reduce_enabled=True)
        state = _make_state()
        state.weekly_pnl = -700.0  # 7% of 10K
        cb = CircuitBreakers(cfg, state)
        reason, _ = cb.check(_make_signal(confidence=0.5))
        assert reason is not None
        assert "CB_L3B_WEEKLY" in reason

    def test_level3b_weekly_loss_allows_high_confidence(self):
        cfg = RiskConfig(max_weekly_loss=0.06, weekly_reduce_enabled=True,
                         weekly_reduce_factor=0.5)
        state = _make_state()
        state.weekly_pnl = -700.0
        cb = CircuitBreakers(cfg, state)
        reason, mult = cb.check(_make_signal(confidence=0.9))
        assert reason is None
        assert mult <= 0.5

    def test_level3c_monthly_loss_halts(self):
        cfg = RiskConfig(max_monthly_loss=0.10, monthly_halt_enabled=True)
        state = _make_state()
        state.monthly_pnl = -1200.0  # 12% of 10K
        cb = CircuitBreakers(cfg, state)
        reason, _ = cb.check(_make_signal())
        assert reason is not None
        assert "CB_L3C_MONTHLY" in reason

    def test_level3d_drawdown_triggers_kill(self):
        cfg = RiskConfig(max_drawdown=0.15, max_drawdown_close_all=True)
        state = _make_state()
        state.peak_equity = 10_000.0
        state.current_equity = 8_400.0  # 16% drawdown
        kill_reasons = []
        cb = CircuitBreakers(cfg, state)
        cb.set_kill_switch_callback(lambda r: kill_reasons.append(r))
        reason, _ = cb.check(_make_signal())
        assert reason is not None
        assert "CB_L3D_DRAWDOWN" in reason
        assert len(kill_reasons) == 1
