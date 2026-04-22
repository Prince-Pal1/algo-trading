"""IC Markets cTrader Open API data feed."""

from __future__ import annotations

import asyncio
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
        ProtoOAAccountAuthRes,
        ProtoOAApplicationAuthReq,
        ProtoOAApplicationAuthRes,
        ProtoOAErrorRes,
        ProtoOAExecutionEvent,
        ProtoOAGetAccountListByAccessTokenReq,
        ProtoOAGetAccountListByAccessTokenRes,
        ProtoOASpotEvent,
        ProtoOASubscribeLiveTrendbarReq,
        ProtoOASubscribeSpotsReq,
        ProtoOASubscribeSpotsRes,
        ProtoOASymbolByIdReq,
        ProtoOASymbolsListReq,
        ProtoOASymbolsListRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod
    CTRADER_AVAILABLE = True
except ImportError:
    CTRADER_AVAILABLE = False


log = get_logger("icmarkets_feed")


# cTrader ProtoOATrendbarPeriod enum values (verified 2026-04-18).
_TIMEFRAME_TO_PROTO = {
    "1m": 1,    # M1
    "5m": 5,    # M5
    "15m": 7,   # M15
    "30m": 8,   # M30
    "1h": 9,    # H1
    "4h": 10,   # H4
    "1d": 12,   # D1
}

# Period minutes — used to derive the candle timestamp from
# Trendbar.utcTimestampInMinutes (bar-open reference).
_TIMEFRAME_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440,
}

