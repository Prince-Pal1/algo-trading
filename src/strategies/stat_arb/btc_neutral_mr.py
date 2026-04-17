"""BTC-Neutral Residual Mean Reversion Strategy.

Removes BTC systemic risk from altcoin returns via rolling regression,
then trades mean reversion on the residual (idiosyncratic) component.

Reference: briplotnik/systematic-crypto-trading (GitHub/Medium). Reported Sharpe 2.3.

How it works:
    1. Rolling OLS: altcoin_ret = alpha + beta * btc_ret + residual
    2. Z-score the residual series
    3. LONG when z < -z_entry (altcoin undervalued after removing BTC exposure)
    4. SHORT when z > z_entry (altcoin overvalued)
    5. EXIT when z crosses back through ±z_exit (reverted)
    6. STOP when z exceeds ±z_stop (breakdown)

Requires: `ref_close` column in features (BTC close price, injected by backtest CLI).
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction


class BTCNeutralMRStrategy(BaseStrategy):
    """BTC-neutral residual mean reversion on altcoins."""
    fee_style = "arbitrage"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str,
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        regression_window: int = 60,
        z_entry: float = 2.0,
        z_exit: float = 0.5,
        z_stop: float = 3.5,
        atr_period: int = 14,
        sl_atr_mult: float = 2.0,
        max_hold_bars: int = 72,
        cooldown_bars: int = 5,
    ):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)

        self.regression_window = regression_window
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.z_stop = z_stop
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.max_hold_bars = max_hold_bars
        self.cooldown_bars = cooldown_bars

        # Internal state
        self._bars_since_exit: int = 999
        self._bars_in_position: int = 0
        self._atr_col = f"ATR_{atr_period}"

        # Rolling price buffers
        self._alt_closes: deque[float] = deque(maxlen=regression_window + 1)
        self._btc_closes: deque[float] = deque(maxlen=regression_window + 1)

    def _compute_z_score(self) -> float | None:
        """Compute z-score of the altcoin residual after removing BTC beta."""
        if len(self._alt_closes) < self.regression_window + 1:
            return None

        alt_prices = np.array(self._alt_closes)
        btc_prices = np.array(self._btc_closes)

        # Compute returns
        alt_ret = np.diff(np.log(alt_prices))
        btc_ret = np.diff(np.log(btc_prices))

        # Rolling OLS: alt_ret = alpha + beta * btc_ret + residual
        # Use last `regression_window` returns
        x = btc_ret[-self.regression_window:]
        y = alt_ret[-self.regression_window:]

        # Add intercept
        X = np.column_stack([np.ones(len(x)), x])
        try:
            # OLS via normal equations
            coeffs = np.linalg.lstsq(X, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return None

        # Residuals
        residuals = y - X @ coeffs

        # Z-score of latest residual
        std = residuals.std()
        if std < 1e-10:
            return None

        z = (residuals[-1] - residuals.mean()) / std
        return float(z)

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        # Tick counters
        if self._position == "FLAT":
            self._bars_since_exit += 1
        else:
            self._bars_in_position += 1

        close = features["close"]
        atr = features.get(self._atr_col)
        btc_close = features.get("ref_close")

        if btc_close is None or pd.isna(btc_close):
            return None
        if atr is None or pd.isna(atr):
            return None

        # Accumulate prices
        self._alt_closes.append(float(close))
        self._btc_closes.append(float(btc_close))

        # Compute z-score
        z = self._compute_z_score()
        if z is None:
            return None

        # ── EXIT LOGIC (check first) ──

        if self._position == "LONG":
            # Mean reversion target: z crosses back above -z_exit
            if z >= -self.z_exit:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "z_reversion", "z_score": round(z, 3)},
                )

            # Stop loss: z dives further (breakdown, not reverting)
            if z < -self.z_stop:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.8,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "z_stop", "z_score": round(z, 3)},
                )

            # Timeout
            if self._bars_in_position >= self.max_hold_bars:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.7,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "timeout", "z_score": round(z, 3)},
                )

        if self._position == "SHORT":
            # Mean reversion target: z drops back below z_exit
            if z <= self.z_exit:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "z_reversion", "z_score": round(z, 3)},
                )

            # Stop loss: z blows up further
            if z > self.z_stop:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.8,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "z_stop", "z_score": round(z, 3)},
                )

            # Timeout
            if self._bars_in_position >= self.max_hold_bars:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.7,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "timeout", "z_score": round(z, 3)},
                )

        # ── ENTRY FILTERS ──

        if self._bars_since_exit < self.cooldown_bars:
            return None

        # ── LONG: altcoin undervalued (z < -z_entry) ──
        if z < -self.z_entry and self._position != "LONG":
            stop_loss = close - self.sl_atr_mult * atr
            take_profit = close + self.sl_atr_mult * atr * 2

            risk = close - stop_loss
            if risk <= 0:
                return None

            confidence = min(1.0, abs(z) / self.z_stop)
            self._bars_in_position = 0

            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=max(0.5, confidence),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "z_score": round(z, 3),
                    "atr": round(float(atr), 4),
                },
            )

        # ── SHORT: altcoin overvalued (z > z_entry) ──
        if z > self.z_entry and self._position != "SHORT":
            stop_loss = close + self.sl_atr_mult * atr
            take_profit = close - self.sl_atr_mult * atr * 2

            risk = stop_loss - close
            if risk <= 0:
                return None

            confidence = min(1.0, abs(z) / self.z_stop)
            self._bars_in_position = 0

            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=max(0.5, confidence),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "z_score": round(z, 3),
                    "atr": round(float(atr), 4),
                },
            )

        return None
