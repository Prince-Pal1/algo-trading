"""Tests for RiskManager — full check chain orchestration."""

import pytest

from src.risk.config import RiskConfig
from src.risk.kelly_sizer import StrategyStats
from src.risk.manager import RiskManager
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction

from tests.test_risk.test_kill_switch import _make_state


def _make_signal(
    action: SignalAction = SignalAction.LONG,
    risk_pct: float = 0.01,
    confidence: float = 0.9,
    entry_price: float = 100.0,
    stop_loss: float = 99.0,
    strategy_name: str = "test_strat",
) -> Signal:
    """Build test signal. Defaults use low price to stay within position limits
    ($10K equity, $100 entry → ~$100 notional = 1% of equity)."""
    return Signal(
        symbol="TESTUSDT", action=action, confidence=confidence,
        strategy_name=strategy_name, timeframe="1h",
        entry_price=entry_price, stop_loss=stop_loss, risk_pct=risk_pct,
    )


def _make_manager(state: RiskState | None = None,
                  config: RiskConfig | None = None) -> RiskManager:
    if state is None:
        state = _make_state()
    if config is None:
        # Generous position limits so tests focus on the check being tested
        config = RiskConfig(max_position_pct=5.0, max_portfolio_heat=10.0)
    return RiskManager(config, state)


class TestRiskManager:
    def test_clean_signal_approved(self):
        mgr = _make_manager()
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is True
        assert decision.reason == "APPROVED"
        assert decision.adjusted_quantity is not None
        assert decision.adjusted_quantity > 0

    def test_close_always_approved(self):
        mgr = _make_manager()
        mgr.kill_switch.activate("test")
        decision = mgr.evaluate(_make_signal(action=SignalAction.CLOSE))
        assert decision.approved is True
        assert decision.reason == "CLOSE_ALLOWED"

    def test_kill_switch_short_circuits(self):
        mgr = _make_manager()
        mgr.kill_switch.activate("emergency")
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is False
        assert "KILL_SWITCH" in decision.reason
        assert decision.checks_passed == []

    def test_circuit_breaker_rejects(self):
        state = _make_state()
        cfg = RiskConfig(max_risk_per_trade=0.02)
        mgr = _make_manager(state=state, config=cfg)
        decision = mgr.evaluate(_make_signal(risk_pct=0.05))
        assert decision.approved is False
        assert "CB_L1_TRADE" in decision.reason
        assert "kill_switch" in decision.checks_passed

    def test_drawdown_reduces_size(self):
        state = _make_state()
        state.peak_equity = 10_000.0
        state.current_equity = 9_250.0  # 7.5% DD, half of 15% max
        mgr = _make_manager(state=state)
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is True
        assert decision.adjusted_risk_pct is not None
        # Drawdown scaler should reduce risk_pct

    def test_all_checks_in_order(self):
        mgr = _make_manager()
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is True
        expected = ["kill_switch", "circuit_breakers", "fat_finger",
                    "position_limits", "duplicate_filter", "kelly_sizer",
                    "drawdown_scaler"]
        assert decision.checks_passed == expected

    def test_zero_quantity_rejected(self):
        state = _make_state()
        # Use small max_drawdown with CB disabled so drawdown scaler zeros out
        state.peak_equity = 10_000.0
        state.current_equity = 9_500.0  # 5% DD
        cfg = RiskConfig(max_drawdown=0.05, max_drawdown_close_all=False,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = _make_manager(state=state, config=cfg)
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is False
        assert "ZERO_QUANTITY" in decision.reason

    def test_update_fill_tracks_position(self):
        from src.utils.types import Fill, Side
        mgr = _make_manager()
        fill = Fill(
            order_id="test-1", symbol="TESTUSDT", side=Side.BUY,
            price=100.0, quantity=1.0, commission=0.1,
            timestamp=1000, exchange="paper",
        )
        mgr.update_fill(fill, "test_strat")
        assert ("test_strat", "TESTUSDT") in mgr.state.open_positions

    def test_update_trade_close(self):
        mgr = _make_manager()
        mgr.state.add_position("TESTUSDT", "test", "LONG", 1.0, 100.0)
        assert ("test", "TESTUSDT") in mgr.state.open_positions
        mgr.update_trade_close("test", 10.0, "TESTUSDT")
        assert ("test", "TESTUSDT") not in mgr.state.open_positions
        assert mgr.state.daily_pnl == 10.0

    def test_get_status(self):
        mgr = _make_manager()
        status = mgr.get_status()
        assert "kill_switch" in status
        assert "equity" in status
        assert "drawdown_pct" in status
        assert "open_positions" in status

    def test_strategy_stats_used_in_kelly(self):
        cfg = RiskConfig(kelly_min_trades=10, kelly_min_win_rate=0.40,
                         max_risk_per_trade=0.05,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = _make_manager(config=cfg)
        stats = StrategyStats(win_rate=0.6, avg_win=2.0, avg_loss=1.0,
                              total_trades=50)
        mgr.set_strategy_stats("test_strat", stats)
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is True
        # Kelly should compute a different risk_pct than the default

    def test_duplicate_signal_rejected(self):
        cfg = RiskConfig(duplicate_cooldown_s=30.0,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = _make_manager(config=cfg)
        sig = _make_signal()
        d1 = mgr.evaluate(sig)
        assert d1.approved is True
        # Same signal again within cooldown
        d2 = mgr.evaluate(sig)
        assert d2.approved is False
        assert "DUPLICATE" in d2.reason

    def test_size_multiplier_from_circuit_breaker(self):
        from datetime import datetime, timezone
        state = _make_state()
        state.daily_pnl = -400.0
        state._daily_start_equity = 10_000.0
        # Set reset markers to today so check_period_resets() doesn't zero out daily_pnl
        now = datetime.now(timezone.utc)
        state._last_daily_reset = now.strftime("%Y-%m-%d")
        state._last_weekly_reset = now.strftime("%Y-W%W")
        state._last_monthly_reset = now.strftime("%Y-%m")
        cfg = RiskConfig(max_daily_loss=0.03, daily_halt_enabled=True,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = _make_manager(state=state, config=cfg)
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is True
        # AGGRESSIVE mode uses daily_size_mult=0.7 (not hardcoded 0.5)
        assert decision.size_multiplier == 0.7
