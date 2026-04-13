"""Sub-phase 0.1 — tests for src/m3s/types.py msgspec structs."""

from __future__ import annotations

import msgspec
import pytest

from src.m3s.types import (
    AllocationDecision,
    CompoundState,
    M3SEventType,
    PortfolioSnapshot,
    StrategySnapshot,
)


class TestSnapshotRoundtrip:
    """Frozen snapshots must round-trip through msgspec JSON unchanged."""

    def test_portfolio_snapshot_roundtrip(self):
        snap = PortfolioSnapshot(
            ts_ms=1_700_000_000_000,
            equity=10_500.75,
            hwm=10_800.00,
            drawdown_pct=0.0278,
            per_strategy={
                "bb_rsi_mr_opt": StrategySnapshot(
                    name="bb_rsi_mr_opt",
                    n_trades_30d=22,
                    rolling_sharpe_30d=1.75,
                    realized_vol_30d=0.12,
                    pnl_30d=312.40,
                    lifetime_sharpe=1.80,
                    lifetime_winrate=0.54,
                ),
            },
            signal_corr={"bb_rsi_mr_opt|vol_momentum": 0.42},
        )

        encoded = msgspec.json.encode(snap)
        decoded = msgspec.json.decode(encoded, type=PortfolioSnapshot)
        assert decoded == snap
        # Nested struct survived
        assert decoded.per_strategy["bb_rsi_mr_opt"].n_trades_30d == 22
        assert decoded.signal_corr["bb_rsi_mr_opt|vol_momentum"] == pytest.approx(0.42)

    def test_allocation_decision_roundtrip(self):
        dec = AllocationDecision(
            ts_ms=1_700_000_060_000,
            weights={"bb_rsi_mr_opt": 0.40, "donchian_adx": 0.30, "vol_momentum": 0.30},
            method="hrp_lite",
            inputs_hash="deadbeef",
            reasoning="equal-weight within single cluster; cold-start path",
        )
        encoded = msgspec.json.encode(dec)
        decoded = msgspec.json.decode(encoded, type=AllocationDecision)
        assert decoded == dec
        assert sum(decoded.weights.values()) == pytest.approx(1.0)


class TestFrozenInvariants:
    """Frozen structs must reject attribute mutation."""

    def test_portfolio_snapshot_is_frozen(self):
        snap = PortfolioSnapshot(
            ts_ms=0,
            equity=10_000.0,
            hwm=10_000.0,
            drawdown_pct=0.0,
            per_strategy={},
            signal_corr={},
        )
        with pytest.raises((AttributeError, TypeError)):
            snap.equity = 9999.0  # type: ignore[misc]

    def test_compound_state_is_mutable(self):
        """CompoundState is deliberately mutable — the compounder writes to it."""
        st = CompoundState(
            base_equity=10_000.0,
            hwm=10_000.0,
            last_updated_ts_ms=0,
            mode="STANDARD",
        )
        st.base_equity = 10_500.0
        st.hwm = 10_500.0
        assert st.base_equity == 10_500.0
        assert st.hwm == 10_500.0


class TestEventTypeEnum:
    """M3SEventType is string-valued for stable on-disk storage."""

    def test_event_type_values_are_strings(self):
        assert M3SEventType.ALLOCATION.value == "allocation"
        assert M3SEventType.COMPOUND_UPDATE.value == "compound_update"
        assert M3SEventType.CUSTOM_CONFIG_LOADED.value == "custom_config_loaded"
        # All enum values are plain strings so they serialize naturally
        for ev in M3SEventType:
            assert isinstance(ev.value, str)
            assert ev.value == ev.value.lower()
