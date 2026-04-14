"""Tier-based leverage budget allocator (floor + dynamic hybrid)."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.m3s.types import PortfolioSnapshot
from src.utils.logger import get_logger
from src.utils.types import Tier

log = get_logger("m3s.leverage_budget")


@dataclass(frozen=True)
class LeverageBudgetDecision:
    per_strategy: dict[str, float]
    per_tier_budget: dict[Tier, float]
    aggregate_cap: float
    floor_total: float
    dynamic_pool: float
    reasoning: str


class LeverageBudgetAllocator:
    """Allocates aggregate leverage cap to strategies via tier-level floors
    + dynamic pool weighted by rolling Sharpe.

    Algorithm:
    1. Each tier has a static floor fraction of aggregate_cap. Floors sum
       must be < 1.0 so there's a dynamic pool.
    2. Dynamic pool = aggregate_cap × (1 - sum(floor_fractions)).
    3. Dynamic pool is allocated across tiers by their sum of
       rolling_sharpe_30d across member strategies (zero-floored).
    4. Per-tier budget = floor × cap + dynamic weight × pool.
    5. Per-strategy grant = min(requested, tier_budget / strategies_in_tier).

    This keeps each strategy's grant ≤ its request (never up-sizes) while
    ensuring that tier-level budgets never exceed the aggregate cap.
    """

    def __init__(
        self,
        *,
        aggregate_cap: float,
        tier_floors: dict[Tier, float] | None = None,
    ) -> None:
        if aggregate_cap <= 0:
            raise ValueError(f"aggregate_cap must be > 0, got {aggregate_cap}")
        self._aggregate_cap = float(aggregate_cap)
        self._tier_floors = dict(tier_floors or {})
        total_floor = sum(self._tier_floors.values())
        if total_floor < 0 or total_floor > 1.0:
            raise ValueError(
                f"tier_floors sum must be in [0, 1], got {total_floor}"
            )

    def allocate_leverage(
        self,
        snapshot: PortfolioSnapshot,
        strategy_requests: dict[str, float],
        strategy_tiers: dict[str, Tier],
    ) -> LeverageBudgetDecision:
        if not strategy_requests:
            return LeverageBudgetDecision(
                per_strategy={},
                per_tier_budget={},
                aggregate_cap=self._aggregate_cap,
                floor_total=sum(self._tier_floors.values()),
                dynamic_pool=0.0,
                reasoning="no_requests",
            )

        # Group strategies by tier and collect per-strategy Sharpe
        tier_to_strategies: dict[Tier, list[str]] = {}
        for strategy_name, tier in strategy_tiers.items():
            if strategy_name not in strategy_requests:
                continue
            tier_to_strategies.setdefault(tier, []).append(strategy_name)

        # Compute tier scores from rolling Sharpe (≥0 only)
        tier_scores: dict[Tier, float] = {}
        for tier, strategies in tier_to_strategies.items():
            total = 0.0
            for strat_name in strategies:
                stat = snapshot.per_strategy.get(strat_name)
                if stat is not None:
                    total += max(0.0, float(stat.rolling_sharpe_30d))
            tier_scores[tier] = total

        # Dynamic pool + floor split
        floor_total = sum(
            self._tier_floors.get(tier, 0.0) for tier in tier_to_strategies
        )
        dynamic_fraction = max(0.0, 1.0 - floor_total)
        dynamic_pool = self._aggregate_cap * dynamic_fraction
        total_score = sum(tier_scores.values())

        per_tier_budget: dict[Tier, float] = {}
        for tier in tier_to_strategies:
            floor_frac = self._tier_floors.get(tier, 0.0)
            floor_part = self._aggregate_cap * floor_frac
            if total_score > 0:
                dynamic_part = dynamic_pool * (tier_scores.get(tier, 0.0) / total_score)
            else:
                # Cold start: no Sharpe data → equal-weight dynamic split
                dynamic_part = dynamic_pool / len(tier_to_strategies)
            per_tier_budget[tier] = floor_part + dynamic_part

        # Per-strategy grants: split tier budget equally across its members
        per_strategy: dict[str, float] = {}
        for tier, strategies in tier_to_strategies.items():
            if not strategies:
                continue
            per_strategy_share = per_tier_budget[tier] / len(strategies)
            for strat_name in strategies:
                requested = float(strategy_requests.get(strat_name, 0.0))
                granted = min(requested, per_strategy_share)
                per_strategy[strat_name] = max(0.0, granted)

        reasoning = (
            f"cap={self._aggregate_cap:.1f} floor={floor_total:.3f} "
            f"pool={dynamic_pool:.1f} tiers={len(tier_to_strategies)}"
        )
        log.info(
            "m3s.leverage_budget_allocated",
            aggregate_cap=self._aggregate_cap,
            floor_total=floor_total,
            dynamic_pool=dynamic_pool,
            tiers=len(tier_to_strategies),
            grants=per_strategy,
        )

        return LeverageBudgetDecision(
            per_strategy=per_strategy,
            per_tier_budget=per_tier_budget,
            aggregate_cap=self._aggregate_cap,
            floor_total=floor_total,
            dynamic_pool=dynamic_pool,
            reasoning=reasoning,
        )
