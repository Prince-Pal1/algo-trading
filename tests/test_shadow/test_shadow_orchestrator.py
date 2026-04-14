from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.feeds.parquet_replay_feed import ParquetReplayFeed
from src.shadow_orchestrator import ShadowOrchestrator, StrategyRoute
from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction


class _LongThenCloseStrategy(BaseStrategy):
    def __init__(self, entry_bar: int = 15, exit_bar: int = 35):
        super().__init__(
            name="test_long_close",
            markets=["XAUUSD"],
            timeframe="1h",
            leverage_range=(1.0, 10.0),
        )
        self._entry_bar = entry_bar
        self._exit_bar = exit_bar
        self._bar_counter = 0

    def on_features(self, symbol, timeframe, features):
        self._bar_counter += 1
        price = float(features["close"])
        if self._bar_counter == self._entry_bar:
            return Signal(
                symbol=symbol, action=SignalAction.LONG, confidence=0.9,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=price, stop_loss=price * 0.99,
                take_profit=price * 1.02, risk_pct=0.01, leverage=5.0,
            )
        if self._bar_counter == self._exit_bar:
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
            )
        return None


def _write_synthetic_parquet(path: Path, n: int = 60) -> Path:
    base = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    ts = [int((base + timedelta(hours=i)).timestamp() * 1000) for i in range(n)]
    prices = 2400.0 + np.arange(n) * 1.0  # steadily rising
    df = pd.DataFrame({
        "timestamp": ts,
        "open": prices - 0.5,
        "high": prices + 0.3,
        "low": prices - 0.8,
        "close": prices + 0.5,
        "volume": np.full(n, 100.0),
    })
    df.to_parquet(path)
    return path


class TestShadowOrchestrator:
    @pytest.mark.asyncio
    async def test_runs_end_to_end_on_synthetic_data(self, tmp_path):
        parquet = _write_synthetic_parquet(tmp_path / "x.parquet", n=60)
        feed = ParquetReplayFeed(parquet, symbol="XAUUSD", timeframe="1h")
        strat = _LongThenCloseStrategy(entry_bar=15, exit_bar=35)

        orchestrator = ShadowOrchestrator(
            feed=feed,
            strategy_routes=[StrategyRoute(strat, "institutional", 5.0)],
            initial_institutional_cash=10_000.0,
            initial_aggressive_cash=0.0,
            indicators=["atr_14", "atr_20", "adx_14"],
            state_dump_path=tmp_path / "dump",
            state_dump_interval=20,
        )
        state = await orchestrator.run(parquet)

        assert state.bar_index == 60
        assert len(state.equity_total) == 60
        assert state.equity_total[-1] > 0
        # The strategy opened + closed one trade
        assert len(state.trades) >= 1

    @pytest.mark.asyncio
    async def test_state_dump_parquet_written(self, tmp_path):
        parquet = _write_synthetic_parquet(tmp_path / "x.parquet", n=60)
        feed = ParquetReplayFeed(parquet, symbol="XAUUSD", timeframe="1h")
        strat = _LongThenCloseStrategy(entry_bar=15, exit_bar=35)

        orchestrator = ShadowOrchestrator(
            feed=feed,
            strategy_routes=[StrategyRoute(strat, "institutional", 5.0)],
            initial_institutional_cash=10_000.0,
            initial_aggressive_cash=0.0,
            indicators=["atr_14"],
            state_dump_path=tmp_path / "dump",
            state_dump_interval=20,
        )
        await orchestrator.run(parquet)

        equity_parquet = tmp_path / "dump" / "equity.parquet"
        assert equity_parquet.exists()
        loaded = pd.read_parquet(equity_parquet)
        assert list(loaded.columns) == [
            "bar_index", "equity_institutional",
            "equity_aggressive", "equity_total", "margin_level",
        ]
        assert len(loaded) == 60

    @pytest.mark.asyncio
    async def test_no_trades_when_strategy_idle(self, tmp_path):
        parquet = _write_synthetic_parquet(tmp_path / "x.parquet", n=30)
        feed = ParquetReplayFeed(parquet, symbol="XAUUSD", timeframe="1h")

        class _Idle(BaseStrategy):
            def __init__(self):
                super().__init__(
                    name="idle", markets=["XAUUSD"], timeframe="1h",
                    leverage_range=(1.0, 10.0),
                )

            def on_features(self, symbol, timeframe, features):
                return None

        orchestrator = ShadowOrchestrator(
            feed=feed,
            strategy_routes=[StrategyRoute(_Idle(), "institutional", 5.0)],
            initial_institutional_cash=10_000.0,
            initial_aggressive_cash=0.0,
            indicators=["atr_14"],
        )
        state = await orchestrator.run(parquet)

        assert len(state.trades) == 0
        # Equity held flat (no trades = no P&L changes)
        assert abs(state.equity_total[-1] - 10_000.0) < 0.01
