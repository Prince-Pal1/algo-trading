from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.execution.icmarkets_executor import CTRADER_AVAILABLE, ICMarketsExecutor
from src.utils.types import Side, Signal, SignalAction

pytestmark = pytest.mark.skipif(
    not CTRADER_AVAILABLE,
    reason="ctrader-open-api not installed",
)


def _signal(
    action: SignalAction = SignalAction.LONG,
    symbol: str = "XAUUSD",
    risk_pct: float | None = 0.01,
) -> Signal:
    return Signal(
        symbol=symbol,
        action=action,
        confidence=0.8,
        strategy_name="test",
        timeframe="5m",
        risk_pct=risk_pct,
        entry_price=2400.0,
        stop_loss=2395.0,
        take_profit=2420.0,
    )


class TestConstruction:
    def test_construct(self):
        executor = ICMarketsExecutor(
            account_id=5012345,
            symbol_id_map={"XAUUSD": 41},
            send_message=MagicMock(),
        )
        assert executor._account_id == 5012345
        assert executor._symbol_id_map == {"XAUUSD": 41}


class TestExecute:
    @pytest.mark.asyncio
    async def test_hold_returns_none(self):
        executor = ICMarketsExecutor(
            account_id=1, symbol_id_map={"XAUUSD": 41}, send_message=MagicMock(),
        )
        assert await executor.execute(_signal(action=SignalAction.HOLD)) is None

    @pytest.mark.asyncio
    async def test_unknown_symbol_rejected(self):
        executor = ICMarketsExecutor(
            account_id=1, symbol_id_map={"EURUSD": 1}, send_message=MagicMock(),
        )
        result = await executor.execute(_signal(symbol="XAUUSD"))
        assert result is None

    @pytest.mark.asyncio
    async def test_long_signal_sends_order_request(self):
        send_mock = MagicMock()
        executor = ICMarketsExecutor(
            account_id=5012345,
            symbol_id_map={"XAUUSD": 41},
            send_message=send_mock,
        )
        # Won't return a real fill (no execution event arrives), but the
        # send_message mock should have been called with a NewOrderReq.
        task = asyncio.create_task(executor.execute(_signal(action=SignalAction.LONG)))
        await asyncio.sleep(0.1)
        send_mock.assert_called_once()
        # Task will time out on the future — cancel it
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_zero_quantity_skips_order(self):
        send_mock = MagicMock()
        executor = ICMarketsExecutor(
            account_id=1, symbol_id_map={"XAUUSD": 41}, send_message=send_mock,
        )
        # risk_pct=0 → derived quantity = 0
        result = await executor.execute(_signal(risk_pct=0.0))
        assert result is None
        send_mock.assert_not_called()


class TestExecutionEventHandling:
    def test_pending_order_resolves_on_execution_event(self):
        send_mock = MagicMock()
        executor = ICMarketsExecutor(
            account_id=1, symbol_id_map={"XAUUSD": 41}, send_message=send_mock,
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            future = loop.create_future()
            executor._pending["test_msg_id"] = type(
                "_P", (), {
                    "client_msg_id": "test_msg_id",
                    "symbol": "XAUUSD",
                    "side": Side.BUY,
                    "quantity": 1.0,
                    "submitted_ts_ms": 0,
                    "future": future,
                },
            )()

            fake_deal = MagicMock()
            fake_deal.dealId = 42
            fake_deal.executionPrice = 2400.5
            fake_deal.filledVolume = 100  # = 1.0 oz
            fake_deal.commission = 30     # = $0.30
            fake_deal.executionTimestamp = 1_700_000_000_000

            fake_event = MagicMock()
            fake_event.clientMsgId = "test_msg_id"
            fake_event.HasField = lambda field: True
            fake_event.deal = fake_deal

            executor.on_execution_event(fake_event)

            assert future.done()
            fill = future.result()
            assert fill.symbol == "XAUUSD"
            assert fill.price == 2400.5
            assert fill.quantity == 1.0
        finally:
            loop.close()


class TestPositions:
    @pytest.mark.asyncio
    async def test_get_positions_empty(self):
        executor = ICMarketsExecutor(
            account_id=1, symbol_id_map={}, send_message=MagicMock(),
        )
        positions = await executor.get_positions()
        assert positions == {}

    @pytest.mark.asyncio
    async def test_close_all_with_no_positions(self):
        executor = ICMarketsExecutor(
            account_id=1, symbol_id_map={}, send_message=MagicMock(),
        )
        assert await executor.close_all() == []
