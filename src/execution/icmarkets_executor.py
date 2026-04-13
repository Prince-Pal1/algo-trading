"""IC Markets cTrader Open API executor."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from src.execution.base import BaseExecutor
from src.utils.logger import get_logger
from src.utils.types import Fill, Position, Side, Signal, SignalAction

try:
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAClosePositionReq,
        ProtoOAExecutionEvent,
        ProtoOANewOrderReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType,
        ProtoOATradeSide,
    )
    CTRADER_AVAILABLE = True
except ImportError:
    CTRADER_AVAILABLE = False


log = get_logger("icmarkets_executor")


@dataclass
class _PendingOrder:
    client_msg_id: str
    symbol: str
    side: Side
    quantity: float
    submitted_ts_ms: int
    future: asyncio.Future


class ICMarketsExecutor(BaseExecutor):
    """cTrader Open API executor.

    Implements BaseExecutor so the TradingEngine can swap between
    PaperExecutor and ICMarketsExecutor via config. Order placement is
    ASYNCHRONOUS in cTrader: send ProtoOANewOrderReq, receive an
    ProtoOAExecutionEvent back on the message bus, correlate via
    clientMsgId, resolve the Future → caller gets the Fill.
    """

    def __init__(
        self,
        *,
        account_id: int,
        symbol_id_map: dict[str, int],
        send_message,  # Callable[[protobuf_message], None] — from the feed's client
    ) -> None:
        if not CTRADER_AVAILABLE:
            raise ImportError(
                "ctrader-open-api is not installed. Run `pip install ctrader-open-api`."
            )
        self._account_id = account_id
        self._symbol_id_map = symbol_id_map
        self._send_message = send_message
        self._positions: dict[str, Position] = {}
        self._pending: dict[str, _PendingOrder] = {}

    async def execute(self, signal: Signal) -> Fill | None:
        if signal.action == SignalAction.HOLD:
            return None
        if signal.action == SignalAction.CLOSE:
            return await self._close_position(signal.symbol)

        side = Side.BUY if signal.action == SignalAction.LONG else Side.SELL
        quantity = self._derive_quantity(signal)
        if quantity <= 0:
            log.warning("icmarkets_zero_quantity", symbol=signal.symbol)
            return None

        return await self._submit_order(
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

    async def _submit_order(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Fill | None:
        symbol_id = self._symbol_id_map.get(symbol)
        if symbol_id is None:
            log.warning("icmarkets_unknown_symbol", symbol=symbol)
            return None

        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = (
            ProtoOATradeSide.BUY if side == Side.BUY else ProtoOATradeSide.SELL
        )
        req.volume = int(quantity * 100)  # cTrader expects "volume in hundredths"
        if stop_loss is not None:
            req.stopLoss = float(stop_loss)
        if take_profit is not None:
            req.takeProfit = float(take_profit)

        client_msg_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        pending = _PendingOrder(
            client_msg_id=client_msg_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            submitted_ts_ms=int(time.time() * 1000),
            future=future,
        )
        self._pending[client_msg_id] = pending

        try:
            self._send_message(req, clientMsgId=client_msg_id)
        except TypeError:
            # Older client versions take just the message
            self._send_message(req)

        try:
            fill = await asyncio.wait_for(future, timeout=10.0)
            return fill
        except asyncio.TimeoutError:
            log.warning("icmarkets_order_timeout", symbol=symbol, client_msg_id=client_msg_id)
            self._pending.pop(client_msg_id, None)
            return None

    async def _close_position(self, symbol: str) -> Fill | None:
        position = self._positions.get(symbol)
        if position is None:
            log.debug("icmarkets_close_no_position", symbol=symbol)
            return None

        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._account_id
        # cTrader close requires position_id — we'd get this from reconcile
        # in the real flow. Placeholder — full flow lands in phase G.3 wiring.
        log.info("icmarkets_close_requested", symbol=symbol)
        return None

    def on_execution_event(self, event: "ProtoOAExecutionEvent") -> None:
        """Called by the feed's message dispatcher when an execution event arrives."""
        client_msg_id = getattr(event, "clientMsgId", None) or ""
        pending = self._pending.pop(client_msg_id, None)
        if pending is None:
            return

        deal = event.deal if event.HasField("deal") else None
        if deal is None:
            return

        fill = Fill(
            order_id=str(deal.dealId),
            symbol=pending.symbol,
            side=pending.side,
            price=float(deal.executionPrice),
            quantity=float(deal.filledVolume) / 100.0,
            commission=float(deal.commission) / 100.0,
            timestamp=int(deal.executionTimestamp),
            exchange="icmarkets",
        )
        if not pending.future.done():
            pending.future.set_result(fill)

    def _derive_quantity(self, signal: Signal) -> float:
        risk_pct = signal.risk_pct if signal.risk_pct is not None else 0.01
        return 100.0 * risk_pct  # placeholder — the real sizing lives in the engine

    async def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    async def close_all(self) -> list[Fill]:
        fills: list[Fill] = []
        for symbol in list(self._positions):
            fill = await self._close_position(symbol)
            if fill is not None:
                fills.append(fill)
        return fills
