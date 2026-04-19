"""Binance USD-M Perpetual liquidation feed — `!forceOrder@arr` stream.

Subscribes to the all-symbols liquidation stream on Binance Futures
WebSocket (`wss://fstream.binance.com`). Every forced-liquidation fill
is emitted as a `LiquidationEvent` via the async `on_liquidation`
callback.

This feed runs INDEPENDENTLY of BinanceWebSocketFeed — it uses the
futures WS endpoint (fstream.binance.com) rather than spot
(stream.binance.com). A single engine can wire BOTH feeds by
constructing one of each.

Binance publishes one of two stream shapes:
  `<symbol>@forceOrder`        — per-symbol stream
  `!forceOrder@arr`            — all-symbols stream

We use `!forceOrder@arr` (cheaper + catches all symbols) + filter
by configured `symbols` list at receive time.

Usage:
    feed = BinanceLiquidationFeed(symbols=["BTCUSDT", "ETHUSDT"])
    feed.on_liquidation = async_handler
    await feed.start()  # blocks until stop()
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Callable, Coroutine
from typing import Any

import certifi
import websockets
from websockets.asyncio.client import ClientConnection

from src.utils.logger import get_logger
from src.utils.types import LiquidationEvent

log = get_logger("binance_liq_feed")

OnLiquidation = Callable[[LiquidationEvent], Coroutine[Any, Any, None]]


class BinanceLiquidationFeed:
    """Async Binance Futures liquidation WS client."""

    FSTREAM_URL = "wss://fstream.binance.com/ws"
    RECONNECT_BASE = 1.0
    RECONNECT_MAX = 60.0

    def __init__(self, symbols: list[str] | None = None):
        """
        Args:
            symbols: optional filter — only emit liquidations whose symbol
                is in this list. If None or empty, emit ALL symbols.
                Use uppercase (e.g. "BTCUSDT", "ETHUSDT").
        """
        self.symbols: set[str] = {s.upper() for s in (symbols or [])}
        self._ws: ClientConnection | None = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_BASE

        self.on_liquidation: OnLiquidation | None = None

    @property
    def _url(self) -> str:
        # Subscribe to all-symbols aggregated stream; filter client-side.
        return f"{self.FSTREAM_URL}/!forceOrder@arr"

    async def start(self) -> None:
        """Connect and stream liquidation events. Blocks until stop() is called."""
        self._running = True
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        while self._running:
            try:
                log.info("connecting", url=self._url, symbols_filter=list(self.symbols) or "ALL")
                async with websockets.connect(
                    self._url, ssl=ssl_ctx, ping_interval=20, ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = self.RECONNECT_BASE
                    log.info("connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            await self._handle_message(raw)
                        except Exception as e:
                            log.error("message_error", error=str(e), raw=str(raw)[:200])
            except Exception as e:
                log.warning("disconnected", error=str(e))
                if not self._running:
                    break
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.RECONNECT_MAX,
                )

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _handle_message(self, raw: str) -> None:
        """Parse Binance's forceOrder payload and dispatch."""
        msg = json.loads(raw)
        # Payload shape (per Binance docs 2024):
        # { "e":"forceOrder", "E":<ts_ms>, "o": {
        #     "s":"BTCUSDT", "S":"SELL",           // long liq'd
        #     "o":"LIMIT", "f":"IOC",
        #     "q":"0.014",     // orig qty
        #     "p":"9910",      // price
        #     "ap":"9910",     // avg price
        #     "X":"FILLED",    // status
        #     "l":"0.014",     // last fill qty
        #     "z":"0.014",     // accumulated fill qty
        #     "T":<ts_ms>      // trade time
        # }}
        if msg.get("e") != "forceOrder":
            return
        o = msg.get("o", {})
        symbol = str(o.get("s", "")).upper()
        if self.symbols and symbol not in self.symbols:
            return
        try:
            price = float(o.get("ap") or o.get("p") or 0)
            qty = float(o.get("z") or o.get("q") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0 or qty <= 0:
            return
        ts = int(o.get("T") or msg.get("E") or 0)

        event = LiquidationEvent(
            symbol=symbol,
            side=str(o.get("S", "")).upper(),
            price=price,
            quantity=qty,
            notional_usd=price * qty,
            timestamp=ts,
        )
        log.debug("liquidation", symbol=symbol, side=event.side,
                  notional=round(event.notional_usd, 0))
        if self.on_liquidation is not None:
            try:
                await self.on_liquidation(event)
            except Exception as e:
                log.warning("on_liquidation_handler_failed", error=str(e))
