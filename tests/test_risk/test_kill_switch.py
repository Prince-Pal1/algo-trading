"""Tests for kill switch — highest priority risk check."""

import pytest

from src.risk.config import RiskConfig
from src.risk.kill_switch import KillSwitch
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction


def _make_state() -> RiskState:
    state = RiskState.__new__(RiskState)
    state.db_path = ":memory:"
    state.peak_equity = 10_000.0
    state.current_equity = 10_000.0
    state.daily_pnl = 0.0
    state.weekly_pnl = 0.0
    state.monthly_pnl = 0.0
    state._daily_start_equity = 10_000.0
    state._last_daily_reset = ""
    state._last_weekly_reset = ""
    state._last_monthly_reset = ""
    state.open_positions = {}
    state.strategy_pnl = {}
    state.recent_signals = __import__("collections").deque(maxlen=200)
    state.kill_switch_active = False
    state.kill_switch_reason = ""
    state.strategy_paused = set()
    state.active_mode = "AGGRESSIVE"
    state.custom_multipliers = {}
    state.fat_finger_avg_trade_size = 0.0
    state.fat_finger_trade_count = 0
    return state


def _make_signal(action: SignalAction = SignalAction.LONG) -> Signal:
    return Signal(
        symbol="BTCUSDT", action=action, confidence=0.9,
        strategy_name="test", timeframe="1h", entry_price=50000.0,
        stop_loss=49000.0, risk_pct=0.01,
    )


class TestKillSwitch:
    def test_inactive_allows_signals(self):
        state = _make_state()
        ks = KillSwitch(state)
        assert ks.check(_make_signal()) is None

    def test_active_rejects_long(self):
        state = _make_state()
        ks = KillSwitch(state)
        ks.activate("test_reason")
        reason = ks.check(_make_signal(SignalAction.LONG))
        assert reason is not None
        assert "KILL_SWITCH_ACTIVE" in reason

    def test_active_rejects_short(self):
        state = _make_state()
        ks = KillSwitch(state)
        ks.activate("test_reason")
        reason = ks.check(_make_signal(SignalAction.SHORT))
        assert reason is not None

    def test_active_allows_close(self):
        state = _make_state()
        ks = KillSwitch(state)
        ks.activate("test_reason")
        assert ks.check(_make_signal(SignalAction.CLOSE)) is None

    def test_deactivate_resumes(self):
        state = _make_state()
        ks = KillSwitch(state)
        ks.activate("test")
        assert ks.is_active is True
        ks.deactivate("manual")
        assert ks.is_active is False
        assert ks.check(_make_signal()) is None

    def test_activate_sets_state(self):
        state = _make_state()
        ks = KillSwitch(state)
        ks.activate("drawdown_breach")
        assert state.kill_switch_active is True
        assert state.kill_switch_reason == "drawdown_breach"

    def test_deactivate_clears_state(self):
        state = _make_state()
        ks = KillSwitch(state)
        ks.activate("test")
        ks.deactivate("manual_reset")
        assert state.kill_switch_active is False
        assert state.kill_switch_reason == ""
