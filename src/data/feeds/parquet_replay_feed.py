"""Historical parquet replay feed — emits bars as if live."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.logger import get_logger
from src.utils.types import Candle, Tick

log = get_logger("parquet_replay_feed")


OnTick = Callable[[Tick], Coroutine[Any, Any, None]]
OnCandle = Callable[[Candle], Coroutine[Any, Any, None]]


_TIMEFRAME_TO_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


class ParquetReplayFeed:
    """Reads a parquet file once, emits bars chronologically via on_candle.

    Matches BinanceWebSocketFeed's public surface (on_tick/on_candle
    attribute-set + async start()/stop()) so the shadow orchestrator can
    swap feeds without code changes. Supports fast-forward (for test loops)
    and realtime-paced (for live shadow) via `realtime_mode`.
    """

    def __init__(
        self,
        parquet_path: str | Path,
        symbol: str,
        timeframe: str = "1h",
        *,
        realtime_mode: bool = False,
        speed_multiplier: float = 1.0,
    ) -> None:
        self._parquet_path = Path(parquet_path)
        self._symbol = symbol.upper()
        self._timeframe = timeframe
        self._realtime_mode = realtime_mode
        self._speed_multiplier = max(1e-6, float(speed_multiplier))
        self._running = False
        self._df: pd.DataFrame | None = None

        # Match BinanceWebSocketFeed's callback surface exactly
        self.on_tick: OnTick | None = None
        self.on_candle: OnCandle | None = None

    def _load(self) -> pd.DataFrame:
        if not self._parquet_path.exists():
            raise FileNotFoundError(f"parquet not found: {self._parquet_path}")
        df = pd.read_parquet(self._parquet_path)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"parquet missing columns: {missing}")
        return df.sort_values("timestamp").reset_index(drop=True)

    async def start(self) -> None:
        self._df = self._load()
        self._running = True
        log.info(
            "parquet_replay_start",
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=len(self._df),
            realtime_mode=self._realtime_mode,
        )

        tf_seconds = _TIMEFRAME_TO_SECONDS.get(self._timeframe, 3600)
        sleep_between_bars = (
            (tf_seconds / self._speed_multiplier) if self._realtime_mode else 0.0
        )

        for _, row in self._df.iterrows():
            if not self._running:
                break
            candle = Candle(
                symbol=self._symbol,
                timeframe=self._timeframe,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0) or 0.0),
                timestamp=int(row["timestamp"]),
                closed=True,
            )
            if self.on_candle is not None:
                await self.on_candle(candle)
            if not self._running:
                break  # callback may have called stop() — don't sleep
            if sleep_between_bars > 0:
                await asyncio.sleep(sleep_between_bars)

        log.info("parquet_replay_complete", symbol=self._symbol, bars=len(self._df))
        self._running = False

    async def stop(self) -> None:
        self._running = False
