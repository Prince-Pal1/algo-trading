"""Unit tests for BinanceFuturesClient (P2 of Futures executor build).

Mocks httpx requests — no network traffic. Validates:
  - HMAC signing against canonical Binance docs example
  - REST endpoint routing (testnet vs mainnet)
  - Error handling (BinanceAPIError with code + msg)
  - Signed params include timestamp + recvWindow + signature
  - Order placement produces correct request body
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.execution.binance_futures_client import (
    BinanceAPIError,
    BinanceFuturesClient,
    BinanceFuturesConfig,
)


# Binance docs canonical example — key, secret, signature are from the public docs
DOCS_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"


def _cfg(testnet: bool = True) -> BinanceFuturesConfig:
    return BinanceFuturesConfig(
        api_key="test_key", api_secret=DOCS_SECRET, testnet=testnet,
    )


class TestSigning:
    def test_hmac_matches_binance_docs_canonical_example(self):
        """Exact example from Binance Futures Signed-Endpoint docs."""
        client = BinanceFuturesClient(_cfg())
        params = {
            "symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
            "quantity": 1, "price": "0.1", "recvWindow": 5000,
            "timestamp": 1499827319559,
        }
        expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
        assert client._sign(params) == expected

    def test_signed_params_include_timestamp_and_signature(self):
        client = BinanceFuturesClient(_cfg())
        signed = client._signed_params({"symbol": "BTCUSDT"})
        assert "timestamp" in signed
        assert "recvWindow" in signed
        assert "signature" in signed
        assert signed["symbol"] == "BTCUSDT"
        # 64-char hex sig
        assert len(signed["signature"]) == 64
        assert all(c in "0123456789abcdef" for c in signed["signature"])


class TestUrlRouting:
    def test_testnet_urls(self):
        cfg = _cfg(testnet=True)
        assert cfg.rest_base == "https://testnet.binancefuture.com"
        assert cfg.ws_base == "wss://stream.binancefuture.com/ws"

    def test_mainnet_urls(self):
        cfg = _cfg(testnet=False)
        assert cfg.rest_base == "https://fapi.binance.com"
        assert cfg.ws_base == "wss://fstream.binance.com/ws"


class TestConfigFromEnv:
    def test_rejects_missing_keys(self, monkeypatch):
        monkeypatch.delenv("BINANCE_FUTURES_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_FUTURES_API_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="BINANCE_FUTURES_API_KEY"):
            BinanceFuturesConfig.from_env()

    def test_loads_testnet_default(self, monkeypatch):
        monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "k")
        monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "s")
        monkeypatch.delenv("BINANCE_FUTURES_TESTNET", raising=False)
        cfg = BinanceFuturesConfig.from_env()
        assert cfg.testnet is True  # safer default

    def test_honors_explicit_mainnet(self, monkeypatch):
        monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "k")
        monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "s")
        monkeypatch.setenv("BINANCE_FUTURES_TESTNET", "false")
        cfg = BinanceFuturesConfig.from_env()
        assert cfg.testnet is False


class TestApiError:
    def test_raises_with_parsed_code_and_msg(self):
        err = BinanceAPIError(400, -2019, "Margin is insufficient", '{"code":-2019,"msg":"..."}')
        assert err.status_code == 400
        assert err.code == -2019
        assert "Margin is insufficient" in str(err)


class _FakeResponse:
    """Minimal httpx.Response-like object for mocking."""
    def __init__(self, status: int, payload: dict | list):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
class TestRequestFlow:
    async def test_ping_ok(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            # Patch the underlying httpx client's request method
            async def fake_request(*args, **kwargs):
                return _FakeResponse(200, {})
            client._http.request = AsyncMock(side_effect=fake_request)
            ok = await client.ping()
            assert ok is True

    async def test_ping_failure_returns_false(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            client._http.request = AsyncMock(
                side_effect=httpx.ConnectError("host unreachable"),
            )
            ok = await client.ping()
            assert ok is False

    async def test_get_account_signs_and_returns_parsed(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            async def fake_request(method, path, params=None, **_):
                # Signed endpoint must include timestamp + signature
                assert "timestamp" in params
                assert "signature" in params
                assert path == "/fapi/v2/account"
                return _FakeResponse(200, {"totalWalletBalance": "1000.0"})
            client._http.request = AsyncMock(side_effect=fake_request)
            data = await client.get_account()
            assert data["totalWalletBalance"] == "1000.0"

    async def test_get_positions_filters_zero_entries(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            payload = [
                {"symbol": "BTCUSDT", "positionAmt": "0.5"},
                {"symbol": "ETHUSDT", "positionAmt": "0"},  # filtered out
                {"symbol": "SOLUSDT", "positionAmt": "-1.2"},
            ]
            async def fake_request(*a, **kw):
                return _FakeResponse(200, payload)
            client._http.request = AsyncMock(side_effect=fake_request)
            positions = await client.get_positions()
            symbols = {p["symbol"] for p in positions}
            assert symbols == {"BTCUSDT", "SOLUSDT"}

    async def test_place_market_order_params(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            captured = {}
            async def fake_request(method, path, params=None, **_):
                captured["method"] = method
                captured["path"] = path
                captured["params"] = params
                return _FakeResponse(200, {
                    "orderId": 12345, "clientOrderId": params["newClientOrderId"],
                    "status": "NEW", "avgPrice": "0",
                })
            client._http.request = AsyncMock(side_effect=fake_request)
            resp = await client.place_market_order(
                symbol="BTCUSDT", side="BUY", quantity=0.1,
                client_order_id="myclient-abc123",
            )
            assert captured["method"] == "POST"
            assert captured["path"] == "/fapi/v1/order"
            assert captured["params"]["symbol"] == "BTCUSDT"
            assert captured["params"]["side"] == "BUY"
            assert captured["params"]["type"] == "MARKET"
            assert captured["params"]["quantity"] == "0.1"
            assert captured["params"]["newClientOrderId"] == "myclient-abc123"
            assert "signature" in captured["params"]
            assert resp["orderId"] == 12345

    async def test_place_market_order_reduce_only_flag(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            captured = {}
            async def fake_request(method, path, params=None, **_):
                captured["params"] = params
                return _FakeResponse(200, {"orderId": 1})
            client._http.request = AsyncMock(side_effect=fake_request)
            await client.place_market_order(
                symbol="BTCUSDT", side="SELL", quantity=0.1,
                client_order_id="close-123", reduce_only=True,
            )
            assert captured["params"]["reduceOnly"] == "true"

    async def test_cancel_order_by_client_order_id(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            captured = {}
            async def fake_request(method, path, params=None, **_):
                captured["method"] = method
                captured["params"] = params
                return _FakeResponse(200, {"status": "CANCELED"})
            client._http.request = AsyncMock(side_effect=fake_request)
            await client.cancel_order(symbol="BTCUSDT", client_order_id="abc")
            assert captured["method"] == "DELETE"
            assert captured["params"]["origClientOrderId"] == "abc"

    async def test_cancel_order_requires_id(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            client._http.request = AsyncMock()
            with pytest.raises(ValueError, match="client_order_id or order_id"):
                await client.cancel_order(symbol="BTCUSDT")

    async def test_set_margin_type_swallows_already_set_code(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            async def fake_request(method, path, params=None, **_):
                return _FakeResponse(400, {"code": -4046, "msg": "No need to change"})
            client._http.request = AsyncMock(side_effect=fake_request)
            res = await client.set_margin_type("BTCUSDT", "ISOLATED")
            assert res["msg"] == "margin_type_already_set"

    async def test_non_2xx_raises_binance_api_error(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            async def fake_request(*a, **kw):
                return _FakeResponse(400, {"code": -2019, "msg": "Margin insufficient"})
            client._http.request = AsyncMock(side_effect=fake_request)
            with pytest.raises(BinanceAPIError) as excinfo:
                await client.get_account()
            assert excinfo.value.code == -2019
            assert "Margin" in excinfo.value.msg

    async def test_listen_key_start_returns_key(self):
        client = BinanceFuturesClient(_cfg())
        async with client:
            async def fake_request(method, path, params=None, **_):
                assert path == "/fapi/v1/listenKey"
                return _FakeResponse(200, {"listenKey": "test_listen_key_xyz"})
            client._http.request = AsyncMock(side_effect=fake_request)
            key = await client.start_user_stream()
            assert key == "test_listen_key_xyz"
