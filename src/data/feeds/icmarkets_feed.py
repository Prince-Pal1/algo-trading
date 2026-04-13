"""IC Markets cTrader Open API data feed."""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from src.utils.logger import get_logger
from src.utils.types import Candle, Tick

try:
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq,
        ProtoOAApplicationAuthReq,
        ProtoOAExecutionEvent,
        ProtoOASpotEvent,
        ProtoOASubscribeLiveTrendbarReq,
        ProtoOASubscribeSpotsReq,
        ProtoOASymbolByIdReq,
        ProtoOASymbolsListReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod
    CTRADER_AVAILABLE = True
except ImportError:
    CTRADER_AVAILABLE = False


log = get_logger("icmarkets_feed")


_TIMEFRAME_TO_PROTO = {
    "1m": 1,    # M1
    "5m": 5,    # M5
    "15m": 6,   # M15
    "30m": 7,   # M30
    "1h": 8,    # H1
    "4h": 9,    # H4
    "1d": 10,   # D1
}


OnTick = Callable[[Tick], Coroutine[Any, Any, None]]
OnCandle = Callable[[Candle], Coroutine[Any, Any, None]]


@dataclass
class ICMarketsConfig:
    client_id: str
    client_secret: str
    access_token: str
    account_id: int
    host: str = "demo.ctraderapi.com"
    port: int = 5035

    @classmethod
    def from_env(cls) -> "ICMarketsConfig":
        env = os.getenv("CTRADER_ENVIRONMENT", "demo").lower()
        host_key = "CTRADER_LIVE_HOST" if env == "live" else "CTRADER_DEMO_HOST"
        default_host = "live.ctraderapi.com" if env == "live" else "demo.ctraderapi.com"
        account_id = int(os.getenv("CTRADER_ACCOUNT_ID", "0"))
        return cls(
            client_id=os.environ["CTRADER_CLIENT_ID"],
            client_secret=os.environ["CTRADER_CLIENT_SECRET"],
            access_token=os.environ["CTRADER_ACCESS_TOKEN"],
            account_id=account_id,
            host=os.getenv(host_key, default_host),
            port=int(os.getenv("CTRADER_PORT", "5035")),
        )


class ICMarketsFeed:
    """cTrader Open API feed — emits Tick + Candle via async callbacks.

    Same callback contract as BinanceWebSocketFeed so the engine doesn't
    need to know which feed is driving it. Set `on_tick` and `on_candle`
    before calling `start()`.
    """

    def __init__(
        self,
        config: ICMarketsConfig,
        *,
        symbols: list[str],
        timeframes: list[str] | None = None,
    ) -> None:
        if not CTRADER_AVAILABLE:
            raise ImportError(
                "ctrader-open-api is not installed. Run `pip install ctrader-open-api`."
            )
        self._config = config
        self._symbols = symbols
        self._timeframes = timeframes or ["5m"]
        self._client: Client | None = None
        self._running = False
        self._symbol_id_map: dict[str, int] = {}

        self.on_tick: OnTick | None = None
        self.on_candle: OnCandle | None = None

    def _new_client(self) -> Client:
        return Client(
            self._config.host,
            self._config.port,
            TcpProtocol,
        )

    def _on_message_received(self, client: Client, message) -> None:
        """Dispatch incoming protobuf messages to the right handler."""
        msg = Protobuf.extract(message)
        msg_type = type(msg).__name__

        if isinstance(msg, ProtoOASpotEvent):
            self._handle_spot_event(msg)
        elif isinstance(msg, ProtoOAExecutionEvent):
            self._handle_execution_event(msg)
        else:
            log.debug("icmarkets_msg_received", type=msg_type)

    def _handle_spot_event(self, event: "ProtoOASpotEvent") -> None:
        if self.on_tick is None:
            return
        symbol_id = event.symbolId
        symbol_name = self._symbol_name_for_id(symbol_id) or f"SID_{symbol_id}"
        price = float(event.bid) / 100_000.0 if event.HasField("bid") else 0.0
        if price <= 0:
            return
        tick = Tick(
            symbol=symbol_name,
            price=price,
            quantity=0.0,
            timestamp=int(event.timestamp) if event.HasField("timestamp") else 0,
            exchange="icmarkets",
        )
        log.debug("icmarkets_spot", symbol=symbol_name, price=price)

    def _handle_execution_event(self, event: "ProtoOAExecutionEvent") -> None:
        log.info("icmarkets_execution", type=event.executionType)

    def _symbol_name_for_id(self, symbol_id: int) -> str | None:
        for name, sid in self._symbol_id_map.items():
            if sid == symbol_id:
                return name
        return None

    def _authenticate(self) -> None:
        req = ProtoOAApplicationAuthReq()
        req.clientId = self._config.client_id
        req.clientSecret = self._config.client_secret
        if self._client is None:
            raise RuntimeError("client not initialized")
        self._client.send(req)

    def _authenticate_account(self) -> None:
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self._config.account_id
        req.accessToken = self._config.access_token
        if self._client is None:
            raise RuntimeError("client not initialized")
        self._client.send(req)

    def _load_symbol_catalog(self) -> None:
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._config.account_id
        req.includeArchivedSymbols = False
        if self._client is None:
            raise RuntimeError("client not initialized")
        self._client.send(req)

    def _subscribe_spots(self) -> None:
        if self._client is None:
            raise RuntimeError("client not initialized")
        for symbol_name in self._symbols:
            symbol_id = self._symbol_id_map.get(symbol_name)
            if symbol_id is None:
                log.warning("icmarkets_unknown_symbol", symbol=symbol_name)
                continue
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = self._config.account_id
            req.symbolId.append(symbol_id)
            self._client.send(req)

    def _subscribe_trendbars(self) -> None:
        if self._client is None:
            raise RuntimeError("client not initialized")
        for symbol_name in self._symbols:
            symbol_id = self._symbol_id_map.get(symbol_name)
            if symbol_id is None:
                continue
            for tf in self._timeframes:
                period = _TIMEFRAME_TO_PROTO.get(tf)
                if period is None:
                    continue
                req = ProtoOASubscribeLiveTrendbarReq()
                req.ctidTraderAccountId = self._config.account_id
                req.symbolId = symbol_id
                req.period = period
                self._client.send(req)

    def start(self) -> None:
        self._client = self._new_client()
        self._client.setConnectedCallback(lambda c: self._authenticate())
        self._client.setDisconnectedCallback(
            lambda c, reason: log.warning("icmarkets_disconnected", reason=str(reason))
        )
        self._client.setMessageReceivedCallback(self._on_message_received)
        self._running = True
        self._client.startService()

    def stop(self) -> None:
        self._running = False
        if self._client is not None:
            self._client.stopService()
            self._client = None
