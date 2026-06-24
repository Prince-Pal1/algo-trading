"""Sub-phase 0.8 — smoke tests for M3S ↔ main.py wiring.

These tests verify that:
1. TradingEngine imports cleanly with M3S modules (no circular imports).
2. `_maybe_init_m3s` is a no-op when `cfg.m3s.enabled=False` (default).
3. `_maybe_init_m3s` constructs a live M3S when enabled.
4. PaperExecutor's `on_trade_close_hook` is wired when M3S is active.
5. `on_signal` wiring is defensive: M3S exception does not break the engine.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.execution.paper_executor import PaperExecutor
from src.main import TradingEngine


def _build_engine() -> TradingEngine:
    """Construct a TradingEngine without actually starting the feed."""
    return TradingEngine(
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        indicators=["rsi_14"],
    )


class TestEngineImports:
    def test_imports_cleanly(self):
        """Importing main.py must succeed with M3S modules in scope."""
        from src import main
        assert hasattr(main, "TradingEngine")
        assert hasattr(main, "M3S")
        assert hasattr(main, "M3SStore")

    def test_engine_constructs(self):
        engine = _build_engine()
        assert engine.m3s is None
        assert engine.m3s_store is None
        assert engine.m3s_scheduler is None


class TestMaybeInitM3S:
    def test_disabled_is_noop(self):
        engine = _build_engine()
        engine.paper_executor = PaperExecutor()
        # Simulate a config with m3s disabled (default)
        cfg = MagicMock()
        cfg.settings = {"m3s": {"enabled": False}}
        engine._maybe_init_m3s(cfg)
        assert engine.m3s is None
        assert engine.m3s_store is None

    def test_missing_m3s_section_is_noop(self):
        engine = _build_engine()
        engine.paper_executor = PaperExecutor()
        cfg = MagicMock()
        cfg.settings = {}  # No m3s section at all
        engine._maybe_init_m3s(cfg)
        assert engine.m3s is None

    @pytest.mark.asyncio
    async def test_enabled_constructs_live_m3s(self, tmp_path):
        engine = _build_engine()
        engine.paper_executor = PaperExecutor()
        cfg = MagicMock()
        cfg.settings = {
            "m3s": {
                "enabled": True,
                "mode": "STANDARD",
                "shadow_mode": True,
                "db_path": str(tmp_path / "m3s_smoke.sqlite"),
                "rebalance_cadence_hours": 24.0,
            }
        }
        engine._maybe_init_m3s(cfg)
        try:
            assert engine.m3s is not None
            assert engine.m3s_store is not None
            assert engine.m3s_scheduler is not None
            assert engine.m3s.shadow_mode is True
            assert engine.paper_executor.on_trade_close_hook is not None
        finally:
            if engine.m3s_scheduler is not None:
                engine.m3s_scheduler.stop()
            if engine._m3s_scheduler_task is not None:
                engine._m3s_scheduler_task.cancel()
                try:
                    await engine._m3s_scheduler_task
                except BaseException:
                    pass
            if engine.m3s_store is not None:
                engine.m3s_store.close()

    @pytest.mark.asyncio
    async def test_enabled_with_invalid_mode_falls_back_to_standard(self, tmp_path):
        engine = _build_engine()
        engine.paper_executor = PaperExecutor()
        cfg = MagicMock()
        cfg.settings = {
            "m3s": {
                "enabled": True,
                "mode": "NONSENSE",  # Invalid — should warn and fallback
                "shadow_mode": True,
                "db_path": str(tmp_path / "m3s_smoke_bad_mode.sqlite"),
                "rebalance_cadence_hours": 24.0,
            }
        }
        from src.m3s.modes import M3SMode
        engine._maybe_init_m3s(cfg)
        try:
            assert engine.m3s is not None
            assert engine.m3s.mode.name == M3SMode.STANDARD
        finally:
            if engine.m3s_scheduler is not None:
                engine.m3s_scheduler.stop()
            if engine._m3s_scheduler_task is not None:
                engine._m3s_scheduler_task.cancel()
                try:
                    await engine._m3s_scheduler_task
                except BaseException:
                    pass
            if engine.m3s_store is not None:
                engine.m3s_store.close()


class TestPaperExecutorHook:
    def test_hook_invoked_on_close(self):
        """Verify PaperExecutor.on_trade_close_hook fires on _close_position."""
        pe = PaperExecutor()
        calls = []
        pe.on_trade_close_hook = lambda strat, pnl, sym, ts: calls.append((strat, pnl, sym, ts))

        # Manually inject a position and close it via _close_position
        from src.utils.types import Position, Side, Signal, SignalAction
        pe._positions[("test_strat", "BTCUSDT")] = Position(
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=0.1,
            entry_price=50_000.0,
            current_price=51_000.0,
            unrealized_pnl=100.0,
            realized_pnl=0.0,
            strategy_name="test_strat",
            opened_at=1_700_000_000_000,
        )

        close_sig = Signal(
            symbol="BTCUSDT",
            action=SignalAction.CLOSE,
            confidence=1.0,
            strategy_name="test_strat",
            timeframe="1h",
            entry_price=51_000.0,
        )

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            fill = loop.run_until_complete(pe._close_position(close_sig))
        finally:
            loop.close()

        assert fill is not None
        assert len(calls) == 1
        strategy, pnl, symbol, ts = calls[0]
        assert strategy == "test_strat"
        assert symbol == "BTCUSDT"
        assert pnl > 0.0  # We priced in $1000 of favorable movement minus commission

    def test_hook_exception_does_not_break_close(self):
        """A broken hook must not prevent a close from completing."""
        pe = PaperExecutor()
        pe.on_trade_close_hook = lambda *args: 1 / 0  # ZeroDivisionError
        from src.utils.types import Position, Side, Signal, SignalAction
        pe._positions[("test_strat", "BTCUSDT")] = Position(
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=0.1,
            entry_price=50_000.0,
            current_price=51_000.0,
            unrealized_pnl=100.0,
            realized_pnl=0.0,
            strategy_name="test_strat",
            opened_at=1_700_000_000_000,
        )
        close_sig = Signal(
            symbol="BTCUSDT",
            action=SignalAction.CLOSE,
            confidence=1.0,
            strategy_name="test_strat",
            timeframe="1h",
            entry_price=51_000.0,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            fill = loop.run_until_complete(pe._close_position(close_sig))
        finally:
            loop.close()
        # Close still succeeded despite the broken hook
        assert fill is not None
