"""Paper executor — simulates trade fills for paper trading mode.

Receives Signals, creates simulated Fills, tracks positions and P&L.
Logs all trades to SQLite via Storage.

Usage:
    executor = PaperExecutor(storage=storage)
    fill = await executor.execute(signal)
"""

from __future__ import annotations

import time
import uuid

from src.data.storage import Storage
from src.execution.base import BaseExecutor
from src.utils.logger import get_logger
from src.utils.types import Fill, Position, Side, Signal, SignalAction

log = get_logger("paper_executor")


class PaperExecutor(BaseExecutor):
    """Simulated trade executor for paper trading."""

    def __init__(
        self,
        storage: Storage | None = None,
        initial_capital: float = 10_000.0,
        slippage_pct: float = 0.0002,
        commission_pct: float = 0.001,
    ):
        self._storage = storage
        self._capital = initial_capital
        self._equity = initial_capital
        self._slippage_pct = slippage_pct
        self._commission_pct = commission_pct

        # Open positions: symbol -> Position
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._trade_count = 0

    async def execute(self, signal: Signal) -> Fill | None:
        """Execute a signal with simulated fill."""
        if signal.action == SignalAction.HOLD:
            return None

        # Handle CLOSE
        if signal.action == SignalAction.CLOSE:
            return await self._close_position(signal)

        # Handle LONG / SHORT
        return await self._open_position(signal)

    async def _open_position(self, signal: Signal) -> Fill | None:
        """Open a new position."""
        symbol = signal.symbol

        # Reject if already in a position for this symbol
        if symbol in self._positions:
            log.warning("position_exists", symbol=symbol, action=signal.action.value)
            return None

        entry_price = signal.entry_price or 0
        if entry_price <= 0:
            log.warning("no_entry_price", symbol=symbol)
            return None

        # Apply slippage
        if signal.action == SignalAction.LONG:
            fill_price = entry_price * (1 + self._slippage_pct)
            side = Side.BUY
        else:
            fill_price = entry_price * (1 - self._slippage_pct)
            side = Side.SELL

        # Position sizing: risk-based
        risk_pct = signal.risk_pct or 0.01
        if signal.stop_loss and fill_price != signal.stop_loss:
            risk_per_unit = abs(fill_price - signal.stop_loss)
            risk_amount = self._equity * risk_pct
            quantity = risk_amount / risk_per_unit
        else:
            quantity = (self._equity * risk_pct) / fill_price

        commission = fill_price * quantity * self._commission_pct
        self._trade_count += 1

        fill = Fill(
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            symbol=symbol,
            side=side,
            price=fill_price,
            quantity=quantity,
            commission=commission,
            timestamp=int(time.time() * 1000),
            exchange="paper",
        )

        # Track position
        self._positions[symbol] = Position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=fill_price,
            current_price=fill_price,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            strategy_name=signal.strategy_name,
            opened_at=fill.timestamp,
        )

        self._fills.append(fill)

        # Log to SQLite
        if self._storage:
            await self._storage.trade_log.log_trade(fill, strategy=signal.strategy_name)

        log.info(
            "paper_fill",
            action=signal.action.value,
            symbol=symbol,
            price=round(fill_price, 2),
            qty=round(quantity, 6),
            commission=round(commission, 4),
        )

        return fill

    async def _close_position(self, signal: Signal) -> Fill | None:
        """Close an existing position."""
        symbol = signal.symbol
        pos = self._positions.get(symbol)
        if pos is None:
            log.warning("no_position_to_close", symbol=symbol)
            return None

        exit_price = signal.entry_price or pos.current_price

        # Apply slippage (opposite direction)
        if pos.side == Side.BUY:
            fill_price = exit_price * (1 - self._slippage_pct)
            side = Side.SELL
            pnl = (fill_price - pos.entry_price) * pos.quantity
        else:
            fill_price = exit_price * (1 + self._slippage_pct)
            side = Side.BUY
            pnl = (pos.entry_price - fill_price) * pos.quantity

        commission = fill_price * pos.quantity * self._commission_pct
        net_pnl = pnl - commission
        self._equity += net_pnl
        self._trade_count += 1

        fill = Fill(
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            symbol=symbol,
            side=side,
            price=fill_price,
            quantity=pos.quantity,
            commission=commission,
            timestamp=int(time.time() * 1000),
            exchange="paper",
        )

        self._fills.append(fill)
        del self._positions[symbol]

        # Log to SQLite
        if self._storage:
            await self._storage.trade_log.log_trade(fill, strategy=pos.strategy_name)

        log.info(
            "paper_close",
            symbol=symbol,
            price=round(fill_price, 2),
            pnl=round(net_pnl, 2),
            equity=round(self._equity, 2),
        )

        return fill

    def update_prices(self, symbol: str, price: float) -> None:
        """Update unrealized P&L for a position (called on each tick)."""
        pos = self._positions.get(symbol)
        if pos is None:
            return

        pos.current_price = price
        if pos.side == Side.BUY:
            pos.unrealized_pnl = (price - pos.entry_price) * pos.quantity
        else:
            pos.unrealized_pnl = (pos.entry_price - price) * pos.quantity

    async def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    async def close_all(self) -> list[Fill]:
        fills = []
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            signal = Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=1.0,
                strategy_name=pos.strategy_name,
                timeframe="",
                entry_price=pos.current_price,
            )
            fill = await self._close_position(signal)
            if fill:
                fills.append(fill)
        return fills

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def stats(self) -> dict:
        return {
            "equity": round(self._equity, 2),
            "initial_capital": self._capital,
            "return_pct": round((self._equity - self._capital) / self._capital * 100, 2),
            "open_positions": len(self._positions),
            "total_trades": self._trade_count,
            "total_fills": len(self._fills),
        }
