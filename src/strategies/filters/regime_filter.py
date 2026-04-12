"""Regime filter — pauses strategies during high-toxicity flow periods.

Uses VPIN (Volume-Synchronized Probability of Informed Trading) to detect
imminent price jumps. When VPIN > threshold, mean-reversion strategies should
pause because a large informed-flow-driven move is likely.

Reference: ScienceDirect 2025 — VPIN serial correlation makes it predictive.

Usage in backtest:
    filter = VPINRegimeFilter(threshold=0.7)
    for candle in candles:
        filter.update(candle)
        if filter.should_trade():
            signal = strategy.process(candle)
        else:
            # Skip or reduce position size
            pass
"""

from __future__ import annotations

from src.data.vpin import VPINCalculator


class VPINRegimeFilter:
    """Kill switch for strategies when VPIN exceeds threshold."""

    def __init__(
        self,
        threshold: float = 0.7,
        n_buckets: int = 50,
        cooldown_candles: int = 3,
    ):
        """
        Args:
            threshold: VPIN level above which trading pauses (default 0.7).
            n_buckets: VPIN calculation window (default 50 buckets).
            cooldown_candles: After VPIN drops below threshold, wait this many
                              candles before resuming (avoids whipsaw).
        """
        self.threshold = threshold
        self.cooldown_candles = cooldown_candles
        self._vpin_calc = VPINCalculator(n_buckets=n_buckets)
        self._cooldown_remaining = 0
        self._last_vpin = 0.0
        self._blocked_count = 0
        self._total_count = 0

    def update(self, open_: float, high: float, low: float, close: float, volume: float) -> None:
        """Feed a candle to the VPIN calculator."""
        self._total_count += 1
        vpin = self._vpin_calc.update_from_candle(open_, high, low, close, volume)
        if vpin is not None:
            self._last_vpin = vpin

        if self._last_vpin >= self.threshold:
            self._cooldown_remaining = self.cooldown_candles
            self._blocked_count += 1
        elif self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            self._blocked_count += 1

    def should_trade(self) -> bool:
        """Returns True if it's safe to trade, False if toxic flow detected."""
        if not self._vpin_calc.ready:
            return True  # Allow trading before VPIN is calibrated
        return self._last_vpin < self.threshold and self._cooldown_remaining == 0

    def position_scalar(self) -> float:
        """Scale position size inversely with VPIN.

        Returns 1.0 when VPIN is low (full size), 0.0 when >= threshold.
        """
        if not self._vpin_calc.ready:
            return 1.0
        if self._last_vpin >= self.threshold:
            return 0.0
        return 1.0 - (self._last_vpin / self.threshold)

    @property
    def current_vpin(self) -> float:
        return self._last_vpin

    @property
    def stats(self) -> dict:
        return {
            "current_vpin": round(self._last_vpin, 4),
            "threshold": self.threshold,
            "blocked_candles": self._blocked_count,
            "total_candles": self._total_count,
            "block_rate_pct": round(
                self._blocked_count / max(1, self._total_count) * 100, 1
            ),
        }
