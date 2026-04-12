"""Walk-Forward EMA Momentum Strategy.

Reference: arXiv:2602.10785 — "Walk-Forward Optimization of EMA Crossover"
GitHub: tmr-crypto/wf_optim_crypto_analysis

CRITICAL: Only 60-min timeframe is viable. Sub-30-min all have negative Sharpe
after fees (1-min Sharpe = -12.71). Paper proves static EMA fails but dynamic
walk-forward optimization achieves Sharpe 1.252.

Design:
    - Stop-and-Reverse (SAR): always in a position (long or short)
    - Every `reoptim_interval` candles, re-optimizes fast/slow EMA lengths
      over a training window to maximize net Sharpe (after simulated fees)
    - Uses the optimized params for the next `reoptim_interval` candles
    - Adapts to changing market regimes automatically
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction


class WalkForwardEMA(BaseStrategy):
    """Dynamic EMA crossover with rolling walk-forward parameter optimization."""

    def __init__(
        self,
        name: str = "wf_ema",
        markets: list[str] | None = None,
        timeframe: str = "1h",
        *,
        # Walk-forward windows (in candles, not days — for 1h: 168 candles = 7 days)
        train_window: int = 168,       # 7 days of 1h candles for training
        reoptim_interval: int = 672,   # 28 days — re-optimize every 4 weeks
        # EMA search space
        fast_min: int = 5,
        fast_max: int = 50,
        fast_step: int = 5,
        slow_min: int = 20,
        slow_max: int = 200,
        slow_step: int = 10,
        # Fee model
        fee_per_trade: float = 0.001,  # 0.1% conservative (paper's assumption)
        # Stop loss (hard stop as safety net — SAR normally handles exits)
        hard_stop_pct: float = 0.05,   # 5% hard stop
        # Long-only mode (avoids SHORT leg — better in uptrending markets)
        long_only: bool = False,
    ):
        super().__init__(
            name=name,
            markets=markets or ["BTCUSDT"],
            timeframe=timeframe,
        )

        # Walk-forward config
        self.train_window = train_window
        self.reoptim_interval = reoptim_interval

        # Search grid
        self.fast_range = list(range(fast_min, fast_max + 1, fast_step))
        self.slow_range = list(range(slow_min, slow_max + 1, slow_step))

        # Fee model
        self.fee_per_trade = fee_per_trade

        # Hard stop
        self.hard_stop_pct = hard_stop_pct

        # Mode
        self.long_only = long_only

        # Current optimized params (defaults until first optimization)
        self.current_fast = 12
        self.current_slow = 26

        # Internal state
        self._close_history: deque[float] = deque(maxlen=max(slow_max + 50, train_window + 50))
        self._candle_count = 0
        self._last_optim_candle = -reoptim_interval  # trigger optimization on first eligible candle
        self._last_signal_direction: str | None = None  # "LONG" or "SHORT"

    def _optimize(self) -> tuple[int, int, float]:
        """Find best (fast, slow) EMA pair on the training window.

        Maximizes Sharpe ratio net of simulated trading fees.
        Returns (best_fast, best_slow, best_sharpe).
        """
        closes = np.array(self._close_history)
        # Use the most recent train_window candles
        if len(closes) < self.train_window:
            return self.current_fast, self.current_slow, 0.0

        train = closes[-self.train_window:]

        best_sharpe = -np.inf
        best_fast = self.current_fast
        best_slow = self.current_slow

        for fast in self.fast_range:
            for slow in self.slow_range:
                if fast >= slow:
                    continue
                if slow >= len(train) - 5:
                    continue  # not enough data for this slow period

                # Compute EMAs on training data
                ema_f = self._ema(train, fast)
                ema_s = self._ema(train, slow)

                # Signal: +1 when fast > slow, -1 otherwise
                signal = np.where(ema_f > ema_s, 1.0, -1.0)

                # Returns: position * price return (shifted by 1 for no lookahead)
                price_ret = np.diff(train) / train[:-1]
                position = signal[:-1]  # align with returns
                strat_ret = position * price_ret

                # Subtract fee on every position flip
                flips = np.abs(np.diff(signal))  # 0 or 2 on flip
                # flips has length len(signal)-1, align with strat_ret
                if len(flips) > len(strat_ret):
                    flips = flips[:len(strat_ret)]
                elif len(flips) < len(strat_ret):
                    flips = np.pad(flips, (0, len(strat_ret) - len(flips)))

                fee_cost = (flips > 0).astype(float) * self.fee_per_trade
                strat_ret = strat_ret - fee_cost

                # Sharpe ratio (annualized for hourly data: sqrt(8760))
                if len(strat_ret) > 10 and np.std(strat_ret) > 1e-10:
                    sharpe = np.mean(strat_ret) / np.std(strat_ret) * np.sqrt(8760)
                    if sharpe > best_sharpe:
                        best_sharpe = sharpe
                        best_fast = fast
                        best_slow = slow

        return best_fast, best_slow, best_sharpe

    @staticmethod
    def _ema(data: np.ndarray, span: int) -> np.ndarray:
        """Fast EMA computation using exponential weighting."""
        alpha = 2.0 / (span + 1)
        result = np.empty_like(data, dtype=float)
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
        return result

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = features.get("close")
        if close is None or pd.isna(close):
            return None

        self._close_history.append(float(close))
        self._candle_count += 1

        # Need enough history for the slow EMA + warmup
        if len(self._close_history) < self.current_slow + 10:
            return None

        # Re-optimize if interval has elapsed
        if self._candle_count - self._last_optim_candle >= self.reoptim_interval:
            if len(self._close_history) >= self.train_window:
                old_fast, old_slow = self.current_fast, self.current_slow
                self.current_fast, self.current_slow, sharpe = self._optimize()
                self._last_optim_candle = self._candle_count
                if self.current_fast != old_fast or self.current_slow != old_slow:
                    # Params changed — log via metadata on next signal
                    pass

        # Compute current EMAs from history
        closes = np.array(self._close_history)
        ema_fast = self._ema(closes, self.current_fast)
        ema_slow = self._ema(closes, self.current_slow)

        curr_fast = ema_fast[-1]
        curr_slow = ema_slow[-1]

        # Need at least 2 data points for crossover
        if len(ema_fast) < 2:
            return None

        prev_fast = ema_fast[-2]
        prev_slow = ema_slow[-2]

        # Determine desired direction
        if curr_fast > curr_slow:
            desired = "LONG"
        else:
            desired = "SHORT"

        # Only emit signal on crossover (direction change) or first signal
        is_crossover = (prev_fast <= prev_slow and curr_fast > curr_slow) or \
                       (prev_fast >= prev_slow and curr_fast < curr_slow)

        meta = {"ema_fast": self.current_fast, "ema_slow": self.current_slow}

        if self.long_only:
            # Long-only: enter LONG on bullish crossover, CLOSE on bearish
            if is_crossover:
                if desired == "LONG" and self._position != "LONG":
                    self._last_signal_direction = "LONG"
                    return Signal(
                        symbol=symbol, action=SignalAction.LONG, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=close,
                        stop_loss=close * (1 - self.hard_stop_pct),
                        metadata=meta,
                    )
                elif desired == "SHORT" and self._position == "LONG":
                    self._last_signal_direction = None
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=close,
                        metadata={**meta, "reason": "bearish_crossover"},
                    )
            return None

        # SAR mode: always in market
        if self._last_signal_direction is None:
            # First signal or re-entry after CLOSE
            if desired == "LONG":
                self._last_signal_direction = "LONG"
                sl = close * (1 - self.hard_stop_pct)
            else:
                self._last_signal_direction = "SHORT"
                sl = close * (1 + self.hard_stop_pct)
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG if desired == "LONG" else SignalAction.SHORT,
                confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=close, stop_loss=sl, metadata=meta,
            )

        if is_crossover and desired != self._last_signal_direction:
            self._last_signal_direction = None
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=close,
                metadata={**meta, "reason": "crossover_exit"},
            )

        return None
