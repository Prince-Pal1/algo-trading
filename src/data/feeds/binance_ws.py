"""Binance WebSocket feed — real-time trade ticks, klines, and order book depth.

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
import httpx
import orjson
import websockets
from websockets.asyncio.client import ClientConnection

from src.data.order_book import ApplyResult, OrderBookState
from src.utils.config import get_config
from src.utils.logger import get_logger
from src.utils.types import Candle, OrderBookSnapshot, Tick

log = get_logger("binance_ws")

OnTick = Callable[[Tick], Coroutine[Any, Any, None]]
OnCandle = Callable[[Candle], Coroutine[Any, Any, None]]
OnDepth = Callable[[OrderBookSnapshot], Coroutine[Any, Any, None]]


class BinanceWebSocketFeed:
    """Async Binance WebSocket client for trade ticks and kline streams.

    Supports two stream families:
      - Spot (default): stream.binance.com / testnet.binance.vision
      - USD-M Perpetual Futures (use_futures_stream=True):
        fstream.binance.com / stream.binancefuture.com

    Depth: pass `depth_symbols` to subscribe to diff-depth streams. Each such
    symbol gets an `OrderBookState` that is bootstrapped from a REST snapshot
    and kept in sync from the stream. On a sequence gap the book desyncs and is
    automatically re-bootstrapped — it never serves a book it cannot vouch for.
    Depth is opt-in because a busy book is far more traffic than trades alone.
    """

    # Spot URLs
    BASE_URL = "wss://stream.binance.com:9443/ws"
    TESTNET_URL = "wss://testnet.binance.vision/ws"
    # Futures URLs (USD-M Perpetual)
    FUTURES_BASE_URL = "wss://fstream.binance.com/ws"
    FUTURES_TESTNET_URL = "wss://stream.binancefuture.com/ws"
    # REST depth-snapshot endpoints used to bootstrap each book.
    REST_DEPTH_URL = "https://api.binance.com/api/v3/depth"
    FUTURES_REST_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"
    DEPTH_SNAPSHOT_LIMIT = 1000
    DEPTH_RESYNC_COOLDOWN = 1.0  # seconds between re-bootstrap attempts
    RECONNECT_BASE = 1.0   # initial backoff seconds
    RECONNECT_MAX = 60.0   # max backoff seconds
    RECONNECT_JITTER = 0.1 # 10% jitter

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str] | None = None,
        testnet: bool = True,
        needed_pairs: set[tuple[str, str]] | None = None,
        use_futures_stream: bool = False,
        depth_symbols: list[str] | None = None,
        depth_speed: str = "100ms",
    ):
        self.symbols = [s.lower() for s in symbols]
        self.timeframes = timeframes or ["1m"]
        self.testnet = testnet
        self.use_futures_stream = use_futures_stream
        self.depth_symbols = [s.lower() for s in (depth_symbols or [])]
        self.depth_speed = depth_speed
        self._ws: ClientConnection | None = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_BASE

        # If provided, only subscribe to kline streams for these (symbol, tf) pairs
        # instead of the full cartesian product of symbols × timeframes.
        self._needed_pairs = needed_pairs

        # One book per depth symbol, keyed by lowercase symbol.
        self.books: dict[str, OrderBookState] = {
            sym: OrderBookState(sym.upper()) for sym in self.depth_symbols
        }
        self._resyncing: set[str] = set()

        # Callbacks — set these before calling start()
        self.on_tick: OnTick | None = None
        self.on_candle: OnCandle | None = None
        self.on_depth: OnDepth | None = None

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

        # Diff-depth streams. 100ms is the fastest Binance offers and is what a
        # heatmap needs; 1000ms is the alternative when bandwidth matters.
        for sym in self.depth_symbols:
            streams.append(f"{sym}@depth@{self.depth_speed}")
        return streams

    def _base_url(self) -> str:
        """Select the right WebSocket base based on testnet + spot/futures flags."""
        if self.use_futures_stream:
            return self.FUTURES_TESTNET_URL if self.testnet else self.FUTURES_BASE_URL
        return self.TESTNET_URL if self.testnet else self.BASE_URL

    @property
    def _url(self) -> str:
        streams = self._build_streams()
        return f"{self._base_url()}/{'/'.join(streams)}"

    @property
    def _combined_url(self) -> str:
        """Use combined stream endpoint for multiple streams."""
        base = self._base_url().replace("/ws", "/stream")
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

                    # Any gap in the stream invalidates every book, so a
                    # reconnect always re-bootstraps rather than resuming.
                    bootstrap: asyncio.Task | None = None
                    if self.books:
                        for book in self.books.values():
                            book.reset()
                        self._resyncing.clear()
                        bootstrap = asyncio.create_task(self._bootstrap_all())
                    try:
                        await self._listen(ws)
                    finally:
                        if bootstrap is not None and not bootstrap.done():
                            bootstrap.cancel()

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

    # ── Depth / order book ────────────────────────────────────────────

    def _rest_depth_url(self) -> str:
        return self.FUTURES_REST_DEPTH_URL if self.use_futures_stream else self.REST_DEPTH_URL

    async def _handle_depth(self, data: dict) -> None:
        """Fold one depthUpdate into its book; re-bootstrap on a sequence gap."""
        symbol = str(data.get("s", "")).lower()
        book = self.books.get(symbol)
        if book is None:
            return

        result = book.apply_diff(data)
        if result is ApplyResult.DESYNC:
            # Do not serve a drifted book — rebuild it from REST.
            asyncio.create_task(self._bootstrap_book(symbol, resync=True))
            return

        if result is ApplyResult.APPLIED and self.on_depth:
            snapshot = book.snapshot()
            if snapshot is not None:
                await self.on_depth(snapshot)

    async def _bootstrap_all(self) -> None:
        """Snapshot every depth book once the stream is buffering."""
        # Binance's procedure requires the stream to be live BEFORE the
        # snapshot is taken, so that no update falls between the two.
        await asyncio.sleep(0.5)
        await asyncio.gather(
            *(self._bootstrap_book(sym) for sym in self.depth_symbols),
            return_exceptions=True,
        )

    async def _bootstrap_book(self, symbol: str, resync: bool = False) -> None:
        """Fetch a REST depth snapshot and seed the book from it."""
        if symbol in self._resyncing:
            return  # a bootstrap is already in flight for this symbol
        self._resyncing.add(symbol)
        try:
            if resync:
                await asyncio.sleep(self.DEPTH_RESYNC_COOLDOWN)
            book = self.books.get(symbol)
            if book is None:
                return

            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            async with httpx.AsyncClient(verify=ssl_ctx, timeout=15.0) as client:
                resp = await client.get(
                    self._rest_depth_url(),
                    params={"symbol": symbol.upper(), "limit": self.DEPTH_SNAPSHOT_LIMIT},
                )
                resp.raise_for_status()
                payload = resp.json()

            if resync:
                book.reset()
            applied = book.apply_snapshot(
                int(payload["lastUpdateId"]),
                payload.get("bids", []),
                payload.get("asks", []),
            )
            bid_levels, ask_levels = book.depth_counts
            log.info(
                "depth_bootstrapped",
                symbol=symbol, resync=resync,
                last_update_id=book.last_update_id,
                buffered_applied=applied,
                bids=bid_levels, asks=ask_levels,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(
                "depth_bootstrap_failed",
                symbol=symbol, error=str(e), type=type(e).__name__,
            )
        finally:
            self._resyncing.discard(symbol)

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

                elif event_type == "depthUpdate":
                    await self._handle_depth(data)

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
