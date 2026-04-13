"""In-process strategy-history cache for meta-label feature enrichment.

At feature-build time, the meta-label pipeline wants to know:
    - win rate over the last N closed trades
    - z-score of the last N realized PnLs
    - hours since the last signal
    - bars since the last trade close

Querying signal_audit on every signal hit would work but adds SQL I/O to
the hot signal path. This module instead maintains a bounded in-memory
ring buffer per strategy, updated by `record_signal()` (at emission
time) and `record_close()` (at trade close). Reads are O(1).

## Lifecycle

Construct one `StrategyHistoryCache` per engine. Feed it via:

    cache = StrategyHistoryCache(window=20)
    cache.record_signal(strategy, ts_ms)          # on every signal
    cache.record_close(strategy, pnl, meta_label, ts_ms)  # on every close

Query via:

    stats = cache.stats_for(strategy, now_ms)

`stats` is a `StrategyStats` dataclass whose fields map 1:1 to the
meta-label feature keys. Missing data → fields are None.

## Backward compat

If the cache is empty (engine just started, no signals yet), all stats
fields are None. The meta-label feature builder converts None → not set,
which the training pipeline fills with 0 — same as any other missing
feature.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque


@dataclass(frozen=True)
class StrategyStats:
    """Point-in-time derived stats for one strategy's recent history."""

    strategy_win_rate_last_20: float | None      # [0, 1] or None if no closes
    strategy_pnl_z_last_20: float | None         # z of most-recent pnl vs last-20 mean/std
    hours_since_last_signal: float | None        # None if no prior signal
    bars_since_last_trade_close: float | None    # None if no prior close (in hours as proxy)


class StrategyHistoryCache:
    """In-memory ring buffers per strategy.

    Thread safety: the engine is single-threaded on the signal hot path,
    so no locking is needed. If that ever changes, add a per-strategy
    asyncio.Lock around `record_*` and `stats_for`.
    """

    def __init__(self, window: int = 20) -> None:
        if window < 1:
            raise ValueError(f"window must be ≥ 1, got {window}")
        self._window = int(window)
        # Deque of realized_pnl floats, most recent at right
        self._pnls: dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._window)
        )
        # Deque of meta_label (0/1) floats, same length as pnls
        self._labels: dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._window)
        )
        # Last signal ts_ms per strategy (regardless of close status)
        self._last_signal_ts_ms: dict[str, int] = {}
        # Last close ts_ms per strategy (only set when close lands)
        self._last_close_ts_ms: dict[str, int] = {}

    # ── Update hooks ────────────────────────────────────────────────

    def record_signal(self, strategy: str, ts_ms: int) -> None:
        """Called whenever a strategy emits a signal (before close)."""
        if not strategy:
            return
        if ts_ms > 0:
            self._last_signal_ts_ms[strategy] = int(ts_ms)

    def record_close(
        self,
        strategy: str,
        pnl: float,
        meta_label: int | float | None,
        ts_ms: int,
    ) -> None:
        """Called whenever a strategy closes a trade.

        `meta_label` is the binary outcome from the triple-barrier labeler
        (1 = profit target hit or positive realized pnl, 0 = loss / time
        barrier). May be None if the labeler hasn't scored it yet.
        """
        if not strategy:
            return
        try:
            self._pnls[strategy].append(float(pnl))
        except (TypeError, ValueError):
            return
        if meta_label is not None:
            try:
                self._labels[strategy].append(1.0 if float(meta_label) > 0.5 else 0.0)
            except (TypeError, ValueError):
                pass
        if ts_ms > 0:
            self._last_close_ts_ms[strategy] = int(ts_ms)

    # ── Read path (hot) ─────────────────────────────────────────────

    def stats_for(self, strategy: str, now_ms: int) -> StrategyStats:
        """Compute derived stats for one strategy at a given time."""
        if not strategy:
            return StrategyStats(None, None, None, None)

        pnls = self._pnls.get(strategy)
        labels = self._labels.get(strategy)

        # win rate from labels (preferred) or fallback to pnl > 0 from pnls
        win_rate: float | None = None
        if labels and len(labels) > 0:
            win_rate = sum(labels) / len(labels)
        elif pnls and len(pnls) > 0:
            win_rate = sum(1.0 for p in pnls if p > 0) / len(pnls)

        # z-score of the MOST-RECENT pnl vs the window mean/std
        pnl_z: float | None = None
        if pnls and len(pnls) >= 3:
            mean = sum(pnls) / len(pnls)
            var = sum((p - mean) ** 2 for p in pnls) / len(pnls)
            std = math.sqrt(var)
            if std > 0:
                # Use most-recent pnl (rightmost) — this is the signal the
                # model wants to know: "how unusual was the last close?"
                pnl_z = (pnls[-1] - mean) / std

        # Time gap since last signal
        hrs_since_signal: float | None = None
        last_sig_ts = self._last_signal_ts_ms.get(strategy)
        if last_sig_ts is not None and now_ms > last_sig_ts:
            hrs_since_signal = (now_ms - last_sig_ts) / 3_600_000.0

        # Time gap since last trade close (in "bars" — hours as proxy
        # because we don't know the caller's bar timeframe at feature
        # build time)
        bars_since_close: float | None = None
        last_close_ts = self._last_close_ts_ms.get(strategy)
        if last_close_ts is not None and now_ms > last_close_ts:
            bars_since_close = (now_ms - last_close_ts) / 3_600_000.0

        return StrategyStats(
            strategy_win_rate_last_20=win_rate,
            strategy_pnl_z_last_20=pnl_z,
            hours_since_last_signal=hrs_since_signal,
            bars_since_last_trade_close=bars_since_close,
        )

    # ── Introspection (for tests + dashboards) ──────────────────────

    def n_recorded(self, strategy: str) -> int:
        return len(self._pnls.get(strategy, ()))

    def reset(self, strategy: str | None = None) -> None:
        """Clear history for one strategy (or all if None)."""
        if strategy is None:
            self._pnls.clear()
            self._labels.clear()
            self._last_signal_ts_ms.clear()
            self._last_close_ts_ms.clear()
            return
        self._pnls.pop(strategy, None)
        self._labels.pop(strategy, None)
        self._last_signal_ts_ms.pop(strategy, None)
        self._last_close_ts_ms.pop(strategy, None)
