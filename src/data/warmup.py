"""Historical warmup — pre-loads candles so indicators and strategies are ready on first live bar.

On cold start, FeatureEngine has 0 candles. Strategies need 25-200 candles
before producing their first signal. This module loads historical data from
Parquet files (or downloads via Binance REST) and feeds it through the
FeatureEngine → StrategyRouter pipeline. Signals during warmup are discarded.

Usage:
    count = await warmup(feature_engine, router, symbols, timeframes)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.data.downloader import BinanceDownloader
from src.data.feature_engine import FeatureEngine
from src.data.storage import ParquetStore
from src.strategies.router import StrategyRouter
from src.utils.logger import get_logger
from src.utils.types import Candle

log = get_logger("warmup")

# Timeframe → minutes mapping for calculating how far back to look
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480,
    "1d": 1440, "1w": 10080,
}


async def warmup(
    feature_engine: FeatureEngine,
    router: StrategyRouter,
    symbols: list[str],
    timeframes: list[str],
    min_candles: int = 200,
    needed_pairs: set[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """Feed historical candles to warm up indicator buffers + strategy state.

    Temporarily hooks feature_engine.on_features to route through the
    StrategyRouter (warming up strategy internal state like _prev_features,
    _closes deque, etc.) but discards all signals — no executor connected.

    Args:
        needed_pairs: If provided, only warmup these (symbol, tf) combos
                      instead of the full cartesian product.

    Returns:
        Dict of {"SYMBOL_TF": candles_loaded} for logging.
    """
    parquet = ParquetStore()
    result: dict[str, int] = {}
    downloader: BinanceDownloader | None = None

    # Build iteration list: needed_pairs if available, else cartesian product
    if needed_pairs:
        pairs = [(sym, tf) for sym, tf in needed_pairs]
    else:
        pairs = [(sym, tf) for sym in symbols for tf in timeframes]

    # Save the original callback and replace with warmup callback
    original_callback = feature_engine.on_features

    async def _warmup_on_features(symbol: str, timeframe: str, features: pd.Series) -> None:
        """Route to strategies for state warmup, discard signals."""
        await router.on_features(symbol, timeframe, features)

    feature_engine.on_features = _warmup_on_features

    try:
        for symbol, tf in pairs:
            key = f"{symbol.upper()}_{tf}"

            # Try Parquet first
            df = parquet.load(symbol.upper(), tf)

            if len(df) < min_candles:
                # Download from Binance
                if downloader is None:
                    downloader = BinanceDownloader()

                tf_min = _TF_MINUTES.get(tf, 60)
                lookback_min = min_candles * tf_min * 1.1  # 10% extra
                start = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
                start_str = start.strftime("%Y-%m-%d")

                log.info("warmup_downloading", symbol=symbol, tf=tf,
                         start=start_str, min_candles=min_candles)
                try:
                    df = await downloader.download(symbol.upper(), tf, start_str)
                except Exception as e:
                    log.warning("warmup_download_failed", symbol=symbol, tf=tf,
                                error=str(e))
                    result[key] = 0
                    continue

            if df.empty:
                result[key] = 0
                continue

            # Take the last min_candles rows
            df = df.tail(min_candles).reset_index(drop=True)
            loaded = 0

            for _, row in df.iterrows():
                candle = Candle(
                    symbol=symbol.upper(),
                    timeframe=tf,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    timestamp=int(row["timestamp"]),
                    closed=True,
                )
                await feature_engine.handle_candle(candle)
                loaded += 1

            result[key] = loaded
            log.info("warmup_symbol_done", symbol=symbol, tf=tf, candles=loaded)

    finally:
        # Restore original callback
        feature_engine.on_features = original_callback

    if downloader:
        await downloader.close()

    total = sum(result.values())
    log.info("warmup_complete", total_candles=total, symbols=result)
    return result
