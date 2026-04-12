"""Tests for StrategyRouter — routing, exception isolation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.strategies.router import StrategyRouter
from src.utils.types import Signal, SignalAction


# ── Helpers ──────────────────────────────────────────────────────────────────

def _features(close: float = 100.0) -> pd.Series:
    return pd.Series({
        "open": 99.0, "high": 101.0, "low": 98.0,
        "close": close, "volume": 1000.0, "timestamp": 0,
        "EMA_9": 99.5, "RSI_14": 50.0,
    })


class FakeStrategy:
    """Minimal strategy that returns a configurable signal."""
    def __init__(self, name: str, markets: list[str], timeframe: str,
                 signal: Signal | None = None, raises: Exception | None = None):
        self.name = name
        self.markets = markets
        self.timeframe = timeframe
        self._signal = signal
        self._raises = raises
        self.process_count = 0

    def process(self, symbol: str, timeframe: str, features: pd.Series) -> Signal | None:
        self.process_count += 1
        if self._raises:
            raise self._raises
        return self._signal

    @property
    def stats(self) -> dict:
        return {"name": self.name, "calls": self.process_count}


# ── Tests ────────────────────────────────────────────────────────────────────

class TestRouteBySymbolTimeframe:
    async def test_route_by_symbol_timeframe(self):
        """(ETH, 1h) features → only strategies registered for (ETH, 1h)."""
        router = StrategyRouter()

        eth_signal = Signal(symbol="ETHUSDT", action=SignalAction.LONG,
                            confidence=0.8, strategy_name="eth_strat", timeframe="1h")
        eth_strat = FakeStrategy("eth_strat", ["ETHUSDT"], "1h", signal=eth_signal)
        btc_strat = FakeStrategy("btc_strat", ["BTCUSDT"], "1h")

        router.register(eth_strat)
        router.register(btc_strat)

        signals = await router.on_features("ETHUSDT", "1h", _features())

        assert len(signals) == 1
        assert signals[0].strategy_name == "eth_strat"
        assert eth_strat.process_count == 1
        assert btc_strat.process_count == 0  # Not called for ETH

    async def test_no_route_returns_empty(self):
        """Features for unregistered (symbol, tf) → empty list, no crash."""
        router = StrategyRouter()
        signals = await router.on_features("SOLUSDT", "5m", _features())
        assert signals == []


class TestStrategyExceptionIsolated:
    async def test_strategy_exception_isolated(self):
        """One strategy raises → others still process, signals still collected."""
        router = StrategyRouter()

        good_signal = Signal(symbol="ETHUSDT", action=SignalAction.SHORT,
                             confidence=0.7, strategy_name="good_strat", timeframe="1h")

        bad_strat = FakeStrategy("bad_strat", ["ETHUSDT"], "1h",
                                 raises=ValueError("indicator exploded"))
        good_strat = FakeStrategy("good_strat", ["ETHUSDT"], "1h", signal=good_signal)

        router.register(bad_strat)
        router.register(good_strat)

        signals = await router.on_features("ETHUSDT", "1h", _features())

        # bad_strat crashed but good_strat still produced a signal
        assert len(signals) == 1
        assert signals[0].strategy_name == "good_strat"
        assert bad_strat.process_count == 1
        assert good_strat.process_count == 1
