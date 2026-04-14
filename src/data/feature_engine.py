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

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator, SMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange, DonchianChannel
from ta.volume import VolumeWeightedAveragePrice

from src.utils.logger import get_logger
from src.utils.types import Candle

log = get_logger("feature_engine")

OnFeatures = Callable[[str, str, pd.Series], Coroutine[Any, Any, None]]

# Maximum candles to keep in memory per symbol/timeframe
MAX_BUFFER_SIZE = 500

# Rows used for indicator computation. Must be large enough for the biggest
# window to converge (donchian_120 needs 240+, EMA/RSI recursive indicators
# need ~2-3x window for <0.01% error). 250 covers all current indicators.
COMPUTE_WINDOW = 250


# ── Indicator Definitions ──────────────────────────────────────────────────


def _alma(
    series: pd.Series,
    length: int,
    offset: float = 0.85,
    sigma: float = 6.0,
) -> pd.Series:
    """Arnaud Legoux Moving Average — matches Pine Script's ta.alma exactly.

    ALMA is a Gaussian-weighted moving average where `offset` controls
    where the peak of the Gaussian sits along the window (0 = oldest,
    1 = newest) and `sigma` controls the width (higher sigma = narrower,
    more recent-biased).

    Pine Script default: offset=0.85, sigma=6. We match it exactly.

    Implementation: precompute weights once, then np.convolve. For each
    output position i, ALMA[i] = sum(w[k] * series[i-length+1+k]) for k
    in 0..length-1, divided by the sum of weights.
    """
    if length <= 0:
        return pd.Series(np.zeros(len(series)), index=series.index)
    m = offset * (length - 1)
    s = length / sigma
    weights = np.array([
        np.exp(-((k - m) ** 2) / (2 * s * s))
        for k in range(length)
    ])
    weights_sum = weights.sum()
    if weights_sum <= 0:
        return pd.Series(np.zeros(len(series)), index=series.index)
    weights = weights / weights_sum

    vals = series.to_numpy(dtype=float)
    n = len(vals)
    out = np.full(n, np.nan)
    for i in range(length - 1, n):
        out[i] = float(np.dot(weights, vals[i - length + 1:i + 1]))
    return pd.Series(out, index=series.index).fillna(0.0)


