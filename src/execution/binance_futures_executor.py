"""BinanceFuturesExecutor — live USD-M Perpetual execution.

Phase 3 of the Binance Futures live-trading stack. Consumes:
  - BinanceFuturesClient (REST) for order placement + account queries
  - BinanceFuturesUserDataFeed (WS) for ORDER_TRADE_UPDATE fill confirms
  - BinanceFuturesAccountManager for margin / balance cache

Implements BaseExecutor (async execute / get_positions / close_all) +
adds the same `on_trade_close_hook` contract PaperExecutor exposes so
the engine's M3S + audit wiring works unchanged.

Safety layers (all non-optional):
  1. `allow_live_trading = True` required at construction time.
  2. `max_order_notional_usd` — per-order cap.
  3. Rate limiter — max 10 orders/symbol/minute.
  4. Fill confirmation timeout — 5s; cancels order via REST if no fill.
  5. Position reconciliation on __aenter__ — local cache mirrors Binance.
  6. Startup ping + get_account — abort on connectivity failure.

This executor is REAL MONEY. Do not instantiate without setting
`allow_live_trading=True` AND the BINANCE_FUTURES_TESTNET env var to
`true` during development. Mainnet rollout is gated by explicit flag.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from src.execution.base import BaseExecutor
from src.execution.binance_futures_account import BinanceFuturesAccountManager
from src.execution.binance_futures_client import (
    BinanceAPIError,
    BinanceFuturesClient,
    BinanceFuturesUserDataFeed,
)
from src.utils.logger import get_logger
from src.utils.types import Fill, Position, Side, Signal, SignalAction

log = get_logger("binance_futures_executor")


@dataclass
class _PendingFill:
    """Tracks an in-flight order until the WS confirms the fill."""
    client_order_id: str
    symbol: str
    side: Side
    strategy_name: str
    future: asyncio.Future


class BinanceFuturesExecutor(BaseExecutor):
    """Live executor for Binance USD-M Perpetual Futures.

    Key timing guarantee: `execute(signal)` returns a Fill ONLY after
    Binance confirms the fill via the WS user-data stream (or None on
    timeout / safety reject). Never returns a "pending" Fill.
    """

    def __init__(
        self,
        client: BinanceFuturesClient,
        account: BinanceFuturesAccountManager,
        user_stream: BinanceFuturesUserDataFeed,
        *,
        allow_live_trading: bool = False,
        max_order_notional_usd: float = 500.0,
        rate_limit_per_symbol_per_min: int = 10,
        fill_timeout_s: float = 5.0,
        default_leverage: int = 1,
    ):
        if not allow_live_trading:
            raise RuntimeError(
                "BinanceFuturesExecutor requires allow_live_trading=True. "
                "This guard prevents accidental live orders. Set explicitly "
                "in the engine config when you are ready."
            )
        self._client = client
        self._account = account
        self._user_stream = user_stream
        self._max_notional = float(max_order_notional_usd)
        self._rate_limit = int(rate_limit_per_symbol_per_min)
        self._fill_timeout = float(fill_timeout_s)
        self._default_leverage = int(default_leverage)

        # In-flight orders waiting for WS confirmation
        self._pending: dict[str, _PendingFill] = {}

        # Per-symbol recent-order timestamps (for rate limiting)
        self._order_times: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=60))

        # Local position cache (reconciled from Binance on boot + after fills)
        self._positions: dict[str, Position] = {}

        # Kill switch — flipped by external code (e.g. risk client) if needed
        self._killed = False

        # Trade-close hook signature matches PaperExecutor:
        # (strategy: str, pnl: float, symbol: str, ts_ms: int) -> None
        self.on_trade_close_hook = None

        # Wire the user-data handler
        self._user_stream.on_order_trade_update = self._on_order_trade_update
        self._user_stream.on_account_update = self._on_account_update

    # ── Lifecycle ─────────────────────────────────────────────────

    async def init(self) -> None:
        """Pre-flight + reconciliation. Call once after construction."""
        # Connectivity check
        if not await self._client.ping():
            raise RuntimeError("Binance Futures ping failed — aborting executor boot")

        # Account access check (also verifies API key signing)
        try:
            snap = await self._account.refresh()
        except BinanceAPIError as e:
            raise RuntimeError(f"Binance Futures account check failed: {e}") from e
        log.info(
            "binance_futures_executor_ready",
            wallet=round(snap.total_wallet_balance, 2),
            available=round(snap.available_balance, 2),
            open_positions=snap.positions_count,
        )

        # Position reconciliation
        await self._reconcile_positions()

    async def _reconcile_positions(self) -> None:
        """Mirror Binance's live positions into the local cache."""
        raw_positions = await self._client.get_positions()
        self._positions.clear()
        for p in raw_positions:
            qty = float(p.get("positionAmt", 0))
            if qty == 0:
                continue
            symbol = p["symbol"]
            side = Side.BUY if qty > 0 else Side.SELL
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", entry))
            upnl = float(p.get("unRealizedProfit", 0))
            self._positions[symbol] = Position(
                symbol=symbol, side=side, quantity=abs(qty),
                entry_price=entry, current_price=mark,
                unrealized_pnl=upnl, realized_pnl=0.0,
                strategy_name="", opened_at=int(time.time() * 1000),
            )
        if self._positions:
            log.info("positions_reconciled", count=len(self._positions),
                     symbols=list(self._positions.keys()))

    # ── Safety gates ──────────────────────────────────────────────

    def _check_rate_limit(self, symbol: str) -> bool:
        """True if under the per-symbol-per-minute limit."""
        now = time.time()
        q = self._order_times[symbol]
        # Trim entries older than 60s
        while q and (now - q[0]) > 60.0:
            q.popleft()
        if len(q) >= self._rate_limit:
            return False
        q.append(now)
        return True

    def _check_notional_cap(self, qty: float, price: float) -> bool:
        return (qty * price) <= self._max_notional

    def kill(self) -> None:
        """External kill-switch flip — e.g. by watchdog or risk client."""
        self._killed = True
        log.critical("binance_futures_executor_killed")

    # ── Order submission ──────────────────────────────────────────

    async def execute(self, signal: Signal) -> Fill | None:
        """Execute a trading Signal.

        For LONG/SHORT: submit market order, wait for fill.
        For CLOSE: submit reduce-only market order in the opposite direction.
        Returns Fill on success, None on any safety-gate reject or timeout.
        """
        if self._killed:
            log.warning("order_rejected_kill_switch", strategy=signal.strategy_name)
            return None

        symbol = signal.symbol.upper()

        # Resolve side + reduce_only from action
        if signal.action == SignalAction.LONG:
            side, reduce_only = "BUY", False
        elif signal.action == SignalAction.SHORT:
            side, reduce_only = "SELL", False
        elif signal.action == SignalAction.CLOSE:
            existing = self._positions.get(symbol)
            if not existing:
                log.warning("close_signal_no_position", symbol=symbol)
                return None
            # Close is opposite side of existing position
            side = "SELL" if existing.side == Side.BUY else "BUY"
            reduce_only = True
        else:
            log.debug("signal_ignored", action=signal.action)
            return None

        # Compute quantity
        if signal.action == SignalAction.CLOSE:
            existing = self._positions[symbol]
            qty = existing.quantity
        else:
            qty = self._compute_quantity(signal)
            if qty is None:
                return None

        # Get a reference price for notional check
        entry_ref = signal.entry_price or await self._best_price_estimate(symbol)
        if entry_ref is None or entry_ref <= 0:
            log.warning("order_no_reference_price", symbol=symbol)
            return None

        # ── Safety gates ──
        if not self._check_rate_limit(symbol):
            log.warning("order_rejected_rate_limit", symbol=symbol,
                        strategy=signal.strategy_name)
            return None
        if not self._check_notional_cap(qty, entry_ref):
            log.warning("order_rejected_notional_cap",
                        symbol=symbol, qty=qty, price=entry_ref,
                        notional=qty * entry_ref, cap=self._max_notional,
                        strategy=signal.strategy_name)
            return None
        if not reduce_only:
            # Don't bother with margin check on closes (Binance always lets you close)
            if not await self._account.can_afford(qty * entry_ref, self._default_leverage):
                log.warning("order_rejected_insufficient_margin",
                            symbol=symbol, notional=qty * entry_ref,
                            strategy=signal.strategy_name)
                return None

        # ── Submit + wait for fill ──
        client_order_id = f"algo-{uuid.uuid4().hex[:16]}"
        future: asyncio.Future[Fill | None] = asyncio.get_running_loop().create_future()
        self._pending[client_order_id] = _PendingFill(
            client_order_id=client_order_id,
            symbol=symbol,
            side=Side(side),
            strategy_name=signal.strategy_name or "",
            future=future,
        )

        try:
            await self._client.place_market_order(
                symbol=symbol, side=side, quantity=qty,
                client_order_id=client_order_id, reduce_only=reduce_only,
            )
        except BinanceAPIError as e:
            self._pending.pop(client_order_id, None)
            log.error("order_submit_failed", symbol=symbol, side=side,
                      qty=qty, code=e.code, msg=e.msg)
            return None
        except Exception as e:
            self._pending.pop(client_order_id, None)
            log.error("order_submit_error", symbol=symbol, error=str(e))
            return None

        log.info("order_submitted", symbol=symbol, side=side, qty=qty,
                 client_order_id=client_order_id, reduce_only=reduce_only,
                 strategy=signal.strategy_name)

        try:
            fill = await asyncio.wait_for(future, timeout=self._fill_timeout)
        except asyncio.TimeoutError:
            log.warning("fill_timeout_cancelling", client_order_id=client_order_id)
            self._pending.pop(client_order_id, None)
            # Best-effort cancel
            try:
                await self._client.cancel_order(symbol, client_order_id=client_order_id)
            except Exception as e:
                log.warning("cancel_after_timeout_failed", error=str(e))
            return None

        return fill

    def _compute_quantity(self, signal: Signal) -> float | None:
        """Translate Signal.risk_pct + stop distance into a quantity.

        For LONG/SHORT with a stop_loss, qty = (equity × risk_pct) / |entry - stop|.
        If the risk gate has pre-computed `adjusted_risk_pct`, the engine
        will already have passed us a stop-adjusted signal — we honor it.
        """
        entry = signal.entry_price or 0
        stop = signal.stop_loss or 0
        risk = signal.risk_pct or 0.01
        if entry <= 0 or stop <= 0:
            return None
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return None
        # Use total wallet balance as the risk base for the new path.
        # Paper-path used `self._equity`; futures path uses Binance's own balance.
        snap = self._account._snapshot
        equity = snap.total_wallet_balance if snap else 0.0
        if equity <= 0:
            return None
        qty_raw = (equity * risk) / stop_dist
        # Round to 3 decimals for BTC (Binance precision varies by symbol; conservative).
        return round(qty_raw, 3)

    async def _best_price_estimate(self, symbol: str) -> float | None:
        """Crude price reference for notional checks when signal lacks entry_price."""
        pos = self._positions.get(symbol)
        if pos:
            return pos.current_price
        return None

    # ── User-data stream handlers ─────────────────────────────────

    async def _on_order_trade_update(self, msg: dict) -> None:
        """Handle ORDER_TRADE_UPDATE — the main fill-confirmation path."""
        o = msg.get("o", {})
        client_order_id = o.get("c", "")
        status = o.get("X", "")  # NEW / PARTIALLY_FILLED / FILLED / CANCELED / etc.

        if status not in ("FILLED", "PARTIALLY_FILLED"):
            return  # Ignore non-fill updates

        pending = self._pending.get(client_order_id)
        if pending is None:
            # This can happen if the executor restarted mid-flight OR if
            # it's a manual order. Reconcile positions either way.
            log.debug("orphan_fill_event", client_order_id=client_order_id)
            await self._reconcile_positions()
            return

        # Parse the fill
        avg_price = float(o.get("ap", 0) or o.get("p", 0))
        executed_qty = float(o.get("z", 0) or o.get("q", 0))  # z = cumulative filled qty
        commission = float(o.get("n", 0) or 0)  # commission amount
        ts = int(msg.get("T", 0) or time.time() * 1000)

        # Only finalize on FULL fill
        if status != "FILLED":
            return

        side_str = o.get("S", "BUY")
        side = Side.BUY if side_str == "BUY" else Side.SELL
        fill = Fill(
            order_id=client_order_id,
            symbol=pending.symbol,
            side=side,
            price=avg_price,
            quantity=executed_qty,
            commission=commission,
            timestamp=ts,
            exchange="binance_futures",
        )

        # Realized-pnl fire on a close fill
        was_close = o.get("R") is True or o.get("cp") is True  # "reduceOnly" or "closePosition"
        realized_pnl = float(o.get("rp", 0) or 0)

        # Update local cache
        self._update_position_cache(pending, fill, was_close=was_close)

        # Remove from pending + signal the future
        self._pending.pop(client_order_id, None)
        if not pending.future.done():
            pending.future.set_result(fill)

        # Invalidate account cache — balance just changed
        self._account.invalidate()

        if was_close and self.on_trade_close_hook and pending.strategy_name:
            try:
                self.on_trade_close_hook(
                    pending.strategy_name, realized_pnl, pending.symbol, ts,
                )
            except Exception as e:
                log.warning("trade_close_hook_failed", error=str(e))

        log.info(
            "fill_confirmed", symbol=fill.symbol, side=side.value,
            qty=fill.quantity, price=fill.price, commission=fill.commission,
            was_close=was_close, realized_pnl=realized_pnl,
            strategy=pending.strategy_name,
        )

    def _update_position_cache(
        self, pending: _PendingFill, fill: Fill, *, was_close: bool,
    ) -> None:
        """Keep local position cache in sync after a fill."""
        symbol = fill.symbol
        if was_close:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                side=fill.side,
                quantity=fill.quantity,
                entry_price=fill.price,
                current_price=fill.price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                strategy_name=pending.strategy_name,
                opened_at=fill.timestamp,
            )

    async def _on_account_update(self, msg: dict) -> None:
        """ACCOUNT_UPDATE events tell us balance + position deltas."""
        # We choose to defer heavy parsing — just invalidate the cache
        # so the next can_afford() call refreshes.
        self._account.invalidate()

    # ── BaseExecutor contract ─────────────────────────────────────

    async def get_positions(self) -> dict[str, Position]:
        """Return the cached positions (reconciled on boot + after each fill)."""
        return dict(self._positions)

    async def close_all(self) -> list[Fill]:
        """Emergency: flatten every open position via reduce-only market orders."""
        fills: list[Fill] = []
        for symbol, pos in list(self._positions.items()):
            close_side = "SELL" if pos.side == Side.BUY else "BUY"
            client_order_id = f"close-all-{uuid.uuid4().hex[:12]}"
            future: asyncio.Future[Fill | None] = asyncio.get_running_loop().create_future()
            self._pending[client_order_id] = _PendingFill(
                client_order_id=client_order_id,
                symbol=symbol, side=Side(close_side),
                strategy_name=pos.strategy_name, future=future,
            )
            try:
                await self._client.place_market_order(
                    symbol=symbol, side=close_side, quantity=pos.quantity,
                    client_order_id=client_order_id, reduce_only=True,
                )
                fill = await asyncio.wait_for(future, timeout=self._fill_timeout)
                if fill:
                    fills.append(fill)
            except Exception as e:
                log.error("close_all_failed", symbol=symbol, error=str(e))
                self._pending.pop(client_order_id, None)
        return fills

    # ── Paper-executor parity helpers (so engine wiring is symmetric) ──

    def update_prices(self, symbol: str, price: float) -> None:
        """Update local mark price for a position. No-op on live — Binance
        is source of truth for marks. Present for interface parity."""
        pos = self._positions.get(symbol)
        if pos:
            # Recompute uPnL based on the latest tick price
            sign = 1 if pos.side == Side.BUY else -1
            upnl = sign * (price - pos.entry_price) * pos.quantity
            self._positions[symbol] = Position(
                symbol=pos.symbol, side=pos.side, quantity=pos.quantity,
                entry_price=pos.entry_price, current_price=price,
                unrealized_pnl=upnl, realized_pnl=pos.realized_pnl,
                strategy_name=pos.strategy_name, opened_at=pos.opened_at,
            )

    @property
    def equity(self) -> float:
        """Total wallet balance from the cached account snapshot."""
        snap = self._account._snapshot
        return snap.total_wallet_balance if snap else 0.0
