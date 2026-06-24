"""Phase 4C — PaperExecutor edge case tests.

Tests the executor with unusual sequences and boundary conditions.
"""

from __future__ import annotations

import pytest

from src.execution.paper_executor import PaperExecutor
from src.utils.types import Signal, SignalAction, Side


def _signal(symbol="BTCUSDT", action=SignalAction.LONG, price=100.0,
            stop_loss=None, risk_pct=0.01, strategy="test"):
    return Signal(
        symbol=symbol,
        action=action,
        confidence=0.8,
        strategy_name=strategy,
        timeframe="1h",
        entry_price=price,
        stop_loss=stop_loss,
        risk_pct=risk_pct,
    )


class TestPaperExecutorEdgeCases:

    async def test_multi_symbol_concurrent(self):
        """BTC LONG + ETH SHORT simultaneously → independent positions."""
        ex = PaperExecutor(slippage_pct=0, commission_pct=0)

        btc_fill = await ex.execute(_signal("BTCUSDT", SignalAction.LONG, 50000))
        eth_fill = await ex.execute(_signal("ETHUSDT", SignalAction.SHORT, 3000))

        assert btc_fill is not None
        assert eth_fill is not None
        assert btc_fill.side == Side.BUY
        assert eth_fill.side == Side.SELL

        positions = await ex.get_positions()
        assert len(positions) == 2
        assert ("test", "BTCUSDT") in positions
        assert ("test", "ETHUSDT") in positions
        assert positions[("test", "BTCUSDT")].side == Side.BUY
        assert positions[("test", "ETHUSDT")].side == Side.SELL

    async def test_rapid_open_close_open_close(self):
        """Rapid open-close-open-close on same symbol → correct final equity."""
        initial = 10_000.0
        ex = PaperExecutor(initial_capital=initial, slippage_pct=0, commission_pct=0)

        # Round 1: open LONG at 100, close at 110 → profit = 10 * qty
        await ex.execute(_signal("BTCUSDT", SignalAction.LONG, 100, stop_loss=95))
        # qty = (10000 * 0.01) / |100 - 95| = 20
        await ex.execute(_signal("BTCUSDT", SignalAction.CLOSE, 110))
        # PnL = (110 - 100) * 20 = 200

        equity_after_r1 = ex.equity
        assert equity_after_r1 == pytest.approx(10_200.0, abs=0.01)

        # Round 2: reopen at 110, close at 100 → loss
        await ex.execute(_signal("BTCUSDT", SignalAction.LONG, 110, stop_loss=105))
        # qty = (10200 * 0.01) / |110 - 105| = 20.4
        await ex.execute(_signal("BTCUSDT", SignalAction.CLOSE, 100))
        # PnL = (100 - 110) * 20.4 = -204

        equity_after_r2 = ex.equity
        assert equity_after_r2 == pytest.approx(10_200.0 - 204.0, abs=0.01)

    async def test_close_with_zero_exit_price(self):
        """Close with exit_price=0 → uses current_price from position."""
        ex = PaperExecutor(slippage_pct=0, commission_pct=0)

        await ex.execute(_signal("BTCUSDT", SignalAction.LONG, 100, stop_loss=95))

        # Update price
        ex.update_prices("BTCUSDT", 110.0)

        # Close with entry_price=0 → should fall back to current_price
        close_signal = Signal(
            symbol="BTCUSDT",
            action=SignalAction.CLOSE,
            confidence=1.0,
            strategy_name="test",
            timeframe="1h",
            entry_price=0,
        )
        fill = await ex.execute(close_signal)
        # entry_price=0 is falsy, so _close_position uses pos.current_price (110)
        assert fill is not None
        assert fill.price == pytest.approx(110.0, abs=0.01)

    async def test_update_prices_nonexistent_noop(self):
        """update_prices on non-existent position → no-op, no crash."""
        ex = PaperExecutor()
        # Should not raise
        ex.update_prices("NONEXISTENT", 999.0)
        positions = await ex.get_positions()
        assert len(positions) == 0

    async def test_close_all_three_positions(self):
        """close_all with 3 positions → each P&L independent, all closed."""
        ex = PaperExecutor(slippage_pct=0, commission_pct=0)

        await ex.execute(_signal("BTCUSDT", SignalAction.LONG, 100, stop_loss=95))
        await ex.execute(_signal("ETHUSDT", SignalAction.LONG, 50, stop_loss=45))
        await ex.execute(_signal("SOLUSDT", SignalAction.SHORT, 200, stop_loss=210))

        # Update prices
        ex.update_prices("BTCUSDT", 110.0)
        ex.update_prices("ETHUSDT", 45.0)
        ex.update_prices("SOLUSDT", 190.0)

        fills = await ex.close_all()
        assert len(fills) == 3

        positions = await ex.get_positions()
        assert len(positions) == 0

        # Equity should reflect all three P&Ls
        # BTC: profit (110-100)*qty, ETH: loss (45-50)*qty, SOL: profit (200-190)*qty
        assert ex.equity != 10_000.0  # Should have changed
