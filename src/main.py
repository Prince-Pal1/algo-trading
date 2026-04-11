"""Main entry point — wires the data pipeline and starts the trading engine.

Pipeline: BinanceWS → CandleBuilder → FeatureEngine → Storage
                                    ↘ (future) StrategyRouter → RiskManager → Execution

Usage:
    python -m src.main                          # Run with defaults from config
    python -m src.main --symbols BTCUSDT ETHUSDT --timeframes 1m 5m
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone

import pandas as pd

from src.data.candle_builder import CandleBuilder
from src.data.feature_engine import FeatureEngine
from src.data.feeds.binance_ws import BinanceWebSocketFeed
from src.data.storage import Storage
from src.utils.config import get_config
from src.utils.logger import get_logger
from src.utils.types import Candle, Tick

log = get_logger("main")


class TradingEngine:
    """Wires all data pipeline components together and manages lifecycle."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        indicators: list[str],
    ):
        self.symbols = symbols
        self.timeframes = timeframes

        # ── Components ──
        self.feed = BinanceWebSocketFeed(
            symbols=symbols,
            timeframes=timeframes,
            testnet=True,
        )
        self.candle_builder = CandleBuilder(timeframes=timeframes)
        self.feature_engine = FeatureEngine(indicators=indicators)
        self.storage = Storage()

        # ── Stats ──
        self._tick_count = 0
        self._candle_count = 0
        self._feature_count = 0
        self._start_time: float = 0

    async def _on_tick(self, tick: Tick) -> None:
        """Handle raw tick from exchange."""
        self._tick_count += 1
        await self.candle_builder.handle_tick(tick)

        # Log every 1000 ticks
        if self._tick_count % 1000 == 0:
            log.info("tick_stats", count=self._tick_count, symbol=tick.symbol, price=tick.price)

    async def _on_candle(self, candle: Candle) -> None:
        """Handle completed candle from builder or exchange kline."""
        self._candle_count += 1
        log.info(
            "candle",
            symbol=candle.symbol,
            tf=candle.timeframe,
            o=candle.open,
            h=candle.high,
            l=candle.low,
            c=candle.close,
            v=round(candle.volume, 4),
        )

        # Feed to indicator engine
        await self.feature_engine.handle_candle(candle)

    async def _on_features(self, symbol: str, timeframe: str, features: pd.Series) -> None:
        """Handle computed features — this is where strategies will plug in."""
        self._feature_count += 1

        # Log key indicator values (non-NaN only)
        indicator_vals = {}
        for col in features.index:
            if col not in ("open", "high", "low", "close", "volume", "timestamp"):
                val = features[col]
                if pd.notna(val):
                    indicator_vals[col] = round(float(val), 4)

        if indicator_vals:
            log.info("features", symbol=symbol, tf=timeframe, **indicator_vals)

        # TODO Phase 2: Route features to StrategyRouter
        # await self.strategy_router.on_features(symbol, timeframe, features)

    async def start(self) -> None:
        """Initialize all components and start the data pipeline."""
        import time
        self._start_time = time.time()

        cfg = get_config()
        log.info(
            "engine_starting",
            mode=cfg.mode,
            symbols=self.symbols,
            timeframes=self.timeframes,
            indicators=self.feature_engine.indicators,
        )

        # Initialize storage
        await self.storage.init()

        # Wire the callback chain:
        #   feed.on_tick → _on_tick → candle_builder.handle_tick
        #   feed.on_candle → candle_builder.handle_candle → _on_candle
        #   candle_builder.on_candle → _on_candle → feature_engine.handle_candle
        #   feature_engine.on_features → _on_features
        self.feed.on_tick = self._on_tick
        self.feed.on_candle = self.candle_builder.handle_candle
        self.candle_builder.on_candle = self._on_candle
        self.feature_engine.on_features = self._on_features

        log.info("pipeline_wired", flow="feed → candle_builder → feature_engine → strategies (TODO)")

        # Start the feed (blocks until stop)
        await self.feed.start()

    async def stop(self) -> None:
        """Graceful shutdown."""
        import time

        log.info("engine_stopping")
        await self.feed.stop()
        await self.storage.close()

        elapsed = time.time() - self._start_time if self._start_time else 0
        log.info(
            "engine_stopped",
            uptime_seconds=round(elapsed, 1),
            ticks=self._tick_count,
            candles=self._candle_count,
            features=self._feature_count,
            builder_stats=self.candle_builder.stats,
            engine_stats=self.feature_engine.stats,
        )

    def print_status(self) -> None:
        """Print current pipeline status."""
        import time
        elapsed = time.time() - self._start_time if self._start_time else 0
        print(f"\n{'='*60}")
        print(f"  Algo Trading Engine — Status")
        print(f"  Mode: {get_config().mode} | Uptime: {elapsed:.0f}s")
        print(f"  Symbols: {self.symbols}")
        print(f"  Timeframes: {self.timeframes}")
        print(f"  Ticks: {self._tick_count}")
        print(f"  Candles: {self._candle_count}")
        print(f"  Features: {self._feature_count}")
        print(f"  Builder: {self.candle_builder.stats}")
        print(f"  Engine: {self.feature_engine.stats}")
        print(f"{'='*60}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Algo Trading Engine")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to trade (default: from strategies.toml)",
    )
    parser.add_argument(
        "--timeframes", nargs="+", default=None,
        help="Timeframes to track (default: from strategies.toml)",
    )
    parser.add_argument(
        "--indicators", nargs="+",
        default=["ema_9", "ema_21", "rsi_7", "bbands_20", "macd", "atr_14"],
        help="Indicators to compute",
    )
    return parser.parse_args()


def _get_symbols_from_config() -> list[str]:
    """Pull enabled strategy symbols from strategies.toml."""
    cfg = get_config()
    symbols = set()
    for name, strat in cfg.strategies.items():
        if strat.get("enabled", False):
            for market in strat.get("markets", []):
                symbols.add(market.lower())
    return list(symbols) or ["btcusdt"]


def _get_timeframes_from_config() -> list[str]:
    """Pull enabled strategy timeframes from strategies.toml."""
    cfg = get_config()
    timeframes = set()
    for name, strat in cfg.strategies.items():
        if strat.get("enabled", False):
            tf = strat.get("timeframe")
            if tf:
                timeframes.add(tf)
    return list(timeframes) or ["1m"]


async def run(args: argparse.Namespace) -> None:
    symbols = args.symbols or _get_symbols_from_config()
    timeframes = args.timeframes or _get_timeframes_from_config()

    engine = TradingEngine(
        symbols=symbols,
        timeframes=timeframes,
        indicators=args.indicators,
    )

    # Handle Ctrl+C gracefully
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("shutdown_signal_received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Run feed in background, wait for shutdown signal
    feed_task = asyncio.create_task(engine.start())

    await shutdown_event.wait()
    await engine.stop()

    # Cancel the feed task if still running
    if not feed_task.done():
        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    args = parse_args()

    print(f"\n  Algo Trading Engine v0.1.0")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Mode: {get_config().mode}")
    print(f"  Press Ctrl+C to stop\n")

    try:
        import uvloop
        uvloop.install()
        log.info("uvloop_installed")
    except ImportError:
        pass

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
