"""Binance USD-M Perpetual Futures REST + WebSocket client.

Thin wrapper over httpx (signed REST) and websockets (user-data stream).
Used by BinanceFuturesExecutor for order placement + account sync +
fill confirmation.

Endpoints:
  REST mainnet:   https://fapi.binance.com
  REST testnet:   https://testnet.binancefuture.com
  WS   mainnet:   wss://fstream.binance.com/ws/<listenKey>
  WS   testnet:   wss://stream.binancefuture.com/ws/<listenKey>

Auth: HMAC-SHA256 signature on the query string using the API secret.
The signed `signature` parameter is appended to the query string;
`X-MBX-APIKEY` header carries the API key.

All listenKey management (POST / PUT / DELETE on /fapi/v1/listenKey)
is handled internally. The user-data WebSocket reconnects with
exponential backoff using the same pattern as BinanceLiquidationFeed.

Safety notes:
  - Testnet vs mainnet decided by `testnet: bool` constructor arg
    (also readable from env var BINANCE_FUTURES_TESTNET=true).
  - `X-MBX-APIKEY` NEVER logged — only signature-stripped query strings
    appear in debug output.
  - Any non-2xx REST response raises BinanceAPIError with body + status.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import ssl
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import certifi
import httpx
import websockets

from src.utils.logger import get_logger

log = get_logger("binance_futures")


class BinanceAPIError(Exception):
    """Non-2xx REST response from Binance Futures."""

    def __init__(self, status_code: int, code: int | None, msg: str, raw: str):
        self.status_code = status_code
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"[HTTP {status_code}][code={code}] {msg}")


@dataclass(frozen=True)
class BinanceFuturesConfig:
    api_key: str
    api_secret: str
    testnet: bool = True
    recv_window_ms: int = 5000
    request_timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> "BinanceFuturesConfig":
        api_key = os.environ.get("BINANCE_FUTURES_API_KEY", "")
        api_secret = os.environ.get("BINANCE_FUTURES_API_SECRET", "")
        testnet = os.environ.get("BINANCE_FUTURES_TESTNET", "true").lower() == "true"
        if not api_key or not api_secret:
            raise RuntimeError(
                "BINANCE_FUTURES_API_KEY and BINANCE_FUTURES_API_SECRET must be "
                "set in the environment (.env). Get testnet keys from "
                "https://testnet.binancefuture.com"
            )
        return cls(api_key=api_key, api_secret=api_secret, testnet=testnet)

    @property
    def rest_base(self) -> str:
        return "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"

    @property
    def ws_base(self) -> str:
        return (
            "wss://stream.binancefuture.com/ws"
            if self.testnet
            else "wss://fstream.binance.com/ws"
        )


OnUserEvent = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class BinanceFuturesClient:
    """REST + WebSocket client for Binance USD-M perpetual futures."""

    def __init__(self, config: BinanceFuturesConfig):
        self._cfg = config
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "BinanceFuturesClient":
        self._http = httpx.AsyncClient(
            base_url=self._cfg.rest_base,
            timeout=self._cfg.request_timeout_s,
            verify=self._ssl,
            headers={"X-MBX-APIKEY": self._cfg.api_key},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── Signing helpers ────────────────────────────────────────────

    def _sign(self, params: dict[str, Any]) -> str:
        """HMAC-SHA256 of the URL-encoded query string."""
        query = urlencode(params)
        signature = hmac.new(
            self._cfg.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return signature

    def _signed_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add `timestamp` + `recvWindow` + `signature` to a params dict."""
        out = dict(params)
        out["timestamp"] = int(time.time() * 1000)
        out["recvWindow"] = self._cfg.recv_window_ms
        out["signature"] = self._sign(out)
        return out

    # ── Low-level request helper ──────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        if self._http is None:
            raise RuntimeError("BinanceFuturesClient must be used as an async context manager")

        params = params or {}
        if signed:
            params = self._signed_params(params)

        try:
            response = await self._http.request(method, path, params=params)
        except httpx.HTTPError as e:
            log.error("binance_request_error", method=method, path=path, error=str(e))
            raise

        if response.status_code >= 400:
            try:
                body = response.json()
                code = body.get("code")
                msg = body.get("msg", "")
            except Exception:
                code = None
                msg = response.text
            raise BinanceAPIError(response.status_code, code, msg, response.text)

        return response.json()

    # ── Public market-data endpoints ──────────────────────────────

    async def ping(self) -> bool:
        """Connectivity check."""
        try:
            await self._request("GET", "/fapi/v1/ping")
            return True
        except Exception as e:
            log.warning("binance_ping_failed", error=str(e))
            return False

    async def server_time(self) -> int:
        """Binance server time in ms — used to check clock skew."""
        data = await self._request("GET", "/fapi/v1/time")
        return int(data["serverTime"])

    # ── Account / Position endpoints (signed) ─────────────────────

    async def get_account(self) -> dict[str, Any]:
        """GET /fapi/v2/account — balances, position entries, total margin."""
        return await self._request("GET", "/fapi/v2/account", signed=True)

    async def get_positions(self) -> list[dict[str, Any]]:
        """GET /fapi/v2/positionRisk — one entry per symbol the account has touched."""
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        # Filter to positions with non-zero positionAmt (Binance returns all symbols)
        return [p for p in data if float(p.get("positionAmt", 0)) != 0.0]

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET /fapi/v1/openOrders — pending / unfilled orders."""
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """POST /fapi/v1/leverage — set position leverage for a symbol."""
        params = {"symbol": symbol, "leverage": int(leverage)}
        return await self._request("POST", "/fapi/v1/leverage", params=params, signed=True)

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict[str, Any]:
        """POST /fapi/v1/marginType — ISOLATED (default) or CROSSED."""
        params = {"symbol": symbol, "marginType": margin_type}
        try:
            return await self._request("POST", "/fapi/v1/marginType", params=params, signed=True)
        except BinanceAPIError as e:
            # code -4046 = "No need to change margin type" (already correct) — not an error
            if e.code == -4046:
                return {"msg": "margin_type_already_set"}
            raise

    # ── Order placement ───────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """POST /fapi/v1/order — market order.

        side: "BUY" (long, or close short) or "SELL" (short, or close long).
        reduce_only: True only decreases an existing position (never flips it).
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": f"{float(quantity):.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": client_order_id,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._request("POST", "/fapi/v1/order", params=params, signed=True)

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        client_order_id: str,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """POST /fapi/v1/order — limit order."""
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": f"{float(quantity):.8f}".rstrip("0").rstrip("."),
            "price": f"{float(price):.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": client_order_id,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._request("POST", "/fapi/v1/order", params=params, signed=True)

    async def cancel_order(
        self,
        symbol: str,
        client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> dict[str, Any]:
        """DELETE /fapi/v1/order — cancel by client_order_id OR order_id."""
        if not client_order_id and not order_id:
            raise ValueError("must pass either client_order_id or order_id")
        params: dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            params["orderId"] = order_id
        return await self._request("DELETE", "/fapi/v1/order", params=params, signed=True)

    async def get_order(
        self,
        symbol: str,
        client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> dict[str, Any]:
        """GET /fapi/v1/order — fetch current state of an order."""
        params: dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        elif order_id:
            params["orderId"] = order_id
        else:
            raise ValueError("must pass either client_order_id or order_id")
        return await self._request("GET", "/fapi/v1/order", params=params, signed=True)

    # ── User-data stream (listenKey lifecycle) ────────────────────

    async def start_user_stream(self) -> str:
        """POST /fapi/v1/listenKey — create a listenKey (valid 60 min)."""
        # This endpoint is "User Stream" — requires API key but NOT signing.
        data = await self._request("POST", "/fapi/v1/listenKey")
        return data["listenKey"]

    async def keepalive_user_stream(self) -> None:
        """PUT /fapi/v1/listenKey — extends listenKey validity by 60 min."""
        await self._request("PUT", "/fapi/v1/listenKey")

    async def close_user_stream(self) -> None:
        """DELETE /fapi/v1/listenKey — explicit close (optional; expires on its own)."""
        await self._request("DELETE", "/fapi/v1/listenKey")


class BinanceFuturesUserDataFeed:
    """WebSocket wrapper over the Binance Futures user-data stream.

    Constructor takes the parent client to manage listenKey lifecycle
    (start + 30-min keepalive). Emits events via callbacks.
    """

    RECONNECT_BASE = 1.0
    RECONNECT_MAX = 60.0
    KEEPALIVE_INTERVAL = 30 * 60  # 30 min; Binance expires keys at 60 min

    def __init__(self, client: BinanceFuturesClient):
        self._client = client
        self._listen_key: str | None = None
        self._ws = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_BASE
        self._keepalive_task: asyncio.Task | None = None

        # Callbacks
        self.on_account_update: OnUserEvent | None = None
        self.on_order_trade_update: OnUserEvent | None = None

    async def start(self) -> None:
        """Boot + run forever until stop(). Blocks."""
        self._running = True
        self._listen_key = await self._client.start_user_stream()
        log.info("user_stream_listen_key_obtained")
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        url = f"{self._client._cfg.ws_base}/{self._listen_key}"

        while self._running:
            try:
                log.info("user_stream_connecting", url=url[:60] + "...")
                async with websockets.connect(
                    url, ssl=ssl_ctx, ping_interval=20, ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = self.RECONNECT_BASE
                    log.info("user_stream_connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            await self._handle_message(raw)
                        except Exception as e:
                            log.error("user_stream_message_error", error=str(e),
                                      raw=str(raw)[:200])
            except Exception as e:
                log.warning("user_stream_disconnected", error=str(e))
                if not self._running:
                    break
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.RECONNECT_MAX,
                )

    async def stop(self) -> None:
        self._running = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        # Best-effort: tell Binance we're done with the listenKey
        try:
            await self._client.close_user_stream()
        except Exception:
            pass

    async def _keepalive_loop(self) -> None:
        """Refresh listenKey every 30 min."""
        while self._running:
            await asyncio.sleep(self.KEEPALIVE_INTERVAL)
            if not self._running:
                break
            try:
                await self._client.keepalive_user_stream()
                log.debug("user_stream_keepalive_ok")
            except Exception as e:
                log.warning("user_stream_keepalive_failed", error=str(e))

    async def _handle_message(self, raw: str) -> None:
        msg = json.loads(raw)
        event_type = msg.get("e")
        if event_type == "ACCOUNT_UPDATE" and self.on_account_update:
            await self.on_account_update(msg)
        elif event_type == "ORDER_TRADE_UPDATE" and self.on_order_trade_update:
            await self.on_order_trade_update(msg)
        elif event_type == "listenKeyExpired":
            log.warning("user_stream_listen_key_expired")
            # Force reconnect loop to restart
            if self._ws:
                await self._ws.close()
        else:
            log.debug("user_stream_unhandled", type=event_type)
