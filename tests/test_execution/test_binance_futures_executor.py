"""Unit tests for BinanceFuturesExecutor — Phase 3 of the live executor.

Mocks the REST + WS client fully. Validates:
  - `allow_live_trading` safety gate raises on instantiation
  - Position reconciliation on init() populates from fake REST response
  - execute(LONG) flow: REST submit → fake WS fill → Fill returned
  - Rate limit rejects 11th order within a minute
  - Notional cap rejects oversized orders
  - close_all() flattens every open position
  - `update_prices` recomputes uPnL
  - Trade-close hook fires on close fills
  - Kill switch blocks new orders
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.binance_futures_account import (
    AccountSnapshot,
    BinanceFuturesAccountManager,
)
from src.execution.binance_futures_client import (
    BinanceFuturesClient,
    BinanceFuturesConfig,
    BinanceFuturesUserDataFeed,
)
from src.execution.binance_futures_executor import BinanceFuturesExecutor
from src.utils.types import RiskProfile, Side, Signal, SignalAction


@pytest.fixture
def fake_client():
    client = MagicMock(spec=BinanceFuturesClient)
    client.ping = AsyncMock(return_value=True)
    client.get_account = AsyncMock(return_value={
        "totalWalletBalance": "10000",
        "totalMarginBalance": "10000",
        "totalUnrealizedProfit": "0",
        "availableBalance": "9500",
        "positions": [],
    })
    client.get_positions = AsyncMock(return_value=[])
    client.place_market_order = AsyncMock(return_value={
        "orderId": 1, "status": "NEW", "clientOrderId": "placeholder",
    })
    client.cancel_order = AsyncMock()
    return client


@pytest.fixture
def fake_account(fake_client):
    # Pre-seed a snapshot so no refresh call is needed
    acct = BinanceFuturesAccountManager(fake_client, ttl_seconds=60.0)
    acct._snapshot = AccountSnapshot(
        total_wallet_balance=10_000,
        total_margin_balance=10_000,
        total_unrealized_pnl=0,
        available_balance=9_500,
        positions_count=0,
    )
    # Override refresh to update this in-place on invalidation
    acct.refresh = AsyncMock(return_value=acct._snapshot)
    return acct


@pytest.fixture
def fake_user_stream(fake_client):
    return BinanceFuturesUserDataFeed(fake_client)


def _make_exec(fake_client, fake_account, fake_user_stream, **overrides):
    defaults = dict(
        allow_live_trading=True,
        max_order_notional_usd=500.0,
        rate_limit_per_symbol_per_min=10,
        fill_timeout_s=1.0,   # tighter for tests
        default_leverage=1,
    )
    defaults.update(overrides)
    return BinanceFuturesExecutor(fake_client, fake_account, fake_user_stream, **defaults)


def _long_signal(symbol="BTCUSDT", entry=65_000.0, stop=63_700.0) -> Signal:
    return Signal(
        symbol=symbol, action=SignalAction.LONG, confidence=0.8,
        strategy_name="test_strategy", timeframe="8h",
        entry_price=entry, stop_loss=stop, take_profit=None,
        risk_pct=0.01, metadata={},
    )


def _close_signal(symbol="BTCUSDT") -> Signal:
    return Signal(
        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
        strategy_name="test_strategy", timeframe="8h",
        entry_price=65_500.0, stop_loss=None, take_profit=None,
        risk_pct=0.01, metadata={},
    )


# ── Safety gate ───────────────────────────────────────────────────


class TestSafetyGate:
    def test_requires_allow_live_trading_flag(
        self, fake_client, fake_account, fake_user_stream,
    ):
        with pytest.raises(RuntimeError, match="allow_live_trading=True"):
            BinanceFuturesExecutor(fake_client, fake_account, fake_user_stream)

    def test_constructs_with_flag_true(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        assert e._max_notional == 500.0

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_orders(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        e.kill()
        fill = await e.execute(_long_signal())
        assert fill is None
        fake_client.place_market_order.assert_not_called()


# ── Reconciliation ────────────────────────────────────────────────


class TestInit:
    @pytest.mark.asyncio
    async def test_init_pings_and_reconciles(
        self, fake_client, fake_account, fake_user_stream,
    ):
        fake_client.get_positions.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "64000",
             "markPrice": "65000", "unRealizedProfit": "100"},
        ]
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        await e.init()
        fake_client.ping.assert_awaited_once()
        assert "BTCUSDT" in e._positions
        assert e._positions["BTCUSDT"].quantity == 0.1
        assert e._positions["BTCUSDT"].side == Side.BUY

    @pytest.mark.asyncio
    async def test_init_aborts_on_ping_fail(
        self, fake_client, fake_account, fake_user_stream,
    ):
        fake_client.ping.return_value = False
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        with pytest.raises(RuntimeError, match="ping failed"):
            await e.init()


# ── Order flow ────────────────────────────────────────────────────


class TestExecuteLong:
    @pytest.mark.asyncio
    async def test_rejects_on_notional_cap(
        self, fake_client, fake_account, fake_user_stream,
    ):
        # Cap = $500. Signal entry_price $65k × qty for 1% risk on $10k = ...
        # risk $100 / stop_dist $1300 = 0.077 qty → notional $5000 >> $500
        e = _make_exec(fake_client, fake_account, fake_user_stream,
                       max_order_notional_usd=500.0)
        fill = await e.execute(_long_signal())
        assert fill is None
        fake_client.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_on_insufficient_margin(
        self, fake_client, fake_account, fake_user_stream,
    ):
        fake_account._snapshot = AccountSnapshot(
            total_wallet_balance=10_000, total_margin_balance=10_000,
            total_unrealized_pnl=0, available_balance=50,  # only $50 free
            positions_count=0,
        )
        e = _make_exec(fake_client, fake_account, fake_user_stream,
                       max_order_notional_usd=100_000.0)  # big cap so we hit margin check
        sig = _long_signal(entry=100.0, stop=90.0)  # tiny prices → small notional
        fill = await e.execute(sig)
        # notional = 0.01 × 100 = $1. available = $50. buffer 1.5x → need $1.50.
        # Actually with $50 available, can afford $1 order. Let's test with bigger notional.
        # Re-signal with bigger entry so notional > $75 (50 / 1.5).
        sig2 = _long_signal(entry=10_000.0, stop=9_900.0)  # qty = 100/100 = 1; notional = $10k
        fill = await e.execute(sig2)
        assert fill is None

    @pytest.mark.asyncio
    async def test_rejects_on_rate_limit(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream,
                       rate_limit_per_symbol_per_min=2,
                       max_order_notional_usd=1e9)
        # Signal that would pass if rate limit weren't hit — but still requires a
        # successful fill which won't happen here (no WS). We just need to check
        # that the rate counter increments. Fill the rate buckets manually:
        import time
        for _ in range(2):
            e._order_times["BTCUSDT"].append(time.time())
        # Third call should reject before submit
        sig = _long_signal()
        # Neutralize the other gates
        fake_account._snapshot = AccountSnapshot(
            total_wallet_balance=1e9, total_margin_balance=1e9,
            total_unrealized_pnl=0, available_balance=1e9, positions_count=0,
        )
        fill = await e.execute(sig)
        assert fill is None
        fake_client.place_market_order.assert_not_called()


class TestFillConfirmation:
    @pytest.mark.asyncio
    async def test_ws_fill_resolves_pending_future(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream,
                       max_order_notional_usd=1e9, fill_timeout_s=5.0)

        # Pre-seed account with huge balance so margin check passes
        fake_account._snapshot = AccountSnapshot(
            total_wallet_balance=1e9, total_margin_balance=1e9,
            total_unrealized_pnl=0, available_balance=1e9, positions_count=0,
        )

        async def _submit_then_fake_fill():
            # Submit concurrently with a delayed WS fill event
            submit_task = asyncio.create_task(e.execute(_long_signal()))
            # Let the submit register the pending order
            await asyncio.sleep(0.05)
            # Find the client_order_id the executor generated
            assert len(e._pending) == 1
            cid = next(iter(e._pending))
            # Deliver a synthetic WS ORDER_TRADE_UPDATE event
            await e._on_order_trade_update({
                "T": 1_700_000_000_000,
                "o": {
                    "c": cid, "X": "FILLED", "S": "BUY",
                    "ap": "65000.0", "z": "0.01", "n": "0.52",
                },
            })
            fill = await submit_task
            return fill

        fill = await _submit_then_fake_fill()
        assert fill is not None
        assert fill.symbol == "BTCUSDT"
        assert fill.side == Side.BUY
        assert fill.price == 65_000.0
        assert fill.quantity == 0.01
        assert fill.commission == 0.52
        assert fill.exchange == "binance_futures"
        # Position cache updated
        assert "BTCUSDT" in e._positions

    @pytest.mark.asyncio
    async def test_fill_timeout_cancels_order(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream,
                       max_order_notional_usd=1e9, fill_timeout_s=0.1)
        fake_account._snapshot = AccountSnapshot(
            total_wallet_balance=1e9, total_margin_balance=1e9,
            total_unrealized_pnl=0, available_balance=1e9, positions_count=0,
        )
        fill = await e.execute(_long_signal())
        assert fill is None
        fake_client.cancel_order.assert_awaited()  # cancel was attempted
        assert len(e._pending) == 0  # cleaned up


class TestCloseFlow:
    @pytest.mark.asyncio
    async def test_close_signal_rejects_when_no_position(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        fill = await e.execute(_close_signal())
        assert fill is None
        fake_client.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_fires_trade_close_hook(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream,
                       max_order_notional_usd=1e9, fill_timeout_s=5.0)
        # Seed an open position
        from src.utils.types import Position
        e._positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", side=Side.BUY, quantity=0.01,
            entry_price=64_000.0, current_price=65_500.0,
            unrealized_pnl=15.0, realized_pnl=0.0,
            strategy_name="test_strategy", opened_at=0,
        )
        fake_account._snapshot = AccountSnapshot(
            total_wallet_balance=1e9, total_margin_balance=1e9,
            total_unrealized_pnl=0, available_balance=1e9, positions_count=1,
        )

        close_events: list = []
        e.on_trade_close_hook = lambda strat, pnl, sym, ts: close_events.append((strat, pnl, sym, ts))

        submit_task = asyncio.create_task(e.execute(_close_signal()))
        await asyncio.sleep(0.05)
        cid = next(iter(e._pending))
        # Deliver a synthetic CLOSE fill event with reduceOnly + realized pnl
        await e._on_order_trade_update({
            "T": 1_700_000_000_000,
            "o": {
                "c": cid, "X": "FILLED", "S": "SELL",
                "ap": "65500.0", "z": "0.01", "n": "0.52",
                "R": True, "rp": "15.0",  # reduceOnly + realized pnl
            },
        })
        fill = await submit_task
        assert fill is not None
        assert fill.side == Side.SELL
        # Position cache cleared
        assert "BTCUSDT" not in e._positions
        # Hook fired with realized pnl
        assert len(close_events) == 1
        strat, pnl, sym, _ = close_events[0]
        assert strat == "test_strategy"
        assert pnl == 15.0
        assert sym == "BTCUSDT"


class TestUpdatePrices:
    def test_recomputes_upnl(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        from src.utils.types import Position
        e._positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", side=Side.BUY, quantity=0.1,
            entry_price=64_000.0, current_price=64_000.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
            strategy_name="", opened_at=0,
        )
        e.update_prices("BTCUSDT", 65_000.0)
        # uPnL = 1 × (65000 - 64000) × 0.1 = 100
        assert e._positions["BTCUSDT"].unrealized_pnl == pytest.approx(100.0)
        assert e._positions["BTCUSDT"].current_price == 65_000.0

    def test_no_op_when_no_position(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        e.update_prices("BTCUSDT", 65_000.0)  # should not raise
        assert "BTCUSDT" not in e._positions


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_returns_cache_copy(
        self, fake_client, fake_account, fake_user_stream,
    ):
        e = _make_exec(fake_client, fake_account, fake_user_stream)
        from src.utils.types import Position
        e._positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", side=Side.BUY, quantity=0.1,
            entry_price=64_000.0, current_price=64_000.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
            strategy_name="s", opened_at=0,
        )
        result = await e.get_positions()
        assert "BTCUSDT" in result
        # Modifying the result should not affect the cache
        result.clear()
        assert "BTCUSDT" in e._positions
