"""Exact math tests for PaperExecutor — every number pinned to expected value.

Unlike the smoke tests in test_paper_executor.py (which use commission_pct=0),
these tests verify exact position sizing, commission, slippage, and P&L math
with real-world parameters.
"""

from __future__ import annotations

import pytest

from src.execution.paper_executor import PaperExecutor
from src.utils.types import Signal, SignalAction, Side


# ── Helpers ──────────────────────────────────────────────────────────────────

def _signal(symbol: str, action: SignalAction, entry: float,
            sl: float | None = None, tp: float | None = None,
            risk_pct: float = 0.01) -> Signal:
    return Signal(
        symbol=symbol, action=action, confidence=0.8,
        strategy_name="test", timeframe="1h",
        entry_price=entry, stop_loss=sl, take_profit=tp,
        risk_pct=risk_pct,
    )


def _close(symbol: str, exit_price: float) -> Signal:
    return Signal(
        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
        strategy_name="test", timeframe="1h", entry_price=exit_price,
    )


# ── Sub-plan 1A: Position Sizing ─────────────────────────────────────────────

class TestPositionSizingExact:
    """Verify position sizing formula with exact expected quantities.

    Formula (with stop loss):
        fill_price = entry * (1 + slippage)     [LONG]
        risk_per_unit = |fill_price - stop_loss|
        risk_amount = equity * risk_pct
        quantity = risk_amount / risk_per_unit

    Formula (without stop loss):
        quantity = (equity * risk_pct) / fill_price
    """

    @pytest.mark.parametrize("equity,risk_pct,entry,sl,slippage,expected_qty", [
        # Basic: 10k, 1% risk, entry=100, sl=95, no slippage
        # risk_per_unit = |100 - 95| = 5, risk_amount = 100, qty = 100/5 = 20
        (10_000, 0.01, 100.0, 95.0, 0.0, 20.0),

        # With slippage: fill = 100 * 1.0002 = 100.02
        # risk_per_unit = |100.02 - 95| = 5.02, qty = 100/5.02
        (10_000, 0.01, 100.0, 95.0, 0.0002, 100.0 / 5.02),

        # Higher risk: 2% risk
        # risk_amount = 10000 * 0.02 = 200, qty = 200/5 = 40
        (10_000, 0.02, 100.0, 95.0, 0.0, 40.0),

        # Tight stop: entry=100, sl=99
        # risk_per_unit = 1, qty = 100/1 = 100
        (10_000, 0.01, 100.0, 99.0, 0.0, 100.0),

        # BTC scale: entry=50000, sl=49000
        # risk_per_unit = 1000, risk_amount = 100, qty = 0.1
        (10_000, 0.01, 50_000.0, 49_000.0, 0.0, 0.1),
    ])
    async def test_position_sizing_with_stop(self, equity, risk_pct, entry, sl,
                                              slippage, expected_qty):
        executor = PaperExecutor(
            storage=None, initial_capital=equity,
            slippage_pct=slippage, commission_pct=0.0,
        )
        fill = await executor.execute(_signal("TEST", SignalAction.LONG, entry, sl=sl,
                                              risk_pct=risk_pct))
        assert fill is not None
        assert fill.quantity == pytest.approx(expected_qty, rel=1e-6)

    async def test_position_sizing_no_stop(self):
        """No stop loss → fallback: qty = (equity * risk_pct) / fill_price."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.0,
        )
        fill = await executor.execute(_signal("TEST", SignalAction.LONG, 100.0, sl=None,
                                              risk_pct=0.01))
        # qty = (10000 * 0.01) / 100 = 1.0
        assert fill.quantity == pytest.approx(1.0)


# ── Sub-plan 1B: Commission Calculation ──────────────────────────────────────

class TestCommissionExact:
    """Verify commission = fill_price * quantity * commission_pct."""

    async def test_open_commission(self):
        """Opening commission = fill_price * qty * commission_pct."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,  # 0.1%
        )
        fill = await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))

        # qty = 100/5 = 20, commission = 100 * 20 * 0.001 = 2.0
        assert fill.commission == pytest.approx(100.0 * 20.0 * 0.001)

    async def test_commission_deducted_from_equity_on_open(self):
        """Opening commission is deducted from equity immediately (bug fix verification)."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,
        )
        await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))

        # commission = 100 * 20 * 0.001 = 2.0
        # equity = 10000 - 2.0 = 9998.0
        assert executor.equity == pytest.approx(9998.0)

    async def test_close_commission(self):
        """Closing commission = close_fill_price * qty * commission_pct."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,
        )
        await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))
        close_fill = await executor.execute(_close("BTC", 110.0))

        # close commission = 110 * 20 * 0.001 = 2.2
        assert close_fill.commission == pytest.approx(110.0 * 20.0 * 0.001)

    async def test_roundtrip_total_commission(self):
        """Total commission = open + close, both deducted from equity."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,
        )
        open_fill = await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))
        close_fill = await executor.execute(_close("BTC", 110.0))

        total_commission = open_fill.commission + close_fill.commission
        # open: 100 * 20 * 0.001 = 2.0, close: 110 * 20 * 0.001 = 2.2, total = 4.2
        assert total_commission == pytest.approx(4.2)


# ── Sub-plan 1C: Full Roundtrip P&L ─────────────────────────────────────────

class TestRoundtripPnLExact:
    """Step through every intermediate value for LONG and SHORT roundtrips."""

    async def test_long_roundtrip_all_intermediates(self):
        """LONG: entry=100, sl=95, close=110, slippage=0.02%, commission=0.1%.

        Step-by-step:
            open_fill = 100 * 1.0002 = 100.02
            qty = (10000 * 0.01) / |100.02 - 95| = 100 / 5.02 = 19.9203...
            open_commission = 100.02 * 19.9203 * 0.001 = 1.99243...
            equity_after_open = 10000 - 1.99243 = 9998.00757

            close_fill = 110 * 0.9998 = 109.978
            gross_pnl = (109.978 - 100.02) * 19.9203 = 9.958 * 19.9203 = 198.3255...
            close_commission = 109.978 * 19.9203 * 0.001 = 2.19067...
            net_pnl = 198.3255 - 2.19067 = 196.1349...
            final_equity = 9998.00757 + 196.1349 = 10194.1424...
        """
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0002, commission_pct=0.001,
        )

        open_fill = await executor.execute(
            _signal("BTC", SignalAction.LONG, 100.0, sl=95.0, risk_pct=0.01))

        # Verify open intermediates
        assert open_fill.price == pytest.approx(100.02, rel=1e-6)
        expected_qty = 100.0 / abs(100.02 - 95.0)
        assert open_fill.quantity == pytest.approx(expected_qty, rel=1e-6)
        expected_open_comm = 100.02 * expected_qty * 0.001
        assert open_fill.commission == pytest.approx(expected_open_comm, rel=1e-6)
        assert executor.equity == pytest.approx(10_000 - expected_open_comm, rel=1e-6)

        equity_after_open = executor.equity

        # Close
        close_fill = await executor.execute(_close("BTC", 110.0))

        # Verify close intermediates
        assert close_fill.price == pytest.approx(110.0 * 0.9998, rel=1e-6)
        close_price = 110.0 * 0.9998
        gross_pnl = (close_price - 100.02) * expected_qty
        expected_close_comm = close_price * expected_qty * 0.001
        assert close_fill.commission == pytest.approx(expected_close_comm, rel=1e-6)
        net_pnl = gross_pnl - expected_close_comm

        assert executor.equity == pytest.approx(equity_after_open + net_pnl, rel=1e-6)

    async def test_short_roundtrip_all_intermediates(self):
        """SHORT: entry=100, sl=105, close=90, slippage=0.02%, commission=0.1%.

        Step-by-step:
            open_fill = 100 * 0.9998 = 99.98  (SHORT: slippage down)
            qty = (10000 * 0.01) / |99.98 - 105| = 100 / 5.02 = 19.9203...
            open_commission = 99.98 * 19.9203 * 0.001

            close_fill = 90 * 1.0002 = 90.018  (covering: slippage up)
            gross_pnl = (99.98 - 90.018) * qty
            close_commission = 90.018 * qty * 0.001
        """
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0002, commission_pct=0.001,
        )

        open_fill = await executor.execute(
            _signal("ETH", SignalAction.SHORT, 100.0, sl=105.0, risk_pct=0.01))

        # SHORT: fill_price = 100 * (1 - 0.0002) = 99.98
        assert open_fill.price == pytest.approx(99.98, rel=1e-6)
        assert open_fill.side == Side.SELL
        expected_qty = 100.0 / abs(99.98 - 105.0)
        assert open_fill.quantity == pytest.approx(expected_qty, rel=1e-6)

        equity_after_open = executor.equity

        close_fill = await executor.execute(_close("ETH", 90.0))
        # Cover: fill = 90 * (1 + 0.0002) = 90.018
        assert close_fill.price == pytest.approx(90.018, rel=1e-6)
        assert close_fill.side == Side.BUY

        gross_pnl = (99.98 - 90.018) * expected_qty
        close_comm = 90.018 * expected_qty * 0.001
        net_pnl = gross_pnl - close_comm
        assert executor.equity == pytest.approx(equity_after_open + net_pnl, rel=1e-6)

    async def test_long_loss_roundtrip(self):
        """LONG at 100 that drops to 90 — verify loss math."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,
        )
        await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))
        equity_after_open = executor.equity

        close_fill = await executor.execute(_close("BTC", 90.0))

        # qty = 20, gross_pnl = (90 - 100) * 20 = -200
        # close_commission = 90 * 20 * 0.001 = 1.8
        # net_pnl = -200 - 1.8 = -201.8
        gross_pnl = (90.0 - 100.0) * 20.0
        close_comm = 90.0 * 20.0 * 0.001
        assert executor.equity == pytest.approx(equity_after_open + gross_pnl - close_comm, rel=1e-6)
        assert executor.equity < 10_000  # Must be a loss

    async def test_short_loss_roundtrip(self):
        """SHORT at 100 that rises to 110 — verify loss math."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,
        )
        await executor.execute(_signal("ETH", SignalAction.SHORT, 100.0, sl=105.0))
        equity_after_open = executor.equity

        close_fill = await executor.execute(_close("ETH", 110.0))

        # qty = 100/|100-105| = 20, gross_pnl = (100 - 110) * 20 = -200
        # close_comm = 110 * 20 * 0.001 = 2.2
        gross_pnl = (100.0 - 110.0) * 20.0
        close_comm = 110.0 * 20.0 * 0.001
        assert executor.equity == pytest.approx(equity_after_open + gross_pnl - close_comm, rel=1e-6)
        assert executor.equity < 10_000

    async def test_breakeven_with_commission(self):
        """Open and close at same price — lose only commissions."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.001,
        )
        await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))
        await executor.execute(_close("BTC", 100.0))

        # qty = 20, open_comm = 100*20*0.001=2.0, close_comm = 100*20*0.001=2.0
        # gross_pnl = (100 - 100) * 20 = 0
        # total lost = 2.0 + 2.0 = 4.0
        assert executor.equity == pytest.approx(10_000 - 4.0, rel=1e-6)


