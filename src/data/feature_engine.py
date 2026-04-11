"""Feature Engine — computes technical indicators from candles using the `ta` library.

Maintains a rolling pandas DataFrame per symbol/timeframe and computes indicators
on each new candle. Strategies read indicator values from this engine.

Usage:
    engine = FeatureEngine(indicators=["ema_9", "ema_21", "rsi_7", "bbands_20"])
    engine.on_features = my_handler
    await engine.handle_candle(candle)
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

import pandas as pd
from ta.trend import EMAIndicator, SMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice

from src.utils.logger import get_logger
from src.utils.types import Candle

log = get_logger("feature_engine")

OnFeatures = Callable[[str, str, pd.Series], Coroutine[Any, Any, None]]

# Maximum candles to keep in memory per symbol/timeframe
MAX_BUFFER_SIZE = 500


# ── Indicator Definitions ──────────────────────────────────────────────────

def _compute_indicators(df: pd.DataFrame, indicator_list: list[str]) -> pd.DataFrame:
    """Compute requested indicators on a DataFrame with OHLCV columns."""
    n = len(df)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    for ind in indicator_list:
        parts = ind.split("_")
        name = parts[0]
        length = int(parts[1]) if len(parts) > 1 else None

        # Skip if not enough data for the indicator window
        min_rows = (length or 26) + 5  # +5 buffer for warmup
        if n < min_rows:
            continue

        if name == "ema" and length:
            col = f"EMA_{length}"
            if col not in df.columns:
                df[col] = EMAIndicator(close=close, window=length).ema_indicator()

        elif name == "sma" and length:
            col = f"SMA_{length}"
            if col not in df.columns:
                df[col] = SMAIndicator(close=close, window=length).sma_indicator()

        elif name == "rsi" and length:
            col = f"RSI_{length}"
            if col not in df.columns:
                df[col] = RSIIndicator(close=close, window=length).rsi()

        elif name == "bbands" and length:
            upper_col = f"BBU_{length}"
            if upper_col not in df.columns:
                bb = BollingerBands(close=close, window=length)
                df[upper_col] = bb.bollinger_hband()
                df[f"BBM_{length}"] = bb.bollinger_mavg()
                df[f"BBL_{length}"] = bb.bollinger_lband()

        elif name == "macd":
            if "MACD" not in df.columns:
                macd = MACD(close=close)
                df["MACD"] = macd.macd()
                df["MACD_signal"] = macd.macd_signal()
                df["MACD_hist"] = macd.macd_diff()

        elif name == "vwap":
            if "VWAP" not in df.columns:
                try:
                    df["VWAP"] = VolumeWeightedAveragePrice(
                        high=high, low=low, close=close, volume=volume,
                    ).volume_weighted_average_price()
                except Exception:
                    pass  # VWAP needs volume, may fail on sparse data

        elif name == "atr" and length:
            col = f"ATR_{length}"
            if col not in df.columns:
                df[col] = AverageTrueRange(
                    high=high, low=low, close=close, window=length,
                ).average_true_range()

        elif name == "adx" and length:
            col = f"ADX_{length}"
            if col not in df.columns:
                adx = ADXIndicator(high=high, low=low, close=close, window=length)
                df[col] = adx.adx()
                df[f"DI+_{length}"] = adx.adx_pos()
                df[f"DI-_{length}"] = adx.adx_neg()

        elif name == "stoch" and length:
            col = f"STOCHk_{length}"
            if col not in df.columns:
                stoch = StochasticOscillator(high=high, low=low, close=close, window=length)
                df[col] = stoch.stoch()
                df[f"STOCHd_{length}"] = stoch.stoch_signal()

    return df


# ── Feature Engine ─────────────────────────────────────────────────────────


class FeatureEngine:
    """Maintains rolling DataFrames and computes indicators on each new candle."""

    def __init__(self, indicators: list[str] | None = None):
        self.indicators = indicators or ["ema_9", "ema_21", "rsi_7"]

        # key: (symbol, timeframe) -> DataFrame
        self._buffers: dict[tuple[str, str], pd.DataFrame] = defaultdict(
            lambda: pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"])
        )

        # Callback: emits (symbol, timeframe, latest_row_with_indicators)
        self.on_features: OnFeatures | None = None
        self._update_count = 0

    async def handle_candle(self, candle: Candle) -> None:
        """Add candle to buffer, compute indicators, emit features."""
        key = (candle.symbol, candle.timeframe)
        df = self._buffers[key]

        new_row = pd.DataFrame([{
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "timestamp": candle.timestamp,
        }])
        df = pd.concat([df, new_row], ignore_index=True)

        # Trim buffer
        if len(df) > MAX_BUFFER_SIZE:
            df = df.iloc[-MAX_BUFFER_SIZE:].reset_index(drop=True)

        # Compute indicators
        df = _compute_indicators(df, self.indicators)
        self._buffers[key] = df

        # Emit latest row with all indicator values
        if self.on_features and len(df) > 0:
            latest = df.iloc[-1]
            self._update_count += 1
            await self.on_features(candle.symbol, candle.timeframe, latest)

    def get_dataframe(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Get the full buffered DataFrame for a symbol/timeframe."""
        return self._buffers.get((symbol, timeframe), pd.DataFrame())

    def get_latest(self, symbol: str, timeframe: str) -> pd.Series | None:
        """Get the most recent row with indicators."""
        df = self._buffers.get((symbol, timeframe))
        if df is not None and len(df) > 0:
            return df.iloc[-1]
        return None

    @property
    def stats(self) -> dict:
        return {
            "updates": self._update_count,
            "buffers": {f"{s}_{tf}": len(df) for (s, tf), df in self._buffers.items()},
        }
