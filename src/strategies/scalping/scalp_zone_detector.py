"""Gold Scalp Zone Detector — Computes market regime smoothness and directional quality.

Scores rolling 1-minute candle windows on:
1. Kaufman Efficiency Ratio (ER) — net travel / total travel (path efficiency)
2. Linearity / Trendline R² — how cleanly a straight line fits the leg
3. Directional Persistence — percentage of bars closing in the leg direction
4. ATR-Relative Displacement — leg move scaled by current 1m volatility
5. Smoothness & Body Quality — penalty for extreme range variance (CV) and micro-wicking
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ScalpZoneResult:
    timestamp_ms: int
    direction: Literal["LONG", "SHORT"]
    composite_score: float         # 0.0 to 100.0
    is_scalpable: bool            # True if composite_score >= threshold
    net_move_dollars: float
    atr_norm_move: float          # net move / ATR_14
    er: float                     # Kaufman Efficiency Ratio (0.0 to 1.0)
    r_squared: float              # Linear fit R² (0.0 to 1.0)
    directional_ratio: float      # % of bars in leg direction
    cv_range: float               # Range Coefficient of Variation
    median_body_pct: float        # Median body/range ratio
    current_close: float
    atr_14: float


class ScalpZoneDetector:
    """Real-time detector that evaluates rolling 1-minute candle windows."""

    def __init__(
        self,
        window_size: int = 10,
        min_score_threshold: float = 80.0,
        atr_window: int = 14,
    ):
        self.window_size = window_size
        self.min_score_threshold = min_score_threshold
        self.atr_window = atr_window

        # Buffer of raw candles: deque of dicts {'open', 'high', 'low', 'close', 'timestamp'}
        self._buffer: deque[dict] = deque(maxlen=max(60, window_size + atr_window + 5))
        self._tr_buffer: deque[float] = deque(maxlen=atr_window)
        self._prev_close: float | None = None

    def update(self, candle: dict) -> ScalpZoneResult | None:
        """Update with a new closed candle and compute scalp zone metrics.

        Args:
            candle: Dict with keys 'open', 'high', 'low', 'close', 'timestamp'.
        """
        c_open = float(candle["open"])
        c_high = float(candle["high"])
        c_low = float(candle["low"])
        c_close = float(candle["close"])
        ts = int(candle["timestamp"])

        # Update ATR_14
        if self._prev_close is not None:
            tr = max(
                c_high - c_low,
                abs(c_high - self._prev_close),
                abs(c_low - self._prev_close),
            )
            self._tr_buffer.append(tr)
        else:
            self._tr_buffer.append(c_high - c_low)
        self._prev_close = c_close

        self._buffer.append({
            "open": c_open,
            "high": c_high,
            "low": c_low,
            "close": c_close,
            "timestamp": ts,
        })

        if len(self._buffer) < self.window_size:
            return None

        # Slice window
        window = list(self._buffer)[-self.window_size:]
        return self._evaluate_window(window)

    def _evaluate_window(self, window: list[dict]) -> ScalpZoneResult:
        n = len(window)
        opens = np.array([b["open"] for b in window], dtype=float)
        highs = np.array([b["high"] for b in window], dtype=float)
        lows = np.array([b["low"] for b in window], dtype=float)
        closes = np.array([b["close"] for b in window], dtype=float)
        ts = window[-1]["timestamp"]

        # ATR calculation
        if len(self._tr_buffer) > 0:
            atr = float(np.mean(self._tr_buffer))
        else:
            atr = float(np.mean(highs - lows))
        atr_safe = max(atr, 0.20)  # floor to prevent div by zero

        ranges = highs - lows
        ranges_safe = np.where(ranges > 0, ranges, 1e-6)
        bodies = np.abs(closes - opens)
        body_pcts = bodies / ranges_safe
        med_body_pct = float(np.median(body_pcts))

        net_move = float(closes[-1] - closes[0])
        abs_net_move = abs(net_move)
        is_down = net_move < 0
        direction: Literal["LONG", "SHORT"] = "SHORT" if is_down else "LONG"

        # 1. Directional consistency ratio (0 to 1.0)
        if is_down:
            dir_count = np.sum(closes[1:] < closes[:-1])
        else:
            dir_count = np.sum(closes[1:] > closes[:-1])
        dir_ratio = float(dir_count / (n - 1))

        # 2. Range uniformity (Coefficient of Variation)
        mean_range = float(np.mean(ranges))
        std_range = float(np.std(ranges))
        cv = std_range / mean_range if mean_range > 0 else 1.0

        # 3. Linearity / R² (fit to a straight trendline)
        x = np.arange(n, dtype=float)
        y = closes
        if np.std(y) > 1e-6:
            corr = np.corrcoef(x, y)[0, 1]
            r2 = float(corr**2) if not np.isnan(corr) else 0.0
        else:
            r2 = 0.0

        # 4. Kaufman Efficiency Ratio (ER)
        path_len = float(np.sum(np.abs(closes[1:] - closes[:-1])))
        er = float(abs_net_move / path_len) if path_len > 0 else 0.0

        # 5. ATR-Relative Displacement
        atr_norm_move = abs_net_move / atr_safe

        # --- Weighted Composite Scoring (0 to 100) ---
        # Component 1: Kaufman ER (0 to 25 pts) — measures path straightness
        er_score = min(25.0, (er / 0.80) * 25.0)

        # Component 2: Linearity / R² (0 to 25 pts) — measures trendline fit
        r2_score = min(25.0, (r2 / 0.90) * 25.0)

        # Component 3: Directional Persistence (0 to 20 pts) — candle-by-candle drift
        dir_score = min(20.0, (dir_ratio / 0.80) * 20.0)

        # Component 4: ATR-Normalized Displacement (0 to 15 pts) — target >= 2.5x 1m ATR
        move_score = min(15.0, (atr_norm_move / 2.50) * 15.0)

        # Component 5: Smoothness & Body Quality (0 to 15 pts)
        cv_score = max(0.0, min(7.5, (1.0 - cv) * 7.5))
        body_score = min(7.5, (med_body_pct / 0.75) * 7.5)
        smooth_score = cv_score + body_score

        composite_score = round(er_score + r2_score + dir_score + move_score + smooth_score, 2)
        is_scalpable = composite_score >= self.min_score_threshold

        return ScalpZoneResult(
            timestamp_ms=ts,
            direction=direction,
            composite_score=composite_score,
            is_scalpable=is_scalpable,
            net_move_dollars=round(abs_net_move, 2),
            atr_norm_move=round(atr_norm_move, 2),
            er=round(er, 3),
            r_squared=round(r2, 3),
            directional_ratio=round(dir_ratio, 3),
            cv_range=round(cv, 3),
            median_body_pct=round(med_body_pct, 3),
            current_close=round(closes[-1], 3),
            atr_14=round(atr_safe, 3),
        )
