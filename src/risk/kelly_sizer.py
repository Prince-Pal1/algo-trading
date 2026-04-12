"""Fractional Kelly position sizing with volatility targeting.

Replaces fixed-fractional sizing with mathematically optimal position sizing:
1. Compute Kelly fraction from strategy win_rate and avg_win/avg_loss
2. Apply fractional multiplier (quarter-Kelly by default — conservative)
3. Scale by volatility target: size *= vol_target / realized_vol
4. Hard cap at max_risk_per_trade

Falls back to signal.risk_pct when insufficient trade history.
"""

from __future__ import annotations

import numpy as np

from src.utils.logger import get_logger
from src.utils.types import Signal

from .config import RiskConfig, StrategyRiskProfile

log = get_logger("kelly_sizer")


class StrategyStats:
    """Statistics needed for Kelly computation."""

    __slots__ = ("win_rate", "avg_win", "avg_loss", "total_trades", "realized_vol")

    def __init__(self, win_rate: float = 0.0, avg_win: float = 0.0,
                 avg_loss: float = 0.0, total_trades: int = 0,
                 realized_vol: float = 0.0):
        self.win_rate = win_rate
        self.avg_win = avg_win
        self.avg_loss = avg_loss
        self.total_trades = total_trades
        self.realized_vol = realized_vol


class KellySizer:
    """Fractional Kelly position sizing with vol targeting."""

    def __init__(self, config: RiskConfig):
        self._cfg = config

    def compute(self, signal: Signal, stats: StrategyStats | None = None, *,
                profile: StrategyRiskProfile | None = None,
                kelly_frac: float | None = None,
                kelly_max: float | None = None,
                risk_cap: float | None = None) -> float:
        """Compute risk fraction (as pct of equity) for this signal.

        Args:
            profile: Per-strategy overrides (skip vol scaling, etc.)
            kelly_frac: Mode-resolved Kelly fraction (default_fraction * mode_mult)
            kelly_max: Mode-resolved Kelly max (max_fraction * mode_mult)
            risk_cap: Mode-resolved risk cap (max_risk_per_trade * mode_mult)

        Returns a float in [0, risk_cap].
        """
        effective_cap = risk_cap or self._cfg.max_risk_per_trade
        effective_frac = kelly_frac or self._cfg.kelly_default_fraction
        effective_max = kelly_max or self._cfg.kelly_max_fraction

        # Fallback: insufficient history → use strategy's suggested risk_pct
        if stats is None or stats.total_trades < self._cfg.kelly_min_trades:
            base = signal.risk_pct or 0.01
            return min(base, effective_cap)

        # Fallback: win rate too low for Kelly
        if stats.win_rate < self._cfg.kelly_min_win_rate:
            base = signal.risk_pct or 0.01
            return min(base, effective_cap)

        # Compute Kelly fraction
        kelly_f = self._kelly_fraction(stats.win_rate, stats.avg_win, stats.avg_loss)

        if kelly_f <= 0:
            # Negative Kelly = negative edge, use minimum
            return min(signal.risk_pct or 0.005, effective_cap)

        # Apply fractional Kelly (mode-resolved)
        sized = kelly_f * effective_frac
        sized = min(sized, effective_max)

        # Vol target scaling — skip if strategy already handles vol internally
        skip_vol = profile and profile.skip_kelly_vol_scaling
        if not skip_vol and stats.realized_vol > 1e-8 and self._cfg.vol_target > 0:
            vol_scalar = self._cfg.vol_target / stats.realized_vol
            vol_scalar = max(0.5, min(2.0, vol_scalar))
            sized *= vol_scalar

        # Hard cap
        sized = max(0.001, min(sized, effective_cap))

        return sized

    @staticmethod
    def _kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
        """Compute raw Kelly fraction: f = p - q/b.

        Where p = win probability, q = loss probability, b = avg_win/avg_loss.
        """
        if avg_loss <= 0 or avg_win <= 0:
            return 0.0

        b = avg_win / avg_loss  # Win/loss ratio
        p = win_rate
        q = 1.0 - p

        return p - (q / b)

    @staticmethod
    def compute_stats_from_trades(trades: list[dict]) -> StrategyStats:
        """Compute StrategyStats from a list of trade dicts.

        Each trade dict should have 'pnl' field at minimum.
        """
        if not trades:
            return StrategyStats()

        pnls = [t.get("pnl", 0.0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]

        win_rate = len(wins) / len(pnls) if pnls else 0.0
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0

        return StrategyStats(
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            total_trades=len(pnls),
        )
