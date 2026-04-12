"""Drawdown-proportional position size scaler.

Linearly reduces position size as drawdown deepens:
- At 0% drawdown: 1.0x (full size)
- At max_drawdown/2: 0.5x (half size)
- At max_drawdown: 0.0x (effectively halted)

This provides graceful degradation rather than binary halt,
reducing risk exposure as losses accumulate.
"""

from __future__ import annotations

from .config import RiskConfig, StrategyRiskProfile


class DrawdownScaler:
    """Scale position sizes inversely with drawdown depth.

    Supports per-mode aggression exponent and per-strategy sensitivity:
    - aggression < 1: concave curve (gentle, AGGRESSIVE mode)
    - aggression = 1: linear (BALANCED mode, default)
    - aggression > 1: convex curve (harsh, DEFENSIVE mode)
    - sensitivity: multiplier on drawdown effect (0.5 = half as aggressive)
    - floor: minimum output (e.g., 0.3 = never reduce below 30%)
    """

    def __init__(self, config: RiskConfig):
        self._max_dd = config.max_drawdown

    def adjust(self, base_size: float, drawdown_pct: float, *,
               profile: StrategyRiskProfile | None = None,
               aggression: float = 1.0) -> float:
        """Apply drawdown scaling to a base position size.

        Args:
            base_size: The position size before drawdown adjustment.
            drawdown_pct: Current drawdown as fraction (0.0 to 1.0).
            profile: Per-strategy overrides (sensitivity, floor).
            aggression: Mode-resolved exponent on drawdown curve.

        Returns:
            Scaled position size (>= 0).
        """
        if self._max_dd <= 0:
            return base_size

        if drawdown_pct <= 0:
            return base_size

        sensitivity = profile.drawdown_sensitivity if profile else 1.0
        floor = profile.drawdown_floor if profile else 0.0

        ratio = drawdown_pct / self._max_dd
        multiplier = max(0.0, 1.0 - sensitivity * (ratio ** aggression))
        multiplier = max(floor, multiplier)

        return base_size * multiplier