_PROTO_TO_TIMEFRAME = {v: k for k, v in _TIMEFRAME_TO_PROTO.items()}


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

        # Per-(symbol, timeframe) latest trendbar — held until a NEW bar
        # timestamp arrives, at which point the previous bar is emitted as
        # finalized. cTrader's live-trendbar stream emits multiple updates
        # per bar (partial bars) with the same utcTimestampInMinutes but
        # accumulating volume/high/low/close. Without this tracker every
        # partial update flows downstream as `closed=True`, which caused
        # the 2026-04-22 rapid-flip-flop bug.
        self._in_progress_bar: dict[tuple[str, str], "Candle"] = {}

        self.on_tick: OnTick | None = None
        self.on_candle: OnCandle | None = None

    def _new_client(self) -> Client:
        return Client(
            self._config.host,
            self._config.port,
            TcpProtocol,
        )

    def _on_message_received(self, client: Client, message) -> None:
        """Dispatch protobuf messages through the auth → subscribe walk.

        Runs on the Twisted reactor thread. Any coroutines (like on_tick)
        are bridged back to the asyncio loop via run_coroutine_threadsafe.

        Sequence (mirrors scripts/ctrader_smoke.py):
          AppAuthRes → GetAccountListReq
          GetAccountListRes → AccountAuthReq
          AccountAuthRes → SymbolsListReq
          SymbolsListRes → SubscribeSpotsReq
          SubscribeSpotsRes → stream begins
          SpotEvent → on_tick callback
        """
        msg = Protobuf.extract(message)
        msg_type = type(msg).__name__

        if isinstance(msg, ProtoOAErrorRes):
            log.warning("icmarkets_error", code=msg.errorCode, desc=msg.description)
            return

        if isinstance(msg, ProtoOAApplicationAuthRes):
            log.info("icmarkets_app_authed")
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = self._config.access_token
            self._send_quiet(client, req)
            return

        if isinstance(msg, ProtoOAGetAccountListByAccessTokenRes):
            accounts = list(msg.ctidTraderAccount)
            log.info("icmarkets_account_list", count=len(accounts))
            if not accounts:
                log.error("icmarkets_no_accounts")
                return
            # Prefer exact match with configured account_id (ctidTraderAccountId).
            chosen = None
            for a in accounts:
                if a.ctidTraderAccountId == self._config.account_id:
                    chosen = a
                    break
            if chosen is None:
                chosen = accounts[0]
                log.warning(
                    "icmarkets_account_mismatch",
                    configured=self._config.account_id,
                    using=chosen.ctidTraderAccountId,
                )
            self._ctid_trader_account_id = chosen.ctidTraderAccountId
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self._ctid_trader_account_id
            req.accessToken = self._config.access_token
            self._send_quiet(client, req)
            return

        if isinstance(msg, ProtoOAAccountAuthRes):
            log.info("icmarkets_account_authed", ctid=msg.ctidTraderAccountId)
            req = ProtoOASymbolsListReq()
            req.ctidTraderAccountId = self._ctid_trader_account_id
            req.includeArchivedSymbols = False
            self._send_quiet(client, req)
            return

        if isinstance(msg, ProtoOASymbolsListRes):
            wanted = {s.upper() for s in self._symbols}
            for s in msg.symbol:
                if s.symbolName.upper() in wanted:
                    self._symbol_id_map[s.symbolName.upper()] = s.symbolId
            log.info(
                "icmarkets_symbols_resolved",
                total=len(msg.symbol),
                mapped=self._symbol_id_map,
            )
            if not self._symbol_id_map:
                log.error("icmarkets_no_target_symbols", wanted=list(wanted))
                return
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = self._ctid_trader_account_id
            for sid in self._symbol_id_map.values():
                req.symbolId.append(sid)
            self._send_quiet(client, req)
            # Also subscribe to live trendbars for each configured timeframe
            # so the engine receives pre-assembled candles (no 1h wait).
            for tf in self._timeframes:
                period = _TIMEFRAME_TO_PROTO.get(tf)
                if period is None:
                    continue
                for sid in self._symbol_id_map.values():
                    tb_req = ProtoOASubscribeLiveTrendbarReq()
                    tb_req.ctidTraderAccountId = self._ctid_trader_account_id
                    tb_req.symbolId = sid
                    tb_req.period = period
                    self._send_quiet(client, tb_req)
            return

        if isinstance(msg, ProtoOASubscribeSpotsRes):
            log.info("icmarkets_spot_subscribed")
            return

        if isinstance(msg, ProtoOASpotEvent):
            self._handle_spot_event(msg)
            return

        if isinstance(msg, ProtoOAExecutionEvent):
            self._handle_execution_event(msg)
            return

        log.debug("icmarkets_msg_unhandled", type=msg_type)

    def _send_quiet(self, client: "Client", req) -> None:
        """Send a request and swallow the Deferred's TimeoutError.

        cTrader-open-api wraps each `client.send()` in a Deferred with a
        5s timeout. If no response arrives in that window (normal for
        one-shot subscribe requests whose response we route via the main
        message-received handler), the Deferred's cancel path raises
        `twisted.internet.defer.TimeoutError` into whatever callback ran
        last — which poisons unrelated spot-event handling. Attaching an
        errback absorbs the timeout cleanly.
        """
        d = client.send(req)
        if d is not None:
            d.addErrback(lambda err: log.debug(
                "icmarkets_send_errback",
                type=type(req).__name__,
                reason=str(err.value) if hasattr(err, "value") else str(err),
            ))

    def _handle_spot_event(self, event: "ProtoOASpotEvent") -> None:
        symbol_id = event.symbolId
        symbol_name = self._symbol_name_for_id(symbol_id) or f"SID_{symbol_id}"
        loop = getattr(self, "_loop", None)
        # ── Tick path (bid/ask) ──
        if self.on_tick is not None:
            bid = float(event.bid) / 100_000.0 if event.HasField("bid") else 0.0
            ask = float(event.ask) / 100_000.0 if event.HasField("ask") else 0.0
            price = (bid + ask) / 2.0 if bid and ask else (bid or ask)
            if price > 0:
                tick = Tick(
                    symbol=symbol_name,
                    price=price,
                    quantity=0.0,
                    timestamp=int(event.timestamp) if event.HasField("timestamp") else 0,
                    is_buyer_maker=False,
                )
                if loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(self.on_tick(tick), loop)
                    except Exception as e:
                        log.warning("icmarkets_tick_dispatch_failed", error=str(e))
        # ── Candle path (embedded trendbars) ──
        # cTrader emits multiple partial updates per bar with the same
        # timestamp. Only emit when a NEW bar timestamp arrives, meaning
        # the previous bar is now finalized. Track the latest trendbar
        # per (symbol, tf) in self._in_progress_bar.
        if self.on_candle is not None and loop is not None:
            for tb in event.trendbar:
                candle = self._trendbar_to_candle(tb, symbol_name)
                if candle is None:
                    continue
                key = (candle.symbol, candle.timeframe)
                prev = self._in_progress_bar.get(key)
                if prev is not None and candle.timestamp > prev.timestamp:
                    # New bar started — emit the previous one (finalized).
                    try:
                        asyncio.run_coroutine_threadsafe(self.on_candle(prev), loop)
                    except Exception as e:
                        log.warning("icmarkets_candle_dispatch_failed", error=str(e))
                # Track the latest state of the current (in-progress) bar.
                self._in_progress_bar[key] = candle

    def _trendbar_to_candle(self, tb, symbol_name: str) -> "Candle | None":
        """Decode a ProtoOATrendbar into a Candle.

        Proto encoding: prices are ×100,000 integers. `low` is absolute;
        open/high/close are deltas from low. Timestamp is minutes since
        epoch at the bar's OPEN (not close).
        """
        tf = _PROTO_TO_TIMEFRAME.get(tb.period)
        if tf is None:
            return None
        low = float(tb.low) / 100_000.0
        high = low + float(tb.deltaHigh) / 100_000.0
        open_ = low + float(tb.deltaOpen) / 100_000.0
        close = low + float(tb.deltaClose) / 100_000.0
        if low <= 0 or high <= 0:
            return None
        period_minutes = _TIMEFRAME_MINUTES.get(tf, 60)
        # Broadcast ts at bar CLOSE (ms) to match BinanceWS semantics — the
        # CandleBuilder/FeatureEngine expect close-referenced timestamps.
        ts_ms = int((tb.utcTimestampInMinutes + period_minutes) * 60 * 1000)
        return Candle(
            symbol=symbol_name,
            timeframe=tf,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=float(tb.volume),
            timestamp=ts_ms,
            closed=True,
        )

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

    async def start(self) -> None:
        """Start the Twisted-backed cTrader client on a worker thread.

        The cTrader library wraps a Twisted ClientService which runs its
        retry loop on whatever reactor is installed. Mixing Twisted's
        default SelectReactor with asyncio (via asyncioreactor) has proven
        brittle on macOS. Instead we run the default Twisted reactor in
        a dedicated worker thread; callbacks fire on that thread and we
        hand async work back to the main asyncio loop via
        `asyncio.run_coroutine_threadsafe`. The main async loop holds on
        `_stop_event` until `stop()` is called.
        """
        import threading
        from twisted.internet import reactor  # default SelectReactor

        self._loop = asyncio.get_running_loop()
        self._running = True
        self._stop_event = asyncio.Event()
        self._reactor = reactor

        def _run_reactor() -> None:
            self._client = self._new_client()
            self._client.setConnectedCallback(lambda c: self._authenticate())
            self._client.setDisconnectedCallback(
                lambda c, reason: log.warning("icmarkets_disconnected", reason=str(reason))
            )
            self._client.setMessageReceivedCallback(self._on_message_received)
            self._client.startService()
            log.info(
                "icmarkets_reactor_thread_starting",
                symbols=self._symbols, timeframes=self._timeframes,
            )
            try:
                reactor.run(installSignalHandlers=False)
            except Exception as e:
                log.error("icmarkets_reactor_crashed", error=str(e))

        self._reactor_thread = threading.Thread(
            target=_run_reactor, name="icmarkets-reactor", daemon=True,
        )
        self._reactor_thread.start()
        log.info("icmarkets_feed_started", symbols=self._symbols, timeframes=self._timeframes)
        await self._stop_event.wait()

    async def stop(self) -> None:
        self._running = False
        if self._client is not None:
            try:
                self._client.stopService()
            except Exception as e:
                log.warning("icmarkets_stop_failed", error=str(e))
            self._client = None
        if getattr(self, "_reactor", None) is not None and self._reactor.running:
            try:
                self._reactor.callFromThread(self._reactor.stop)
            except Exception as e:
                log.warning("icmarkets_reactor_stop_failed", error=str(e))
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
