"""Tests for PaperExecutor — position tracking, P&L, edge cases."""

from __future__ import annotations

import pytest

from src.execution.paper_executor import PaperExecutor
from src.utils.types import Fill, Signal, SignalAction, Side


# ── Helpers ──────────────────────────────────────────────────────────────────

def _signal(symbol: str, action: SignalAction, entry: float = 100.0,
            sl: float | None = 95.0, tp: float | None = 110.0,
            risk_pct: float = 0.01) -> Signal:
    return Signal(
        symbol=symbol, action=action, confidence=0.8,
        strategy_name="test_strat", timeframe="1h",
        entry_price=entry, stop_loss=sl, take_profit=tp,
        risk_pct=risk_pct,
    )


def _close_signal(symbol: str, exit_price: float) -> Signal:
    return Signal(
        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
        strategy_name="test_strat", timeframe="1h",
        entry_price=exit_price,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

class TestOpenLongPosition:
    async def test_open_long_position(self):
        """LONG signal → Fill returned, position tracked, equity unchanged (no close yet)."""
        executor = PaperExecutor(storage=None, initial_capital=10_000.0)
        fill = await executor.execute(_signal("BTCUSDT", SignalAction.LONG))

        assert fill is not None
        assert fill.side == Side.BUY
        assert fill.symbol == "BTCUSDT"
        assert fill.quantity > 0
        assert fill.commission > 0
        assert ("test_strat", "BTCUSDT") in executor._positions


class TestOpenShortPosition:
    async def test_open_short_position(self):
        """SHORT signal → Fill with SELL side, position tracked."""
        executor = PaperExecutor(storage=None, initial_capital=10_000.0)
        fill = await executor.execute(_signal("ETHUSDT", SignalAction.SHORT, entry=3000.0, sl=3100.0))

        assert fill is not None
        assert fill.side == Side.SELL
        assert ("test_strat", "ETHUSDT") in executor._positions
        assert executor._positions[("test_strat", "ETHUSDT")].side == Side.SELL


class TestClosePositionPnl:
    async def test_close_position_pnl(self):
        """Open LONG at 100, close at 110 → equity increases."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000.0,
            slippage_pct=0.0, commission_pct=0.0,  # Zero for clean P&L check
        )
        await executor.execute(_signal("BTCUSDT", SignalAction.LONG, entry=100.0, sl=95.0))

        pos = executor._positions[("test_strat", "BTCUSDT")]
        qty = pos.quantity

        close_fill = await executor.execute(_close_signal("BTCUSDT", 110.0))

        assert close_fill is not None
        assert ("test_strat", "BTCUSDT") not in executor._positions
        # P&L = (110 - 100) * qty = 10 * qty
        expected_pnl = (110.0 - 100.0) * qty
        assert executor.equity == pytest.approx(10_000.0 + expected_pnl)

    async def test_close_short_pnl(self):
        """Open SHORT at 100, close at 90 → equity increases."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000.0,
            slippage_pct=0.0, commission_pct=0.0,
        )
        await executor.execute(_signal("ETHUSDT", SignalAction.SHORT, entry=100.0, sl=105.0))

        pos = executor._positions[("test_strat", "ETHUSDT")]
        qty = pos.quantity

        close_fill = await executor.execute(_close_signal("ETHUSDT", 90.0))

        assert close_fill is not None
        expected_pnl = (100.0 - 90.0) * qty
        assert executor.equity == pytest.approx(10_000.0 + expected_pnl)


class TestDuplicatePositionRejected:
    async def test_duplicate_position_rejected(self):
        """Second LONG on same (strategy, symbol) → returns None."""
        executor = PaperExecutor(storage=None)
        fill1 = await executor.execute(_signal("BTCUSDT", SignalAction.LONG))
        fill2 = await executor.execute(_signal("BTCUSDT", SignalAction.LONG))

        assert fill1 is not None
        assert fill2 is None


class TestMultiStrategySameSymbol:
    """Two strategies can hold the same symbol concurrently; each closes its own."""

    def _sig(self, strat, action, entry=100.0, sl=95.0):
        return Signal(
            symbol="BTCUSDT", action=action, confidence=0.8,
            strategy_name=strat, timeframe="1h",
            entry_price=entry, stop_loss=sl, risk_pct=0.01,
        )

    async def test_two_strategies_same_symbol(self):
        ex = PaperExecutor(storage=None, slippage_pct=0.0, commission_pct=0.0)
        f1 = await ex.execute(self._sig("vol_momentum", SignalAction.LONG))
        f2 = await ex.execute(self._sig("adaptive_momentum", SignalAction.LONG))

        # Both open independently — no "position_exists" rejection.
        assert f1 is not None and f2 is not None
        assert ("vol_momentum", "BTCUSDT") in ex._positions
        assert ("adaptive_momentum", "BTCUSDT") in ex._positions
        assert len(ex._positions) == 2

        # update_prices marks BOTH positions on the symbol.
        ex.update_prices("BTCUSDT", 110.0)
        assert ex._positions[("vol_momentum", "BTCUSDT")].current_price == 110.0
        assert ex._positions[("adaptive_momentum", "BTCUSDT")].current_price == 110.0

        # Closing one leaves the other intact (no cross-strategy interference).
        close = self._sig("vol_momentum", SignalAction.CLOSE, entry=110.0)
        cf = await ex._close_position(close)
        assert cf is not None
        assert ("vol_momentum", "BTCUSDT") not in ex._positions
        assert ("adaptive_momentum", "BTCUSDT") in ex._positions


class TestCloseNonexistentPosition:
    async def test_close_nonexistent_position(self):
        """CLOSE signal with no open position → returns None."""
        executor = PaperExecutor(storage=None)
        fill = await executor.execute(_close_signal("BTCUSDT", 100.0))

        assert fill is None


class TestSlippageDirection:
    async def test_slippage_direction(self):
        """LONG fill price > entry (slippage up), SHORT fill price < entry (slippage down)."""
        slippage = 0.001  # 0.1%
        executor = PaperExecutor(storage=None, slippage_pct=slippage, commission_pct=0.0)

        # LONG: slippage costs you more (buy higher)
        long_fill = await executor.execute(
            _signal("BTCUSDT", SignalAction.LONG, entry=1000.0, sl=990.0))
        assert long_fill.price > 1000.0
        assert long_fill.price == pytest.approx(1000.0 * (1 + slippage))

        # Need different symbol for SHORT (can't open two on same)
        short_fill = await executor.execute(
            _signal("ETHUSDT", SignalAction.SHORT, entry=1000.0, sl=1010.0))
        assert short_fill.price < 1000.0
        assert short_fill.price == pytest.approx(1000.0 * (1 - slippage))
