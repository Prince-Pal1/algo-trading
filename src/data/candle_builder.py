"""Candle Builder — aggregates raw ticks into OHLCV candles.

Two modes:
1. Pass-through: Forward kline candles from exchange (Binance sends these directly)
2. Build from ticks: Aggregate Tick objects into candles at custom timeframes

Usage:
    builder = CandleBuilder(timeframes=["1m", "5m"])
    builder.on_candle = my_handler
    await builder.handle_tick(tick)       # Build from raw ticks
    await builder.handle_candle(candle)   # Pass-through from exchange klines
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

from src.utils.logger import get_logger
from src.utils.types import Candle, Tick

log = get_logger("candle_builder")

OnCandle = Callable[[Candle], Coroutine[Any, Any, None]]

# Timeframe to milliseconds
TF_MS: dict[str, int] = {
    "5s": 5_000,
    "15s": 15_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class _CandleAccumulator:
    """Accumulates ticks into a single candle for one symbol+timeframe."""

    __slots__ = ("symbol", "timeframe", "tf_ms", "open", "high", "low", "close",
                 "volume", "start_ts", "tick_count")

    def __init__(self, symbol: str, timeframe: str):
        self.symbol = symbol
        self.timeframe = timeframe
        self.tf_ms = TF_MS[timeframe]
        self.reset(0)

    def reset(self, start_ts: int) -> None:
        self.open = 0.0
        self.high = 0.0
        self.low = float("inf")
        self.close = 0.0
        self.volume = 0.0
        self.start_ts = start_ts
        self.tick_count = 0

    def update(self, tick: Tick) -> None:
        if self.tick_count == 0:
            self.open = tick.price
            self.high = tick.price
            self.low = tick.price
            # Align start_ts to timeframe boundary
            self.start_ts = (tick.timestamp // self.tf_ms) * self.tf_ms
        else:
            if tick.price > self.high:
                self.high = tick.price
            if tick.price < self.low:
                self.low = tick.price

        self.close = tick.price
        self.volume += tick.quantity
        self.tick_count += 1

    def is_closed(self, current_ts: int) -> bool:
        if self.tick_count == 0:
            return False
        return current_ts >= self.start_ts + self.tf_ms

    def to_candle(self) -> Candle:
        return Candle(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp=self.start_ts,
            closed=True,
        )


class CandleBuilder:
    """Builds OHLCV candles from raw ticks or forwards exchange klines.

    Dedup logic: when Binance sends kline candles for a (symbol, timeframe),
    those are authoritative. Tick-built candles are suppressed for that pair
    to prevent duplicate emission. Tick aggregation is only used for custom
    timeframes that the exchange doesn't provide (e.g. 5s, 15s).
    """

    def __init__(self, timeframes: list[str] | None = None):
        self.timeframes = timeframes or ["1m"]
        self.on_candle: OnCandle | None = None

        # key: (symbol, timeframe) -> accumulator
        self._accumulators: dict[tuple[str, str], _CandleAccumulator] = defaultdict(
            lambda: None  # type: ignore[arg-type]
        )
        self._candle_count = 0

        # (symbol, timeframe) pairs where exchange provides klines — tick-built
        # candles are suppressed for these to avoid duplicate emission.
        self._kline_pairs: set[tuple[str, str]] = set()

    async def handle_tick(self, tick: Tick) -> None:
        """Aggregate a tick into candles for all configured timeframes."""
        for tf in self.timeframes:
            if tf not in TF_MS:
                continue

            key = (tick.symbol, tf)

            # Skip tick aggregation for pairs where exchange sends klines
            if key in self._kline_pairs:
                continue

            acc = self._accumulators.get(key)
            if acc is None:
                acc = _CandleAccumulator(tick.symbol, tf)
                self._accumulators[key] = acc

            # Check if current candle should close
            if acc.is_closed(tick.timestamp):
                candle = acc.to_candle()
                self._candle_count += 1
                if self.on_candle:
                    await self.on_candle(candle)
                acc.reset(tick.timestamp)

            acc.update(tick)

    async def handle_candle(self, candle: Candle) -> None:
        """Forward exchange-provided kline candle (pass-through mode).

        Only emits when candle.closed is True (finalized candle).
        Auto-registers the (symbol, timeframe) pair so tick-built candles
        are suppressed for it going forward.
        """
        key = (candle.symbol, candle.timeframe)
        if key not in self._kline_pairs:
            self._kline_pairs.add(key)

        if candle.closed and self.on_candle:
            self._candle_count += 1
            await self.on_candle(candle)

    @property
    def stats(self) -> dict:
        return {
            "candles_emitted": self._candle_count,
            "active_accumulators": len(self._accumulators),
        }
