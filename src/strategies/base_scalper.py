"""ScalperStrategy — BaseStrategy subclass with multi-bar helpers.

Solves the two recurring pain points from Tier 5 M1 revival (task #100):

1. **No multi-bar history buffer on BaseStrategy.** Scalpers that need to
   look back N bars had to reinvent a deque each time. ScalperStrategy
   carries a rolling buffer of the last N feature Series automatically.

2. **No direct access to sub-bar data during signal generation.**
   The LeveragedBacktestEngine (task #102) now attaches `intrabar_sub_bars`
   to the row when it's running with an M1PathModel — this class exposes
   a helper that reads those sub-bars safely.

Helper semantics:
- `multi_bar_velocity(n)` — cumulative signed close delta over last n bars,
  normalized by ATR_20. Returns None if insufficient history or ATR missing.
- `is_multi_bar_burst(n, mult)` — True iff |velocity| > mult.
- `is_volume_confirmed(threshold, n)` — True iff last bar's volume >
  threshold × median(last n-1 bars). Filters low-conviction breakouts.
- `broke_n_bar_high(n)` / `broke_n_bar_low(n)` — structural break helpers.
- `intrabar_max_velocity(features)` — largest abs(close - open) across
  M1 sub-bars of the current bar. Returns None if sub-bars not attached.

All helpers return None / False on insufficient data — strategies just
short-circuit, no exception.
"""

from __future__ import annotations

import statistics
from collections import deque

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.types import Signal


class ScalperStrategy(BaseStrategy):
    """BaseStrategy subclass with rolling history + scalper primitives.

    Subclasses override `on_features(symbol, timeframe, features)` same
    as regular BaseStrategy — the history buffer is maintained by this
    class's `process()` override, so subclasses can just call
    `self.multi_bar_velocity(3)` etc. inside their entry logic.
    """
    fee_style = "scalping"

    def __init__(
        self,
        *args,
        history_size: int = 20,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._history_size = int(history_size)
        self._history: deque[pd.Series] = deque(maxlen=self._history_size)

    # ── Process override to maintain the history buffer ────────────────

    def process(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        """Append to history before delegating to on_features.

        Note: we append BEFORE on_features so that helpers like
        `multi_bar_velocity(3)` can see the current bar as `_history[-1]`.
        """
        self._history.append(features)
        return super().process(symbol, timeframe, features)

    # ── Multi-bar velocity helpers ─────────────────────────────────────

    def multi_bar_velocity(self, n: int = 3) -> float | None:
        """Cumulative signed close delta over last n bars, normalized by ATR_20.

        Returns None if the history has fewer than n+1 bars or ATR_20
        is missing / zero. Otherwise:

            velocity_n = (close[-1] - close[-n-1]) / (n * ATR_20[-1])

        At n=3 and ATR_20=10, a value of +2.0 means the close has moved
        +60 units (2 × 3 × 10) in the last 3 bars — a strong directional
        move. A value near 0 means noise.
        """
        if len(self._history) < n + 1:
            return None
        recent = list(self._history)[-(n + 1):]
        first_close = float(recent[0]["close"])
        last_close = float(recent[-1]["close"])
        atr = recent[-1].get("ATR_20")
        if atr is None or pd.isna(atr):
            return None
        atr_f = float(atr)
        if atr_f <= 0:
            return None
        return (last_close - first_close) / (n * atr_f)

    def is_multi_bar_burst(self, n: int = 3, mult: float = 1.5) -> bool:
        """True iff abs(multi_bar_velocity(n)) > mult.

        At default n=3 / mult=1.5, this fires when the close has moved
        more than 4.5 × ATR over 3 bars. On a scalping timeframe that's
        a clear directional burst, not noise.
        """
        v = self.multi_bar_velocity(n)
        return v is not None and abs(v) > mult

    # ── Volume confirmation helpers ────────────────────────────────────

    def is_volume_confirmed(
        self,
        threshold: float = 1.5,
        n: int = 20,
    ) -> bool:
        """Last bar's volume > threshold × median(last n-1 bars).

        At default threshold=1.5 / n=20, this fires when the current
        bar had volume 50% above the rolling 19-bar median — indicates
        a conviction move, not a quiet drift.

        Returns False on:
            - insufficient history (need at least n bars)
            - current volume is 0 (bad data)
            - median volume is 0 (all-zero history → divide-by-zero guard)
        """
        if len(self._history) < n:
            return False
        vols = [float(b.get("volume", 0.0)) for b in list(self._history)[-n:]]
        if not vols or vols[-1] == 0.0:
            return False
        prior = vols[:-1]
        if not prior:
            return False
        median = statistics.median(prior)
        return median > 0 and vols[-1] > threshold * median

    # ── Structural break helpers ───────────────────────────────────────

    def broke_n_bar_high(self, n: int = 10) -> bool:
        """Current bar's high is strictly greater than the prior n-bar high.

        Returns False if fewer than n+1 bars in history.
        """
        if len(self._history) < n + 1:
            return False
        prior = list(self._history)[-(n + 1):-1]
        prior_high = max(float(b["high"]) for b in prior)
        return float(self._history[-1]["high"]) > prior_high

    def broke_n_bar_low(self, n: int = 10) -> bool:
        """Current bar's low is strictly less than the prior n-bar low."""
        if len(self._history) < n + 1:
            return False
        prior = list(self._history)[-(n + 1):-1]
        prior_low = min(float(b["low"]) for b in prior)
        return float(self._history[-1]["low"]) < prior_low

    # ── Intrabar helpers (need M1PathModel in the engine) ─────────────

    @staticmethod
    def intrabar_max_velocity(features: pd.Series) -> float | None:
        """Largest absolute (close - open) across M1 sub-bars of this bar.

        Reads `features.get("intrabar_sub_bars")` which is populated by
        the LeveragedBacktestEngine when it's running against an
        M1PathModel. Each sub-bar is an `M1SubBar` named tuple with
        attributes open, high, low, close, volume.

        Returns None when sub-bars are not attached (e.g., strategy is
        running against the plain Brownian bridge path model, or the
        M1 lookup had no coverage for this M5 bar's timestamp).
        """
        sub_bars = features.get("intrabar_sub_bars")
        if sub_bars is None:
            return None
        try:
            if len(sub_bars) == 0:
                return None
        except TypeError:
            return None
        return max(abs(float(sb.close) - float(sb.open)) for sb in sub_bars)

    @staticmethod
    def intrabar_body_direction(features: pd.Series) -> int | None:
        """Return the NET direction across M1 sub-bars: +1 (up) / -1 (down) / 0.

        Defined as sign(sum of (close - open) over all sub-bars). Useful
        as a directional confirmation filter: if the sub-bars net positive,
        a LONG signal is more credible than one fighting the net drift.
        """
        sub_bars = features.get("intrabar_sub_bars")
        if not sub_bars:
            return None
        net = sum(float(sb.close) - float(sb.open) for sb in sub_bars)
        if net > 0:
            return 1
        if net < 0:
            return -1
        return 0

    @staticmethod
    def intrabar_volume_total(features: pd.Series) -> float | None:
        """Sum of volume across all M1 sub-bars of this bar."""
        sub_bars = features.get("intrabar_sub_bars")
        if not sub_bars:
            return None
        return sum(float(sb.volume) for sb in sub_bars)
