"""Paper executor — simulates trade fills for paper trading mode.

Receives Signals, creates simulated Fills, tracks positions and P&L.
Logs all trades to SQLite via Storage.

Usage:
    executor = PaperExecutor(storage=storage)
    fill = await executor.execute(signal)
"""

from __future__ import annotations

import sqlite3
import time
import uuid

from src.data.storage import Storage
from src.execution.base import BaseExecutor
from src.utils.logger import get_logger
from src.utils.types import Fill, OrderRequest, Position, Side, Signal, SignalAction

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
        self._last_price_persist: float = 0.0  # throttle price updates to DB

        # Optional trade-close callback (M3S hook, sub-phase 0.8).
        # Signature: (strategy: str, pnl: float, symbol: str, ts_ms: int) -> None
        # Set externally after construction.
        self.on_trade_close_hook = None

    async def execute(self, signal: Signal) -> Fill | None:
        """Execute a signal with simulated fill."""
        if signal.action == SignalAction.HOLD:
            return None

        # Handle CLOSE
        if signal.action == SignalAction.CLOSE:
            return await self._close_position(signal)

        # Handle LONG / SHORT
        return await self._open_position(signal)

    async def execute_order(self, order: OrderRequest) -> Fill | None:
        """Execute a pre-sized order from the risk manager.

        Unlike execute(signal), this does NOT recompute sizing — the quantity
        is already determined by the risk manager. Only applies slippage and
        commission.
        """
        symbol = order.symbol

        if symbol in self._positions:
            log.warning("position_exists", symbol=symbol, side=order.side.value)
            return None

        entry_price = order.price or 0
        if entry_price <= 0:
            log.warning("no_entry_price", symbol=symbol)
            return None

        # Apply slippage
        if order.side == Side.BUY:
            fill_price = entry_price * (1 + self._slippage_pct)
        else:
            fill_price = entry_price * (1 - self._slippage_pct)

        quantity = order.quantity
        commission = fill_price * quantity * self._commission_pct
        self._equity -= commission  # Deduct opening commission immediately
        self._trade_count += 1

        fill = Fill(
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            symbol=symbol,
            side=order.side,
            price=fill_price,
            quantity=quantity,
            commission=commission,
            timestamp=int(time.time() * 1000),
            exchange="paper",
        )

        self._positions[symbol] = Position(
            symbol=symbol,
            side=order.side,
            quantity=quantity,
            entry_price=fill_price,
            current_price=fill_price,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            strategy_name=order.strategy_name,
            opened_at=fill.timestamp,
        )

        self._fills.append(fill)

        if self._storage:
            await self._storage.trade_log.log_trade(fill, strategy=order.strategy_name)

        # Persist position + equity for crash recovery
        self._persist_position(self._positions[symbol])
        self._persist_equity()

        log.info(
            "paper_fill_order",
            side=order.side.value,
            symbol=symbol,
            price=round(fill_price, 2),
            qty=round(quantity, 6),
            commission=round(commission, 4),
        )

        return fill

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
        self._equity -= commission  # Deduct opening commission immediately
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

        # Persist position + equity for crash recovery
        self._persist_position(self._positions[symbol])
        self._persist_equity()

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

        # Persist: remove closed position, update equity
        self._remove_position(symbol)
        self._persist_equity()

        # M3S hook (sub-phase 0.8): notify of realized PnL per closed trade.
        # Safe no-op when not wired (hook is None by default).
        if self.on_trade_close_hook is not None:
            try:
                self.on_trade_close_hook(pos.strategy_name, net_pnl, symbol, fill.timestamp)
            except Exception as e:
                log.warning("on_trade_close_hook_failed", error=str(e))

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

        # Throttled persist of current prices (every 10s, not every tick)
        self._persist_prices_batch()

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

    # ── Position Persistence (crash recovery) ─────────────────────────────

    def _get_db(self) -> sqlite3.Connection | None:
        """Get the SQLite connection from storage (if available)."""
        if self._storage and self._storage.trade_log._conn:
            return self._storage.trade_log._conn
        return None

    def _persist_position(self, pos: Position) -> None:
        """Save or update a position in SQLite."""
        conn = self._get_db()
        if conn is None:
            return
        conn.execute(
            "INSERT OR REPLACE INTO paper_positions "
            "(symbol, side, quantity, entry_price, current_price, unrealized_pnl, "
            "strategy_name, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pos.symbol, pos.side.value, pos.quantity, pos.entry_price,
             pos.current_price, pos.unrealized_pnl, pos.strategy_name, pos.opened_at),
        )
        conn.commit()

    def _remove_position(self, symbol: str) -> None:
        """Remove a closed position from SQLite."""
        conn = self._get_db()
        if conn is None:
            return
        conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (symbol,))
        conn.commit()

    def _persist_equity(self) -> None:
        """Save equity state to SQLite."""
        conn = self._get_db()
        if conn is None:
            return
        conn.execute(
            "INSERT OR REPLACE INTO paper_equity (id, equity, initial_capital, trade_count) "
            "VALUES (1, ?, ?, ?)",
            (self._equity, self._capital, self._trade_count),
        )
        conn.commit()

    def _persist_prices_batch(self) -> None:
        """Batch-update current prices for all open positions (throttled)."""
        now = time.time()
        if now - self._last_price_persist < 10.0:
            return
        self._last_price_persist = now

        conn = self._get_db()
        if conn is None or not self._positions:
            return
        for pos in self._positions.values():
            conn.execute(
                "UPDATE paper_positions SET current_price = ?, unrealized_pnl = ? "
                "WHERE symbol = ?",
                (pos.current_price, pos.unrealized_pnl, pos.symbol),
            )
        conn.commit()

    def restore_state(self) -> int:
        """Restore positions and equity from SQLite after a crash/restart.

        Returns the number of positions restored.
        """
        conn = self._get_db()
        if conn is None:
            return 0

        # Restore equity
        row = conn.execute(
            "SELECT equity, initial_capital, trade_count FROM paper_equity WHERE id = 1"
        ).fetchone()
        if row:
            self._equity = row[0]
            self._capital = row[1]
            self._trade_count = row[2]
            log.info("equity_restored", equity=round(self._equity, 2),
                     initial=self._capital, trades=self._trade_count)

        # Restore positions
        rows = conn.execute(
            "SELECT symbol, side, quantity, entry_price, current_price, "
            "unrealized_pnl, strategy_name, opened_at FROM paper_positions"
        ).fetchall()

        for r in rows:
            self._positions[r[0]] = Position(
                symbol=r[0],
                side=Side(r[1]),
                quantity=r[2],
                entry_price=r[3],
                current_price=r[4],
                unrealized_pnl=r[5],
                realized_pnl=0.0,
                strategy_name=r[6],
                opened_at=r[7],
            )

        if rows:
            log.info("positions_restored", count=len(rows),
                     symbols=[r[0] for r in rows])

        return len(rows)
