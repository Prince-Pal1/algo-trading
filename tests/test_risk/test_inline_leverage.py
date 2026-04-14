from __future__ import annotations

import time

import pytest

from src.risk.inline_leverage import (
    AGGRESSIVE_RETAIL_PROFILE,
    INSTITUTIONAL_PROFILE,
    InlineLeverageConfig,
    InlineLeverageGates,
    InlineRiskSnapshot,
)
from src.utils.types import Signal, SignalAction


def _sig(
    leverage: float = 10.0,
    entry_price: float = 2400.0,
    stop_loss: float = 2395.0,
    risk_pct: float = 0.01,
) -> Signal:
    return Signal(
        symbol="XAUUSD",
        action=SignalAction.LONG,
        confidence=0.8,
        strategy_name="test",
        timeframe="1h",
        entry_price=entry_price,
        stop_loss=stop_loss,
        risk_pct=risk_pct,
        leverage=leverage,
    )


def _snap(equity: float = 10_000.0, existing_notional: float = 0.0) -> InlineRiskSnapshot:
    return InlineRiskSnapshot(equity=equity, existing_notional=existing_notional)


class TestInlineLeverageGates:
    def test_passes_clean_signal(self):
        gates = InlineLeverageGates()
        result = gates.check(_sig(leverage=10), _snap())
        assert result.passed is True

    def test_rejects_over_max_per_position(self):
        gates = InlineLeverageGates()
        result = gates.check(_sig(leverage=600), _snap())
        assert result.passed is False
        assert "LEVERAGE_CAP" in result.reason

    def test_rejects_zero_equity(self):
        gates = InlineLeverageGates()
        result = gates.check(_sig(), _snap(equity=0))
        assert result.passed is False
        assert "zero equity" in result.reason

    def test_rejects_aggregate_over_cap(self):
        gates = InlineLeverageGates(InlineLeverageConfig(max_aggregate_leverage=10.0))
        # existing 95k notional + small new = 95k+/10k equity = 9.5x → pass
        result = gates.check(_sig(risk_pct=0.0001), _snap(equity=10_000.0, existing_notional=95_000.0))
        assert result.passed is True
        # Now bigger new notional pushes over
        result = gates.check(
            _sig(risk_pct=0.01, entry_price=2400.0, stop_loss=2395.0),
            _snap(equity=10_000.0, existing_notional=95_000.0),
        )
        assert result.passed is False
        assert "AGGREGATE_LEVERAGE" in result.reason

    def test_rejects_stop_inside_liquidation_buffer(self):
        gates = InlineLeverageGates(InlineLeverageConfig(max_aggregate_leverage=10_000.0))
        # At L=500, 1/L = 0.2%. Stop distance 1% > 0.2% × 0.8 = 0.16% → reject
        sig = _sig(leverage=500.0, entry_price=2400.0, stop_loss=2376.0)  # 1% stop
        result = gates.check(sig, _snap(equity=10_000.0))
        assert result.passed is False
        assert "LIQUIDATION_BUFFER" in result.reason


class TestProfiles:
    def test_institutional_profile_enforces_gates(self):
        gates = InlineLeverageGates(INSTITUTIONAL_PROFILE)
        result = gates.check(_sig(leverage=600), _snap())
        assert result.passed is False

    def test_aggressive_profile_bypasses_gates(self):
        gates = InlineLeverageGates(AGGRESSIVE_RETAIL_PROFILE)
        # 1000× would fail the institutional gate but passes here
        result = gates.check(_sig(leverage=1000.0, stop_loss=2395.0), _snap())
        assert result.passed is True

    def test_profile_override_via_set_profile(self):
        gates = InlineLeverageGates(INSTITUTIONAL_PROFILE)
        gates.set_profile("aggressive", AGGRESSIVE_RETAIL_PROFILE)
        # Without profile name, uses institutional and fails
        assert gates.check(_sig(leverage=1000), _snap()).passed is False
        # With profile name, uses the aggressive config
        assert gates.check(_sig(leverage=1000), _snap(), profile="aggressive").passed is True


class TestPerformance:
    def test_1000_signals_under_1_second(self):
        gates = InlineLeverageGates()
        snap = _snap()
        sig = _sig(leverage=10)
        start = time.perf_counter()
        for _ in range(1000):
            gates.check(sig, snap)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"1000 inline gate checks took {elapsed:.3f}s, must be < 1s"
