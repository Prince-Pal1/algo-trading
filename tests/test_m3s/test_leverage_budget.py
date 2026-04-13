from __future__ import annotations

import pytest

from src.m3s.leverage_budget import LeverageBudgetAllocator
from src.m3s.types import PortfolioSnapshot, StrategySnapshot
from src.utils.types import Tier


def _snap(
    per_strategy: dict[str, StrategySnapshot] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts_ms=1_700_000_000_000,
        equity=10_000.0,
        hwm=10_000.0,
        drawdown_pct=0.0,
        per_strategy=per_strategy or {},
        signal_corr={},
    )


def _strat_snap(name: str, sharpe: float) -> StrategySnapshot:
    return StrategySnapshot(
        name=name,
        n_trades_30d=50,
        rolling_sharpe_30d=sharpe,
        realized_vol_30d=0.15,
        pnl_30d=0.0,
    )


class TestConstruction:
    def test_requires_positive_cap(self):
        with pytest.raises(ValueError, match="aggregate_cap"):
            LeverageBudgetAllocator(aggregate_cap=0)

    def test_rejects_floor_over_100pct(self):
        with pytest.raises(ValueError, match="tier_floors sum"):
            LeverageBudgetAllocator(
                aggregate_cap=100.0,
                tier_floors={Tier.INSTITUTIONAL_TREND: 0.6, Tier.DAY: 0.5},
            )


class TestEmptyFloors:
    def test_all_dynamic(self):
        """With zero floor, the full cap is the dynamic pool."""
        alloc = LeverageBudgetAllocator(aggregate_cap=100.0, tier_floors={})
        snap = _snap({
            "a": _strat_snap("a", 2.0),
            "b": _strat_snap("b", 1.0),
        })
        decision = alloc.allocate_leverage(
            snapshot=snap,
            strategy_requests={"a": 50.0, "b": 50.0},
            strategy_tiers={
                "a": Tier.INSTITUTIONAL_TREND,
                "b": Tier.INSTITUTIONAL_MR,
            },
        )
        # Sharpe: a=2.0, b=1.0 → ratio 2:1 → a gets 66.67, b gets 33.33
        # But each tier has only 1 strategy, so per_strategy = tier_budget
        # Grants are capped by requested (50, 50) so a=50, b=33.33
        assert decision.per_strategy["a"] == pytest.approx(50.0)
        assert decision.per_strategy["b"] == pytest.approx(33.333, rel=1e-3)

    def test_cold_start_equal_weight(self):
        """With no Sharpe data, dynamic pool splits equally across tiers."""
        alloc = LeverageBudgetAllocator(aggregate_cap=100.0, tier_floors={})
        snap = _snap()  # no per_strategy data
        decision = alloc.allocate_leverage(
            snapshot=snap,
            strategy_requests={"a": 50.0, "b": 50.0},
            strategy_tiers={
                "a": Tier.INSTITUTIONAL_TREND,
                "b": Tier.INSTITUTIONAL_MR,
            },
        )
        # 2 tiers × 50 each → both get 50 → both capped at requested 50
        assert decision.per_strategy["a"] == pytest.approx(50.0)
        assert decision.per_strategy["b"] == pytest.approx(50.0)


class TestFloorsOnly:
    def test_all_floor_no_dynamic(self):
        alloc = LeverageBudgetAllocator(
            aggregate_cap=100.0,
            tier_floors={
                Tier.INSTITUTIONAL_TREND: 0.60,
                Tier.INSTITUTIONAL_MR: 0.40,
            },
        )
        snap = _snap()
        decision = alloc.allocate_leverage(
            snapshot=snap,
            strategy_requests={"a": 100.0, "b": 100.0},
            strategy_tiers={
                "a": Tier.INSTITUTIONAL_TREND,
                "b": Tier.INSTITUTIONAL_MR,
            },
        )
        # Floors sum to 1.0 → dynamic_pool = 0
        assert decision.dynamic_pool == pytest.approx(0.0)
        assert decision.per_strategy["a"] == pytest.approx(60.0)
        assert decision.per_strategy["b"] == pytest.approx(40.0)


class TestMixedFloorAndDynamic:
    def test_floor_plus_dynamic_split(self):
        alloc = LeverageBudgetAllocator(
            aggregate_cap=100.0,
            tier_floors={
                Tier.INSTITUTIONAL_TREND: 0.30,
                Tier.INSTITUTIONAL_MR: 0.15,
            },
        )
        snap = _snap({
            "a": _strat_snap("a", 3.0),
            "b": _strat_snap("b", 1.0),
        })
        decision = alloc.allocate_leverage(
            snapshot=snap,
            strategy_requests={"a": 100.0, "b": 100.0},
            strategy_tiers={
                "a": Tier.INSTITUTIONAL_TREND,
                "b": Tier.INSTITUTIONAL_MR,
            },
        )
        # Floors: a tier gets 30, b tier gets 15 (total 45)
        # Dynamic pool = 55
        # Sharpe: a=3, b=1 → ratio 3:1 → a-tier gets 41.25, b-tier gets 13.75
        # Per-tier budget: a=30+41.25=71.25, b=15+13.75=28.75
        assert decision.per_strategy["a"] == pytest.approx(71.25, rel=1e-3)
        assert decision.per_strategy["b"] == pytest.approx(28.75, rel=1e-3)

    def test_requests_cap_grants(self):
        """Grant is never larger than the request, even if tier has more budget."""
        alloc = LeverageBudgetAllocator(aggregate_cap=100.0, tier_floors={})
        snap = _snap({"a": _strat_snap("a", 1.0)})
        decision = alloc.allocate_leverage(
            snapshot=snap,
            strategy_requests={"a": 25.0},  # only asked for 25
            strategy_tiers={"a": Tier.INSTITUTIONAL_TREND},
        )
        # Only one strategy → tier gets 100 budget, but request is 25
        assert decision.per_strategy["a"] == pytest.approx(25.0)


class TestMultipleStrategiesPerTier:
    def test_tier_budget_split_equally_within_tier(self):
        alloc = LeverageBudgetAllocator(aggregate_cap=100.0, tier_floors={})
        snap = _snap({
            "a1": _strat_snap("a1", 2.0),
            "a2": _strat_snap("a2", 1.0),
        })
        decision = alloc.allocate_leverage(
            snapshot=snap,
            strategy_requests={"a1": 100.0, "a2": 100.0},
            strategy_tiers={
                "a1": Tier.INSTITUTIONAL_TREND,
                "a2": Tier.INSTITUTIONAL_TREND,
            },
        )
        # Both in same tier → tier gets full 100 (only one tier, full dynamic pool)
        # Split equally → 50 each
        assert decision.per_strategy["a1"] == pytest.approx(50.0)
        assert decision.per_strategy["a2"] == pytest.approx(50.0)


class TestEmptyRequests:
    def test_no_requests_returns_empty(self):
        alloc = LeverageBudgetAllocator(aggregate_cap=100.0)
        decision = alloc.allocate_leverage(
            snapshot=_snap(),
            strategy_requests={},
            strategy_tiers={},
        )
        assert decision.per_strategy == {}
        assert decision.reasoning == "no_requests"
