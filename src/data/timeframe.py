"""Timeframe utilities — alternate-TF aggregation for strategies.

The SWIFT strategy (Pine Script port — task #103) needs to run at one
timeframe but compute indicators on a HIGHER timeframe. Pine Script
does this via `request.security(sym, stratRes, ...)`. In our Python
backtest we aggregate base-TF bars into alt-TF bars incrementally as
they arrive.

Public API:
    AlternateTimeframeBuilder(multiplier: int)
        feed(bar: pd.Series) -> pd.Series | None
        history(n: int) -> list[dict]
        reset() -> None

Semantics:
- `feed(bar)` appends one base-TF bar to the current pending alt-TF
  bar. Returns None if the alt-TF bar is still forming. When the
  multiplier-th base bar arrives, `feed` closes the pending alt bar,
  appends it to history, and returns the closed bar as a pd.Series
  with OHLCV columns matching the input bars.
- `history(n)` returns the most recent n closed alt-TF bars as a list
  of dicts, each with open/high/low/close/volume/timestamp keys.
- `reset()` clears pending state and history.

This is inherently stateful — one builder per strategy instance.
Deterministic: given the same sequence of base bars, always produces
the same alt-TF bars.
"""

from __future__ import annotations

from collections import deque

import pandas as pd


class AlternateTimeframeBuilder:
    """Builds higher-timeframe bars incrementally from base-timeframe input.

    For a chart timeframe of M5 and a multiplier of 8, this builds H40
    (8 × 5min = 40 min) bars. Strategies call `feed(row)` on every base
    bar; when an alt bar is complete (every 8th base bar), the method
    returns the closed alt bar, otherwise None.
    """

    def __init__(self, multiplier: int, history_size: int = 100) -> None:
        if multiplier <= 0:
            raise ValueError(f"multiplier must be > 0, got {multiplier}")
        self._multiplier = int(multiplier)
        self._history: deque[dict] = deque(maxlen=int(history_size))
        # Pending alt-bar state
        self._pending_count: int = 0
        self._pending_open: float | None = None
        self._pending_high: float = float("-inf")
        self._pending_low: float = float("inf")
        self._pending_close: float = 0.0
        self._pending_volume: float = 0.0
        self._pending_open_ts: int = 0

    def feed(self, bar: pd.Series) -> pd.Series | None:
        """Append one base-TF bar. Return a closed alt-TF bar on completion.

        Arg:
            bar: a pd.Series with open/high/low/close columns. volume
                and timestamp are used if present.

        Returns:
            pd.Series with keys {open, high, low, close, volume,
            timestamp} when the alt-TF bar is complete. Otherwise None.
        """
        b_open = float(bar["open"])
        b_high = float(bar["high"])
        b_low = float(bar["low"])
        b_close = float(bar["close"])
        b_volume = float(bar.get("volume", 0.0) or 0.0)
        b_ts = int(bar.get("timestamp", 0) or 0)

        if self._pending_count == 0:
            # First bar of a new alt bar
            self._pending_open = b_open
            self._pending_high = b_high
            self._pending_low = b_low
            self._pending_close = b_close
            self._pending_volume = b_volume
            self._pending_open_ts = b_ts
        else:
            self._pending_high = max(self._pending_high, b_high)
            self._pending_low = min(self._pending_low, b_low)
            self._pending_close = b_close
            self._pending_volume += b_volume

        self._pending_count += 1

        if self._pending_count >= self._multiplier:
            closed_bar = pd.Series({
                "open": self._pending_open,
                "high": self._pending_high,
                "low": self._pending_low,
                "close": self._pending_close,
                "volume": self._pending_volume,
                "timestamp": self._pending_open_ts,
            })
            self._history.append(dict(closed_bar))
            # Reset pending
            self._pending_count = 0
            self._pending_open = None
            self._pending_high = float("-inf")
            self._pending_low = float("inf")
            self._pending_close = 0.0
            self._pending_volume = 0.0
            self._pending_open_ts = 0
            return closed_bar

        return None

    def history(self, n: int | None = None) -> list[dict]:
        """Return the last n closed alt-TF bars as dicts.

        If n is None, returns ALL stored history (up to history_size).
        Each dict has keys: open, high, low, close, volume, timestamp.
        """
        h = list(self._history)
        if n is None:
            return h
        return h[-n:]

    def has_history(self, n: int) -> bool:
        """True iff at least n closed alt-TF bars are in the buffer."""
        return len(self._history) >= n

    def history_as_dataframe(self, n: int | None = None) -> pd.DataFrame:
        """Convenience: return history as a pandas DataFrame."""
        rows = self.history(n)
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"])
        return pd.DataFrame(rows)

    def reset(self) -> None:
        """Clear all state — pending + history."""
        self._history.clear()
        self._pending_count = 0
        self._pending_open = None
        self._pending_high = float("-inf")
        self._pending_low = float("inf")
        self._pending_close = 0.0
        self._pending_volume = 0.0
        self._pending_open_ts = 0
