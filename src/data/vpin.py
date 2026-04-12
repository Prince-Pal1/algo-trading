"""Volume-Synchronized Probability of Informed Trading (VPIN).

Reference: Easley, Lopez de Prado, O'Hara (2012) — "Flow Toxicity and Liquidity"
Applied: ScienceDirect 2025 — VPIN > 0.7 predicts imminent crypto price jumps.

This implementation uses Bulk Volume Classification (BVC) to estimate buy/sell
volume from OHLCV candles, since we don't have tick-level trade data in backtests.

BVC: buy_volume = volume * CDF(Z) where Z = (close - open) / (high - low)
This approximates the true trade flow using the price bar's direction and range.
"""

from __future__ import annotations

from collections import deque

import numpy as np
from scipy import stats


class VPINCalculator:
    """Calculate VPIN from OHLCV candle data using Bulk Volume Classification.

    Usage:
        vpin_calc = VPINCalculator(avg_daily_volume=1000, n_buckets=50)
        for candle in candles:
            vpin = vpin_calc.update_from_candle(
                open=candle['open'], high=candle['high'],
                low=candle['low'], close=candle['close'],
                volume=candle['volume'],
            )
            if vpin is not None and vpin > 0.7:
                print("TOXIC FLOW — pause trading")
    """

    def __init__(
        self,
        avg_daily_volume: float | None = None,
        n_buckets: int = 50,
        bucket_size: float | None = None,
    ):
        """
        Args:
            avg_daily_volume: Average daily volume. Used to auto-size buckets
                              (bucket_size = avg_daily_volume / 50).
            n_buckets: Number of buckets for VPIN window (default 50).
            bucket_size: Override bucket size directly. If None, computed from avg_daily_volume.
        """
        self.n_buckets = n_buckets
        if bucket_size is not None:
            self.bucket_size = bucket_size
        elif avg_daily_volume is not None:
            self.bucket_size = avg_daily_volume / n_buckets
        else:
            self.bucket_size = 0  # Will auto-calibrate on first update
            self._auto_calibrate = True
            self._volume_buffer: list[float] = []

        self._buckets: deque[float] = deque(maxlen=n_buckets)
        self._current_bucket_volume = 0.0
        self._current_bucket_buy_volume = 0.0
        self._last_vpin: float = 0.0
        self._auto_calibrate = bucket_size is None and avg_daily_volume is None

    def _classify_volume(
        self, open_: float, high: float, low: float, close: float, volume: float
    ) -> tuple[float, float]:
        """Bulk Volume Classification: estimate buy/sell volume from a candle.

        Returns (buy_volume, sell_volume).
        """
        price_range = high - low
        if price_range < 1e-10:
            # Doji — split evenly
            return volume * 0.5, volume * 0.5

        # Z-score of price movement within the bar
        z = (close - open_) / price_range
        # CDF gives probability that the trade was buyer-initiated
        buy_pct = stats.norm.cdf(z)
        buy_vol = volume * buy_pct
        sell_vol = volume * (1 - buy_pct)
        return buy_vol, sell_vol

    def update_from_candle(
        self,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> float | None:
        """Process one OHLCV candle and return VPIN if a bucket completes.

        Returns:
            VPIN value (0-1) when bucket completes, None if no bucket completed.
        """
        # Auto-calibrate bucket size from first 24 candles (assume 1h → 1 day)
        if self._auto_calibrate:
            self._volume_buffer.append(volume)
            if len(self._volume_buffer) >= 24:
                daily_vol = sum(self._volume_buffer[:24])
                self.bucket_size = daily_vol / self.n_buckets
                self._auto_calibrate = False
            else:
                return None

        if self.bucket_size <= 0:
            return None

        buy_vol, sell_vol = self._classify_volume(open_, high, low, close, volume)
        total_vol = buy_vol + sell_vol

        remaining_buy = buy_vol
        remaining_sell = sell_vol
        remaining_total = total_vol
        vpin_result = None

        while remaining_total > 1e-10:
            space = self.bucket_size - self._current_bucket_volume
            fill = min(remaining_total, space)

            # Proportional fill
            if remaining_total > 0:
                buy_fill = fill * (remaining_buy / remaining_total)
            else:
                buy_fill = 0

            self._current_bucket_volume += fill
            self._current_bucket_buy_volume += buy_fill

            remaining_buy -= buy_fill
            remaining_sell -= (fill - buy_fill)
            remaining_total -= fill

            if self._current_bucket_volume >= self.bucket_size - 1e-10:
                # Bucket complete
                if self.bucket_size > 0:
                    buy_pct = self._current_bucket_buy_volume / self.bucket_size
                    order_imbalance = abs(buy_pct - 0.5) * 2  # Normalize to 0-1
                    self._buckets.append(order_imbalance)

                self._current_bucket_volume = 0.0
                self._current_bucket_buy_volume = 0.0

                if len(self._buckets) >= self.n_buckets:
                    self._last_vpin = float(np.mean(self._buckets))
                    vpin_result = self._last_vpin

        return vpin_result

    @property
    def current_vpin(self) -> float:
        """Get the most recently calculated VPIN value."""
        return self._last_vpin

    @property
    def ready(self) -> bool:
        """Whether enough buckets have been filled for a valid VPIN."""
        return len(self._buckets) >= self.n_buckets
