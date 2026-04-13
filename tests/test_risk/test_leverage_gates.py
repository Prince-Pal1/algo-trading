"""Tests for G.0c leverage-first risk gates in RiskManager."""

from __future__ import annotations

import pytest

from src.risk.config import RiskConfig
from src.risk.manager import RiskManager
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction


def _fresh_manager(
    equity: float = 10_000.0,
    max_aggregate_leverage: float = 100.0,
    max_per_position_leverage: float = 500.0,
    liquidation_buffer_pct: float = 0.20,
    leverage_gates_enabled: bool = True,
) -> RiskManager:
    """Build a RiskManager with controllable leverage config."""
    cfg = RiskConfig(
        max_aggregate_leverage=max_aggregate_leverage,
        max_per_position_leverage=max_per_position_leverage,
        liquidation_buffer_pct=liquidation_buffer_pct,
        leverage_gates_enabled=leverage_gates_enabled,
    )
    state = RiskState(db_path=":memory:")
    state.current_equity = equity
    state.peak_equity = equity
    state.active_mode = "BALANCED"
    return RiskManager(config=cfg, state=state)


def _signal(
    leverage: float | None = None,
    entry_price: float = 2400.0,
    stop_loss: float = 2380.0,
    risk_pct: float = 0.01,
    symbol: str = "XAUUSD",
) -> Signal:
    return Signal(
        symbol=symbol,
        action=SignalAction.LONG,
        confidence=0.8,
        strategy_name="gold_test",
        timeframe="5m",
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=entry_price * 1.02,
        risk_pct=risk_pct,
        leverage=leverage,
        timestamp=1_700_000_000_000,
    )


class TestLeverageGatesDisabled:
    def test_crypto_signal_no_leverage_field_passes_gates(self):
        """Signals without a leverage field should sail through leverage gates."""
        mgr = _fresh_manager(leverage_gates_enabled=True)
        sig = _signal(leverage=None)  # crypto-style, no leverage
        reason = mgr._check_leverage_gates(sig)
        # But evaluate() must skip the leverage check when leverage is None
        # (that's the hot-path optimization — test via evaluate())
        decision = mgr.evaluate(sig)
        # Some other check may still reject, but the reason shouldn't be a leverage one
        if not decision.approved and decision.reason:
            assert "LEVERAGE" not in decision.reason
            assert "LIQUIDATION" not in decision.reason
            assert "AGGREGATE" not in decision.reason

    def test_gates_disabled_flag_skips_checks(self):
        """With leverage_gates_enabled=False, even huge leverage passes."""
        mgr = _fresh_manager(leverage_gates_enabled=False)
        sig = _signal(leverage=99_999.0)  # absurd
        # evaluate() must NOT call leverage gates
        decision = mgr.evaluate(sig)
        # _check_leverage_gates itself still returns a reason if called directly,
        # but evaluate() gates on the feature flag
        if not decision.approved and decision.reason:
            assert "LEVERAGE" not in decision.reason


class TestPerPositionLeverageCap:
    def test_within_cap_passes(self):
        mgr = _fresh_manager(max_per_position_leverage=500.0)
        sig = _signal(leverage=100.0)
        reason = mgr._check_leverage_gates(sig)
        # Not rejected for per-position cap specifically (may hit other gates)
        if reason:
            assert "max_per_position_leverage" not in reason

    def test_above_cap_rejects(self):
        mgr = _fresh_manager(max_per_position_leverage=500.0)
        sig = _signal(leverage=1000.0)
        reason = mgr._check_leverage_gates(sig)
        assert reason is not None
        assert "LEVERAGE_CAP" in reason
        assert "1000" in reason
        assert "500" in reason

    def test_at_cap_exact_passes(self):
        mgr = _fresh_manager(max_per_position_leverage=500.0)
        sig = _signal(leverage=500.0)
        reason = mgr._check_leverage_gates(sig)
        # At exactly the cap, should pass the per-position check
        if reason:
            assert "LEVERAGE_CAP" not in reason


class TestLiquidationBuffer:
    def test_safe_stop_passes(self):
        """Stop well inside the safe margin zone — should pass buffer check."""
        mgr = _fresh_manager(liquidation_buffer_pct=0.20)
        # At 10x leverage, margin call distance = 1/10 = 10% of price.
        # Max safe stop = 10% × (1 - 0.20) = 8% of price.
        # A 0.5% stop is WAY inside the safe zone.
        sig = _signal(entry_price=100.0, stop_loss=99.5, leverage=10.0)
        reason = mgr._check_leverage_gates(sig)
        if reason:
            assert "LIQUIDATION" not in reason

    def test_stop_too_far_at_high_leverage_rejects(self):
        """At 100x, margin call is 1% away. A 5% stop is outside the safe zone."""
        mgr = _fresh_manager(liquidation_buffer_pct=0.20)
        # entry=100, stop=95 → 5% stop distance
        # At 100x: margin call distance = 1% of price, safe max = 1% × 0.8 = 0.8%
        # 5% > 0.8% → REJECT
        sig = _signal(entry_price=100.0, stop_loss=95.0, leverage=100.0)
        reason = mgr._check_leverage_gates(sig)
        assert reason is not None
        assert "LIQUIDATION_BUFFER" in reason

    def test_no_leverage_no_buffer_check(self):
        """With leverage=1 (no leverage), the buffer check should be skipped."""
        mgr = _fresh_manager()
        # A 5% stop at 1x is fine (no margin call concern)
        sig = _signal(entry_price=100.0, stop_loss=95.0, leverage=1.0)
        reason = mgr._check_leverage_gates(sig)
        if reason:
            assert "LIQUIDATION" not in reason


class TestAggregateLeverage:
    def test_aggregate_under_cap_passes(self):
        """With no existing positions + modest new position, aggregate is low."""
        mgr = _fresh_manager(equity=10_000.0, max_aggregate_leverage=100.0)
        # XAUUSD at 2400 with risk_pct=0.01, stop=2390 → 10-unit stop distance
        # risk_amount = $100, qty_est = $100 / $10 = 10 units
        # notional = 2400 × 10 = $24,000 → aggregate = 2.4x (under 100x cap)
        sig = _signal(entry_price=2400.0, stop_loss=2390.0, leverage=50.0, risk_pct=0.01)
        reason = mgr._check_leverage_gates(sig)
        if reason:
            assert "AGGREGATE_LEVERAGE" not in reason

    def test_massive_position_exceeds_aggregate(self):
        """A huge new position with a tight stop should push aggregate over cap."""
        mgr = _fresh_manager(equity=1_000.0, max_aggregate_leverage=50.0)
        # entry=2400, stop=2399.999 → 0.001 stop distance
        # risk_amount = $10 (1% of $1000), qty_est = 10/0.001 = 10,000 units
        # notional = 2400 × 10,000 = $24,000,000 → aggregate = 24,000x (WAY over 50x)
        sig = _signal(entry_price=2400.0, stop_loss=2399.999, leverage=50.0, risk_pct=0.01)
        reason = mgr._check_leverage_gates(sig)
        assert reason is not None
        assert "AGGREGATE_LEVERAGE" in reason


class TestConfigLoading:
    def test_defaults(self):
        """Default RiskConfig has sensible leverage defaults."""
        cfg = RiskConfig()
        assert cfg.max_aggregate_leverage == 100.0
        assert cfg.max_per_position_leverage == 500.0
        assert cfg.liquidation_buffer_pct == 0.20
        assert cfg.leverage_gates_enabled is True
