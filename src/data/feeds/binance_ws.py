"""Binance WebSocket feed — real-time trade ticks and kline (candle) streams.

Connects to Binance spot/futures WebSocket API and emits Tick and Candle objects
via async callbacks. Handles auto-reconnect on disconnect.

Usage:
    feed = BinanceWebSocketFeed(symbols=["btcusdt", "ethusdt"], timeframes=["1m", "5m"])
    feed.on_tick = my_tick_handler
    feed.on_candle = my_candle_handler
    await feed.start()
"""

from __future__ import annotations

import asyncio
import random
import ssl
from collections.abc import Callable, Coroutine
from typing import Any

import certifi
import orjson
import websockets
from websockets.asyncio.client import ClientConnection

from src.utils.config import get_config
from src.utils.logger import get_logger
from src.utils.types import Candle, Tick

log = get_logger("binance_ws")

OnTick = Callable[[Tick], Coroutine[Any, Any, None]]
OnCandle = Callable[[Candle], Coroutine[Any, Any, None]]


class BinanceWebSocketFeed:
    """Async Binance WebSocket client for trade ticks and kline streams."""

    BASE_URL = "wss://stream.binance.com:9443/ws"
    TESTNET_URL = "wss://testnet.binance.vision/ws"
    RECONNECT_BASE = 1.0   # initial backoff seconds
    RECONNECT_MAX = 60.0   # max backoff seconds
    RECONNECT_JITTER = 0.1 # 10% jitter

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str] | None = None,
        testnet: bool = True,
        needed_pairs: set[tuple[str, str]] | None = None,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.timeframes = timeframes or ["1m"]
        self.testnet = testnet
        self._ws: ClientConnection | None = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_BASE

        # If provided, only subscribe to kline streams for these (symbol, tf) pairs
        # instead of the full cartesian product of symbols × timeframes.
        self._needed_pairs = needed_pairs

        # Callbacks — set these before calling start()
        self.on_tick: OnTick | None = None
        self.on_candle: OnCandle | None = None

    def _build_streams(self) -> list[str]:
        """Build stream list — trade ticks for all symbols, klines only for needed pairs."""
        streams = []
        for sym in self.symbols:
            streams.append(f"{sym}@trade")

        if self._needed_pairs:
            # Subscribe only to kline streams that strategies actually need
            for sym, tf in self._needed_pairs:
                streams.append(f"{sym.lower()}@kline_{tf}")
        else:
            # Fallback: cartesian product (legacy behavior)
            for sym in self.symbols:
                for tf in self.timeframes:
                    streams.append(f"{sym}@kline_{tf}")
        return streams

    @property
    def _url(self) -> str:
        base = self.TESTNET_URL if self.testnet else self.BASE_URL
        streams = self._build_streams()
        return f"{base}/{'/'.join(streams)}"

    @property
    def _combined_url(self) -> str:
        """Use combined stream endpoint for multiple streams."""
        base = self.TESTNET_URL if self.testnet else self.BASE_URL
        base = base.replace("/ws", "/stream")
        streams = self._build_streams()
        return f"{base}?streams={'/'.join(streams)}"

    async def start(self) -> None:
        """Connect and start receiving data. Blocks until stop() is called."""
        self._running = True

        while self._running:
            try:
                url = self._combined_url
                log.info("connecting", url=url[:80] + "...", symbols=self.symbols)

                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
                async with websockets.connect(url, ping_interval=20, ssl=ssl_ctx) as ws:
                    self._ws = ws
                    self._reconnect_delay = self.RECONNECT_BASE  # reset on success
                    log.info("connected", symbols=self.symbols, timeframes=self.timeframes)
                    await self._listen(ws)

            except websockets.ConnectionClosed as e:
                log.warning("disconnected", code=e.code, reason=str(e.reason))
            except Exception as e:
                log.error("connection_error", error=str(e), type=type(e).__name__)

            if self._running:
                # Exponential backoff with jitter
                jitter = self._reconnect_delay * self.RECONNECT_JITTER * random.random()
                delay = self._reconnect_delay + jitter
                log.info("reconnecting", delay=round(delay, 1))
                await asyncio.sleep(delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self.RECONNECT_MAX)

    async def stop(self) -> None:
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        log.info("stopped")

    async def _listen(self, ws: ClientConnection) -> None:
        """Process incoming messages."""
        async for raw in ws:
            try:
                msg = orjson.loads(raw)

                # Combined stream wraps data in {"stream": ..., "data": ...}
                if "stream" in msg:
                    data = msg["data"]
                    stream = msg["stream"]
                else:
                    data = msg
                    stream = data.get("e", "")

                event_type = data.get("e")

                if event_type == "trade" and self.on_tick:
                    tick = Tick(
                        symbol=data["s"],
                        price=float(data["p"]),
                        quantity=float(data["q"]),
                        timestamp=data["T"],
                        is_buyer_maker=data["m"],
                    )
                    await self.on_tick(tick)

                elif event_type == "kline" and self.on_candle:
                    k = data["k"]
                    candle = Candle(
                        symbol=k["s"],
                        timeframe=k["i"],
                        open=float(k["o"]),
                        high=float(k["h"]),
                        low=float(k["l"]),
                        close=float(k["c"]),
                        volume=float(k["v"]),
                        timestamp=k["t"],
                        closed=k["x"],
                    )
                    await self.on_candle(candle)

            except Exception as e:
                log.error("parse_error", error=str(e), raw=str(raw)[:200])
