"""RSI(2) Mean Reversion — verification strategy.

Used to cross-validate the backtest engine against a reference implementation.
NOT for live trading. Rules are intentionally simple for unambiguous testing.

Rules (Larry Connors RSI(2), long-only):
    Entry: RSI(2) < 10 → go LONG
    Exit:  close > SMA(5) → CLOSE
    No SL/TP (signal-only exits keep comparison clean)
"""

from __future__ import annotations

from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction

import pandas as pd


class RSI2MeanRevStrategy(BaseStrategy):
    """RSI(2) mean reversion — long-only, no SL/TP."""

    def __init__(
        self,
        rsi_entry: float = 10.0,
        risk_pct: float = 0.02,
    ):
        super().__init__(
            name="rsi2_verification",
            markets=["BTCUSDT"],
            timeframe="1h",
        )
        self.rsi_entry = rsi_entry
        self.risk_pct = risk_pct

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        rsi = features.get("RSI_2")
        sma = features.get("SMA_5")
        close = features.get("close")

        if rsi is None or sma is None or close is None:
            return None
        if pd.isna(rsi) or pd.isna(sma):
            return None

        # Entry: RSI(2) < threshold and not already in position
        if self._position == "FLAT" and rsi < self.rsi_entry:
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=1.0,
                strategy_name=self.name,
                timeframe=timeframe,
                risk_pct=self.risk_pct,
            )

        # Exit: close crosses above SMA(5)
        if self._position == "LONG" and close > sma:
            return Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=1.0,
                strategy_name=self.name,
                timeframe=timeframe,
            )

        return None
