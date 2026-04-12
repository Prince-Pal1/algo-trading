"""Duplicate / rapid-fire signal filter.

Prevents the same signal from executing multiple times within a cooldown window.
Uses (symbol, strategy, action) as the dedup key.
"""

from __future__ import annotations

import time

from src.utils.types import Signal, SignalAction

_DEFAULT_COOLDOWN = 30.0  # seconds


class DuplicateFilter:
    """Reject duplicate signals within cooldown period."""

    def __init__(self, cooldown_seconds: float = _DEFAULT_COOLDOWN):
        self._cooldown = cooldown_seconds
        self._recent: dict[str, float] = {}

    def check(self, signal: Signal) -> str | None:
        """Check if this signal is a duplicate.

        Returns rejection reason if duplicate, None if OK.
        CLOSE signals are never considered duplicates.

        Uses signal.timestamp (Unix ms) when non-zero (backtest mode),
        otherwise falls back to wall-clock time (live mode).
        """
        if signal.action in (SignalAction.CLOSE, SignalAction.HOLD):
            return None

        key = f"{signal.symbol}:{signal.strategy_name}:{signal.action.value}"
        # Use signal timestamp (ms → s) for backtesting, wall-clock for live
        now = signal.timestamp / 1000.0 if signal.timestamp > 0 else time.time()

        # Clean old entries
        self._recent = {k: t for k, t in self._recent.items() if now - t < self._cooldown}

        last_time = self._recent.get(key)
        if last_time is not None:
            elapsed = now - last_time
            return (f"DUPLICATE: {key} fired {elapsed:.1f}s ago "
                    f"(cooldown {self._cooldown:.0f}s)")

        self._recent[key] = now
        return None

    def record(self, signal: Signal) -> None:
        """Manually record a signal (for post-approval tracking)."""
        key = f"{signal.symbol}:{signal.strategy_name}:{signal.action.value}"
        self._recent[key] = time.time()
