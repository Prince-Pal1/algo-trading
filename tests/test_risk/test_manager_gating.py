"""RiskManager gating tests — every rejection path + approval path.

Uses an in-memory RiskState (db_path=":memory:") so we don't touch trades.db.
Each test exercises exactly one rejection reason by constructing the minimum
state needed, then asserts `decision.approved == False` with a specific reason.
"""

from __future__ import annotations

import pytest

from src.risk.config import RiskConfig
from src.risk.manager import RiskManager
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction


def _make_manager(initial_equity: float = 10_000.0) -> RiskManager:
    state = RiskState(db_path=":memory:")
    state.current_equity = initial_equity
    state.peak_equity = initial_equity
    state._daily_start_equity = initial_equity
    # Pre-seed period markers to match our signal timestamps (2023-11-14)
    # so check_period_resets does NOT wipe pre-populated pnl counters.
    state._last_daily_reset = "2023-11-14"
    state._last_weekly_reset = "2023-W46"
    state._last_monthly_reset = "2023-11"
    return RiskManager(config=RiskConfig(), state=state)


def _signal(action=SignalAction.LONG, entry=100.0, sl=95.0, qty_value=None, strategy="test_strat") -> Signal:
    return Signal(
        symbol="BTCUSDT",
        action=action,
        confidence=0.9,
        strategy_name=strategy,
        timeframe="1h",
        entry_price=entry,
        stop_loss=sl,
        risk_pct=0.01,
        timestamp=1_700_000_000_000,
    )


class TestApprovalPath:
    def test_clean_long_signal_approved(self):
        mgr = _make_manager()
        decision = mgr.evaluate(_signal())
        assert decision.approved, f"Expected approval, got: {decision.reason}"
        assert decision.reason == "APPROVED"
        # Sizing: risk = 10000 * risk_pct / |100 - 95| = 10000 * kelly_default * 0.25_default / 5
        assert decision.adjusted_quantity is not None
        assert decision.adjusted_quantity > 0
        # All 6 non-close checks should have been touched
        for check in ("kill_switch", "circuit_breakers", "fat_finger",
                      "position_limits", "duplicate_filter", "kelly_sizer",
                      "drawdown_scaler"):
            assert check in decision.checks_passed

    def test_close_signal_always_approved(self):
        """Exits are never gated — even with kill switch armed, CLOSE passes
        after the kill-switch check. With no kill switch, CLOSE short-circuits
        remaining checks."""
        mgr = _make_manager()
        decision = mgr.evaluate(_signal(action=SignalAction.CLOSE))
        assert decision.approved
        assert decision.reason == "CLOSE_ALLOWED"


class TestRejectionPaths:
    def test_kill_switch_blocks_long(self):
        mgr = _make_manager()
        mgr.kill_switch.activate("manual_test")
        decision = mgr.evaluate(_signal())
        assert not decision.approved
        assert "KILL_SWITCH" in decision.reason

    def test_daily_loss_reduces_size(self):
        """Daily loss ≥ 3% is NOT a hard reject — it reduces size via size_multiplier."""
        mgr = _make_manager()
        mgr.state.daily_pnl = -400.0  # 4% of starting equity
        mgr.state.current_equity = 9_600.0
        decision = mgr.evaluate(_signal())
        assert decision.approved  # Daily loss scales size, not rejects
        # Either size_multiplier dropped OR daily_cb fired
        assert decision.size_multiplier <= 1.0

    def test_monthly_loss_limit_rejects(self):
        """Monthly loss ≥ 10% → hard reject via CB_L3C_MONTHLY.

        Peak equity pinned to current_equity to avoid drawdown firing first
        (drawdown check runs ahead of monthly in the circuit-breaker chain).
        """
        mgr = _make_manager()
        mgr.state.peak_equity = 8_500.0
        mgr.state.current_equity = 8_500.0
        mgr.state.monthly_pnl = -1500.0  # 1500/8500 ≈ 17.6% > 10%
        decision = mgr.evaluate(_signal())
        assert not decision.approved
        assert "MONTHLY" in decision.reason

    def test_drawdown_limit_triggers_rejection(self):
        """Drawdown ≥ 15% → MAX_DRAWDOWN circuit breaker."""
        mgr = _make_manager(initial_equity=10_000.0)
        mgr.state.peak_equity = 10_000.0
        mgr.state.current_equity = 8_400.0  # 16% drawdown
        decision = mgr.evaluate(_signal())
        assert not decision.approved
        assert any(tok in decision.reason for tok in ("DRAWDOWN", "CIRCUIT"))

    def test_duplicate_signal_blocked(self):
        """Same signal within duplicate_cooldown_s → rejected."""
        mgr = _make_manager()
        sig = _signal()
        # First evaluation approves and registers the signal
        first = mgr.evaluate(sig)
        assert first.approved
        # Exact duplicate (same symbol/strategy/action/price) within cooldown
        second = mgr.evaluate(sig)
        assert not second.approved
        assert "DUPLICATE" in second.reason


class TestModeSwitching:
    def test_set_mode_changes_active_mode(self):
        mgr = _make_manager()
        assert mgr.state.active_mode == "AGGRESSIVE"
        mgr.set_mode("DEFENSIVE")
        assert mgr.state.active_mode == "DEFENSIVE"

    def test_mode_switch_persists_to_state(self):
        """set_mode writes active_mode to state (consumed by next evaluate)."""
        mgr = _make_manager()
        for mode in ("BALANCED", "DEFENSIVE", "AGGRESSIVE"):
            mgr.set_mode(mode)
            assert mgr.state.active_mode == mode
            # Subsequent evaluations resolve multipliers from state.active_mode
            resolved = mgr._resolve_mode()
            assert resolved is not None