def _compute_indicators(df: pd.DataFrame, indicator_list: list[str]) -> pd.DataFrame:
    """Compute requested indicators on a DataFrame with OHLCV columns."""
    n = len(df)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    for ind in indicator_list:
        parts = ind.split("_")
        # Detect an integer suffix so multi-underscore names like
        # "volume_zscore_30" parse as name="volume_zscore", length=30,
        # while "body_pct" parses as name="body_pct", length=None.
        length: int | None = None
        if len(parts) > 1:
            try:
                length = int(parts[-1])
                name = "_".join(parts[:-1])
            except ValueError:
                name = ind
        else:
            name = ind

        # Skip if not enough data for the indicator window
        # ADX/Donchian/Stoch need ~2x window; others need ~window+5
        # Per-bar indicators (body_pct, close_in_range) need no history.
        if name in ("body_pct", "close_in_range"):
            min_rows = 1
        elif name in ("adx", "donchian", "stoch"):
            min_rows = (length or 14) * 2 + 5
        else:
            min_rows = (length or 26) + 5
        if n < min_rows:
            continue

        if name == "ema" and length:
            col = f"EMA_{length}"
            df[col] = EMAIndicator(close=close, window=length).ema_indicator()

        elif name == "sma" and length:
            col = f"SMA_{length}"
            df[col] = SMAIndicator(close=close, window=length).sma_indicator()

        elif name == "rsi" and length:
            col = f"RSI_{length}"
            df[col] = RSIIndicator(close=close, window=length).rsi()

        elif name == "bbands" and length:
            upper_col = f"BBU_{length}"
            bb = BollingerBands(close=close, window=length)
            df[upper_col] = bb.bollinger_hband()
            df[f"BBM_{length}"] = bb.bollinger_mavg()
            df[f"BBL_{length}"] = bb.bollinger_lband()

        elif name == "macd":
            macd = MACD(close=close)
            df["MACD"] = macd.macd()
            df["MACD_signal"] = macd.macd_signal()
            df["MACD_hist"] = macd.macd_diff()

        elif name == "vwap":
            try:
                df["VWAP"] = VolumeWeightedAveragePrice(
                    high=high, low=low, close=close, volume=volume,
                ).volume_weighted_average_price()
            except Exception:
                pass  # VWAP needs volume, may fail on sparse data

        elif name == "atr" and length:
            col = f"ATR_{length}"
            df[col] = AverageTrueRange(
                high=high, low=low, close=close, window=length,
            ).average_true_range()

        elif name == "adx" and length:
            col = f"ADX_{length}"
            adx = ADXIndicator(high=high, low=low, close=close, window=length)
            df[col] = adx.adx()
            df[f"DI+_{length}"] = adx.adx_pos()
            df[f"DI-_{length}"] = adx.adx_neg()

        elif name == "donchian" and length:
            col = f"DCH_{length}"
            dc = DonchianChannel(high=high, low=low, close=close, window=length)
            df[col] = dc.donchian_channel_hband()
            df[f"DCL_{length}"] = dc.donchian_channel_lband()
            df[f"DCM_{length}"] = dc.donchian_channel_mband()

        elif name == "stoch" and length:
            col = f"STOCHk_{length}"
            stoch = StochasticOscillator(high=high, low=low, close=close, window=length)
            df[col] = stoch.stoch()
            df[f"STOCHd_{length}"] = stoch.stoch_signal()

        elif name == "alma" and length:
            # Arnaud Legoux Moving Average — Gaussian-weighted average
            # with tunable offset (where the peak of the Gaussian sits
            # along the window) and sigma (width). Pine Script defaults:
            # offset=0.85, sigma=6. We match Pine exactly.
            col = f"ALMA_{length}"
            df[col] = _alma(close, length, offset=0.85, sigma=6.0)

        elif name == "alma_open" and length:
            # ALMA of the OPEN price — used by the SWIFT strategy to
            # detect ALMA-close vs ALMA-open crossovers on the alt TF.
            col = f"ALMA_OPEN_{length}"
            df[col] = _alma(df["open"], length, offset=0.85, sigma=6.0)

        # ── Scalper primitives (task #102) ────────────────────────────
        # Multi-bar velocity, microstructure, and volume-based
        # confirmation features for advanced scalper strategies.
        # Added after Tier 5 M1 revival (task #100) found the existing
        # single-bar (travel > N*ATR) filter degenerates on M1 noise.

        elif name == "roc" and length:
            # Rate of change: 100 * (close - close[-N]) / close[-N]
            col = f"ROC_{length}"
            prior = close.shift(length)
            roc = 100.0 * (close - prior) / prior.where(prior != 0, np.nan)
            df[col] = roc.fillna(0.0).astype(float)

        elif name == "velocity" and length:
            # Cumulative signed close delta over last N bars, normalized
            # by ATR_20. Requires ATR_20 to be computed first — if it's
            # missing we skip rather than crash, so the caller can
            # request [`atr_20`, `velocity_3`] in that order.
            atr_col = "ATR_20"
            if atr_col not in df.columns:
                # Silent skip — strategies using velocity must request
                # atr_20 too. Documented in feature_engine contract.
                continue
            col = f"VELOCITY_{length}"
            delta = close - close.shift(length)
            atr_safe = df[atr_col].where(df[atr_col] > 0, np.nan)
            df[col] = (delta / (length * atr_safe)).fillna(0.0).astype(float)

        elif name == "body_pct":
            # abs(close - open) / (high - low). 0 = doji, 1 = full body.
            # Per-bar, no length. Guards against zero-range bars.
            open_col = df["open"]
            rng = (high - low)
            rng_safe = rng.where(rng > 0, np.nan)
            df["BODY_PCT"] = ((close - open_col).abs() / rng_safe).fillna(0.0).astype(float)

        elif name == "close_in_range":
            # (close - low) / (high - low). 0 = close at low, 1 = close at high.
            rng = (high - low)
            rng_safe = rng.where(rng > 0, np.nan)
            df["CLOSE_IN_RANGE"] = ((close - low) / rng_safe).fillna(0.5).astype(float)

        elif name == "volume_zscore" and length:
            # Parameterized volume z-score. The VOL_ZSCORE_20 auto-compute
            # at the bottom of this function stays for backward compat;
            # this is a requestable version with custom N.
            col = f"VOLUME_Z_{length}"
            vol_mean = volume.rolling(length).mean()
            vol_std = volume.rolling(length).std()
            df[col] = ((volume - vol_mean) / vol_std).fillna(0.0)

        elif name == "dollar_volume" and length:
            # Rolling sum of close * volume — proxy for notional flow.
            col = f"DOLLAR_VOL_{length}"
            dv = close * volume
            df[col] = dv.rolling(length).sum().fillna(0.0)

    # Auto-compute rolling z-scores for meta-labeling features. These are
    # backward-compatible additions — no strategy is required to read them,
    # but they populate the meta-label features.py feature builder when
    # present. Gated on sufficient history so strategies running on short
    # buffers don't get NaN values in the latest row.
    if n >= 22 and "volume" in df.columns:
        vol_mean = volume.rolling(20).mean()
        vol_std = volume.rolling(20).std()
        df["VOL_ZSCORE_20"] = ((volume - vol_mean) / vol_std).fillna(0.0)

    if n >= 52 and "MACD_hist" in df.columns:
        mh = df["MACD_hist"]
        mh_mean = mh.rolling(50).mean()
        mh_std = mh.rolling(50).std()
        df["MACD_HIST_ZSCORE_50"] = ((mh - mh_mean) / mh_std).fillna(0.0)

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

        # In-place append — avoids full DataFrame copy from pd.concat
        df.loc[len(df)] = {
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "timestamp": candle.timestamp,
        }

        # Trim buffer
        if len(df) > MAX_BUFFER_SIZE:
            df = df.iloc[-MAX_BUFFER_SIZE:].reset_index(drop=True)

        self._buffers[key] = df

        # Compute indicators on a tail slice for efficiency.
        # Full buffer is kept for storage, but ta library only processes
        # the last COMPUTE_WINDOW rows (enough for all indicators to converge).
        n = len(df)
        if n > COMPUTE_WINDOW:
            compute_df = df.iloc[-COMPUTE_WINDOW:].copy().reset_index(drop=True)
            compute_df = _compute_indicators(compute_df, self.indicators)
            # Copy indicator columns from computed slice's last row to main buffer
            last_row = compute_df.iloc[-1]
            for col in compute_df.columns:
                if col not in ("open", "high", "low", "close", "volume", "timestamp"):
                    df.at[df.index[-1], col] = last_row[col]
        else:
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