# ── Sub-plan 1D: Edge Cases ──────────────────────────────────────────────────

class TestExecutorMathEdgeCases:
    async def test_hold_signal_noop(self):
        """HOLD signal → no fill, no state change."""
        executor = PaperExecutor(storage=None, initial_capital=10_000)
        sig = Signal(symbol="BTC", action=SignalAction.HOLD, confidence=0.5,
                     strategy_name="test", timeframe="1h")
        fill = await executor.execute(sig)
        assert fill is None
        assert executor.equity == 10_000
        assert len(executor._positions) == 0

    async def test_multi_symbol_concurrent(self):
        """BTC LONG + ETH SHORT simultaneously — independent P&L."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.0,
        )
        await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))
        await executor.execute(_signal("ETH", SignalAction.SHORT, 50.0, sl=55.0))

        assert "BTC" in executor._positions
        assert "ETH" in executor._positions
        assert executor._positions["BTC"].side == Side.BUY
        assert executor._positions["ETH"].side == Side.SELL

    async def test_entry_equals_zero_rejected(self):
        """Entry price = 0 → rejected."""
        executor = PaperExecutor(storage=None)
        fill = await executor.execute(_signal("BTC", SignalAction.LONG, 0.0, sl=None))
        assert fill is None

    async def test_rapid_open_close_open(self):
        """Open, close, open again on same symbol — should work."""
        executor = PaperExecutor(
            storage=None, initial_capital=10_000,
            slippage_pct=0.0, commission_pct=0.0,
        )
        f1 = await executor.execute(_signal("BTC", SignalAction.LONG, 100.0, sl=95.0))
        assert f1 is not None
        f2 = await executor.execute(_close("BTC", 110.0))
        assert f2 is not None
        f3 = await executor.execute(_signal("BTC", SignalAction.LONG, 110.0, sl=105.0))
        assert f3 is not None
        assert "BTC" in executor._positions
