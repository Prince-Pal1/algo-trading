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
    stale_tolerance_min: float | None = None,
) -> dict[str, int]:
    """Feed historical candles to warm up indicator buffers + strategy state.

    Temporarily hooks feature_engine.on_features to route through the
    StrategyRouter (warming up strategy internal state like _prev_features,
    _closes deque, etc.) but discards all signals — no executor connected.

    Args:
        needed_pairs: If provided, only warmup these (symbol, tf) combos
                      instead of the full cartesian product.
        stale_tolerance_min: Max age (minutes) of the cache's newest candle
                      before it is considered unusable when a fresh download
                      is unavailable. Defaults to max(10 bars, 3600 min) —
                      the 60h floor tolerates weekend market closure (XAUUSD
                      Fri 21:00 → Sun 22:00 ≈ 49h).

    A cache that merely has *enough rows* is not enough: feeding stale
    candles primes lookback buffers (e.g. vol_momentum's 168-bar deque)
    with old prices, so live "momentum" becomes the price gap between the
    stale window and now. See docs/investigations/
    2026-06-11_vol_momentum_stale_warmup.md — this poisoned vol_momentum
    into a permanent short bias for ~3 weeks.

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

    # Detach router storage for the replay: signals emitted on historical
    # candles must not be logged to the live `signals` table. Before this
    # guard, every restart wrote phantom rows at stale prices (see
    # docs/investigations/2026-06-11_vol_momentum_stale_warmup.md).
    saved_storage = getattr(router, "_storage", None)
    router._storage = None

    try:
        for symbol, tf in pairs:
            key = f"{symbol.upper()}_{tf}"

            # Skip synthetic symbols (e.g., BTCUSDT-CARRY for funding carry).
            # These don't have real OHLCV on Binance spot and are served by
            # dedicated feeds (e.g., FundingSyntheticFeed).
            if "-CARRY" in symbol.upper():
                log.info("warmup_skip_synthetic", symbol=symbol, tf=tf)
                result[key] = 0
                continue

            tf_min = _TF_MINUTES.get(tf, 60)
            tolerance_min = (
                stale_tolerance_min
                if stale_tolerance_min is not None
                else max(10 * tf_min, 3600.0)
            )

            # Try Parquet first — but row count alone is NOT sufficient.
            # The cache must also be recent, else we prime lookback buffers
            # with stale prices.
            df = parquet.load(symbol.upper(), tf)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            cache_age_min = (
                (now_ms - int(df["timestamp"].max())) / 60_000.0
                if len(df) else float("inf")
            )
            cache_usable = len(df) >= min_candles
            # "Live-fresh" = newest candle within 3 bars of now; skip download.
            cache_fresh = cache_usable and cache_age_min <= 3 * tf_min

            if not cache_fresh:
                # Cache missing, short, or stale → try a fresh download.
                if downloader is None:
                    downloader = BinanceDownloader()

                lookback_min = min_candles * tf_min * 1.1  # 10% extra
                start = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
                start_str = start.strftime("%Y-%m-%d")

                log.info("warmup_downloading", symbol=symbol, tf=tf,
                         start=start_str, min_candles=min_candles,
                         cache_age_min=round(cache_age_min, 1))
                fresh_df = None
                try:
                    fresh_df = await downloader.download(symbol.upper(), tf, start_str)
                except Exception as e:
                    log.warning("warmup_download_failed", symbol=symbol, tf=tf,
                                error=str(e))

                if fresh_df is not None and not fresh_df.empty:
                    df = fresh_df
                    # Persist back so the next restart has a fresh cache even
                    # if the next download fails (save() merges + dedupes).
                    try:
                        parquet.save(fresh_df, symbol.upper(), tf)
                    except Exception as e:
                        log.warning("warmup_cache_persist_failed", symbol=symbol,
                                    tf=tf, error=str(e))
                elif cache_usable and cache_age_min <= tolerance_min:
                    # Download unavailable (e.g. XAUUSD is not on Binance) but
                    # the cache is within tolerance — weekend gaps land here.
                    log.warning("warmup_cache_stale_tolerated", symbol=symbol,
                                tf=tf, cache_age_min=round(cache_age_min, 1),
                                tolerance_min=tolerance_min)
                else:
                    # No fresh data and the cache is too old (or too short) to
                    # trust. Feeding it would poison lookback buffers — skip
                    # warmup entirely; strategies fill from live candles only.
                    log.warning("warmup_skipped_stale_cache", symbol=symbol,
                                tf=tf, cache_rows=len(df),
                                cache_age_min=round(cache_age_min, 1),
                                tolerance_min=tolerance_min)
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
        # Restore original callback + signal logging
        feature_engine.on_features = original_callback
        router._storage = saved_storage

    if downloader:
        await downloader.close()

    total = sum(result.values())
    log.info("warmup_complete", total_candles=total, symbols=result)
    return result
