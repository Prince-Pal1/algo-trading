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


# ── Multi-market per-symbol instantiation (2026-04-17 bug fix) ──────────

class TestMultiMarketIsolation:
    """Regression tests for the cross-symbol state sharing bug.

    Pre-fix: a config like `markets = [A, B, C]` produced ONE strategy
    instance registered to all three markets. Internal per-instance state
    (`_closes` deque, `_position`, counters) leaked across symbols —
    ADAUSDT's 168-bar momentum lookback would resolve to whatever symbol
    happened to write 168 appends ago. See
    docs/investigations/2026-04-17_vol_momentum_stale_buffer.md.

    Post-fix: router.from_config detects `len(markets) > 1` and instantiates
    one strategy object per market. Each has independent state.
    """

    def test_single_market_unchanged(self, monkeypatch):
        """len(markets)==1 still produces exactly one strategy instance."""
        from src.strategies.router import STRATEGY_REGISTRY

        # Minimal fake config + fake strategy to avoid touching real TOML
        class _FakeCfg:
            strategies = {
                "singlemkt": {"enabled": True, "markets": ["BTCUSDT"], "timeframe": "1h"},
            }

        class _FakeStrat:
            @classmethod
            def from_config(cls, name: str):
                return FakeStrategy(name=name, markets=["BTCUSDT"], timeframe="1h")

        import src.strategies.router as router_mod
        monkeypatch.setitem(STRATEGY_REGISTRY, "singlemkt", _FakeStrat)
        monkeypatch.setattr(router_mod, "get_config", lambda: _FakeCfg, raising=False)

        # Patch the inline import site
        import src.utils.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "get_config", lambda: _FakeCfg)

        router = StrategyRouter.from_config()
        assert len(router._strategies) == 1
        assert router._strategies[0].markets == ["BTCUSDT"]

    def test_multi_market_produces_one_instance_per_market(self, monkeypatch):
        """len(markets)==3 → exactly 3 independent strategy instances."""
        from src.strategies.router import STRATEGY_REGISTRY

        class _FakeCfg:
            strategies = {
                "multi": {
                    "enabled": True,
                    "markets": ["DOGEUSDT", "ADAUSDT", "DOTUSDT"],
                    "timeframe": "1h",
                },
            }

        created_instances = []

        class _FakeStrat:
            @classmethod
            def from_config(cls, name: str):
                inst = FakeStrategy(name=name, markets=["DOGEUSDT", "ADAUSDT", "DOTUSDT"],
                                    timeframe="1h")
                created_instances.append(inst)
                return inst

        monkeypatch.setitem(STRATEGY_REGISTRY, "multi", _FakeStrat)
        import src.utils.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "get_config", lambda: _FakeCfg)

        router = StrategyRouter.from_config()

        # One instance per market
        assert len(router._strategies) == 3
        assert len(created_instances) == 3
        # Each instance scoped to exactly one market (unique per instance)
        market_sets = [tuple(s.markets) for s in router._strategies]
        assert sorted(market_sets) == [("ADAUSDT",), ("DOGEUSDT",), ("DOTUSDT",)]
        # All keep the same NAME so downstream audit consumers see one logical
        # strategy — the per-symbol split is an engine-internal detail.
        assert all(s.name == "multi" for s in router._strategies)
        # Routes: three (symbol, tf) keys, each pointing to a single strategy
        assert len(router._routes) == 3
        for market in ("DOGEUSDT", "ADAUSDT", "DOTUSDT"):
            route_strategies = router._routes[(market, "1h")]
            assert len(route_strategies) == 1

    def test_instances_have_independent_state(self, monkeypatch):
        """A mutation on one multi-market instance does NOT affect siblings."""
        from collections import deque

        class _FakeCfg:
            strategies = {
                "indep": {
                    "enabled": True,
                    "markets": ["A", "B"],
                    "timeframe": "1h",
                },
            }

        class _StatefulStrat:
            """Mimics a real strategy with a mutable buffer."""
            def __init__(self):
                self.name = "indep"
                self.markets = ["A", "B"]
                self.timeframe = "1h"
                self._closes: deque = deque(maxlen=10)

            @classmethod
            def from_config(cls, name: str):
                return cls()

            def process(self, symbol, timeframe, features):
                self._closes.append(features["close"])
                return None

            @property
            def stats(self):
                return {"name": self.name, "buf_len": len(self._closes)}

        from src.strategies.router import STRATEGY_REGISTRY
        monkeypatch.setitem(STRATEGY_REGISTRY, "indep", _StatefulStrat)
        import src.utils.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "get_config", lambda: _FakeCfg)

        router = StrategyRouter.from_config()
        assert len(router._strategies) == 2

        # Find each instance by its scoped market
        a_inst = [s for s in router._strategies if s.markets == ["A"]][0]
        b_inst = [s for s in router._strategies if s.markets == ["B"]][0]
        assert a_inst is not b_inst  # distinct Python objects

        # Mutate A's buffer
        a_inst._closes.append(1.0)
        a_inst._closes.append(2.0)
        a_inst._closes.append(3.0)

        # B's buffer must be unaffected
        assert len(a_inst._closes) == 3
        assert len(b_inst._closes) == 0

    def test_disabled_strategy_skipped(self, monkeypatch):
        """enabled=false → strategy is not registered."""
        class _FakeCfg:
            strategies = {
                "offstrat": {
                    "enabled": False,
                    "markets": ["BTCUSDT", "ETHUSDT"],
                    "timeframe": "1h",
                },
            }

        class _FakeStrat:
            @classmethod
            def from_config(cls, name: str):
                raise RuntimeError("Should not be called for disabled strategy")

        from src.strategies.router import STRATEGY_REGISTRY
        monkeypatch.setitem(STRATEGY_REGISTRY, "offstrat", _FakeStrat)
        import src.utils.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "get_config", lambda: _FakeCfg)

        router = StrategyRouter.from_config()
        assert len(router._strategies) == 0
