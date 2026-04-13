"""Strategy Router — dispatches features to matching strategies and collects signals.

The router sits between the FeatureEngine and the Executor:
    FeatureEngine → on_features → StrategyRouter → strategies → signals → Executor

Usage:
    router = StrategyRouter.from_config(storage, executor)
    signals = await router.on_features("BTCUSDT", "1m", features)
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from src.data.storage import Storage
from src.strategies.base import BaseStrategy
from src.utils.logger import get_logger
from src.utils.types import Signal

log = get_logger("router")


class StrategyRouter:
    """Routes features to matching strategies and handles signal flow."""

    def __init__(self, storage: Storage | None = None):
        self._strategies: list[BaseStrategy] = []
        self._routes: dict[tuple[str, str], list[BaseStrategy]] = defaultdict(list)
        self._storage = storage
        self._signal_count = 0
        self._exception_count = 0

    def register(self, strategy: BaseStrategy) -> None:
        """Register a strategy. Auto-maps its (symbol, timeframe) pairs."""
        self._strategies.append(strategy)
        for market in strategy.markets:
            key = (market.upper(), strategy.timeframe)
            self._routes[key].append(strategy)
            log.info(
                "strategy_registered",
                strategy=strategy.name,
                symbol=market,
                timeframe=strategy.timeframe,
            )

    async def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> list[Signal]:
        """Route features to matching strategies, collect and log signals.

        Called by TradingEngine._on_features() — same signature.
        """
        key = (symbol.upper(), timeframe)
        strategies = self._routes.get(key, [])
        signals: list[Signal] = []

        for strategy in strategies:
            try:
                signal = strategy.process(symbol, timeframe, features)
            except Exception as e:
                self._exception_count += 1
                log.error("strategy_exception", strategy=strategy.name,
                          symbol=symbol, error=str(e), type=type(e).__name__)
                continue

            if signal is not None:
                signals.append(signal)
                self._signal_count += 1

                # Log signal to SQLite
                if self._storage:
                    await self._storage.trade_log.log_signal(signal)

        return signals

    @classmethod
    def from_config(cls, storage: Storage | None = None) -> StrategyRouter:
        """Factory: create router with all enabled strategies from strategies.toml.

        Uses the STRATEGY_REGISTRY to map config section names to strategy classes.
        """
        from src.utils.config import get_config

        router = cls(storage=storage)
        cfg = get_config()

        for name, strat_cfg in cfg.strategies.items():
            if not strat_cfg.get("enabled", False):
                log.info("strategy_disabled", name=name)
                continue

            strategy_cls = STRATEGY_REGISTRY.get(name)
            if strategy_cls is None:
                log.warning("strategy_not_found", name=name, available=list(STRATEGY_REGISTRY.keys()))
                continue

            strategy = strategy_cls.from_config(name)
            router.register(strategy)

        log.info(
            "router_ready",
            strategies=len(router._strategies),
            routes=len(router._routes),
        )
        return router

    @property
    def stats(self) -> dict:
        return {
            "total_signals": self._signal_count,
            "strategies": [s.stats for s in self._strategies],
            "routes": {f"{s}_{tf}": [st.name for st in strats]
                       for (s, tf), strats in self._routes.items()},
        }


# ── Strategy Registry ─────────────────────────────────────────────────────
# Maps config section names to strategy classes.
# Add new strategies here as they are implemented.

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str, cls: type[BaseStrategy]) -> None:
    """Register a strategy class for config-based instantiation."""
    STRATEGY_REGISTRY[name] = cls


# Import strategies to trigger registration
# (each strategy module calls register_strategy at import time)
def _load_strategies() -> None:
    try:
        from src.strategies.scalping.ema_crossover import EMAScalpStrategy
        register_strategy("ema_crossover", EMAScalpStrategy)
    except ImportError:
        pass

    try:
        from src.strategies.day_trading.bb_rsi_mr import BBRSIMeanRevStrategy
        register_strategy("bb_rsi_mr", BBRSIMeanRevStrategy)
    except ImportError:
        pass

    try:
        from src.strategies.trend_following.donchian_ensemble import DonchianEnsembleStrategy
        register_strategy("donchian_ensemble_adx", DonchianEnsembleStrategy)
    except ImportError:
        pass

    try:
        from src.strategies.momentum.vol_momentum import VolMomentumStrategy
        register_strategy("vol_momentum", VolMomentumStrategy)
    except ImportError:
        pass

    try:
        from src.strategies.carry.funding_mean_reversion import (
            FundingMeanReversionStrategy,
        )
        register_strategy("funding_mean_reversion", FundingMeanReversionStrategy)
    except ImportError:
        pass

    try:
        from src.strategies.carry.funding_carry import FundingCarryStrategy
        register_strategy("funding_carry", FundingCarryStrategy)
    except ImportError:
        pass

    try:
        from src.strategies.momentum.clenow_momentum import ClenowMomentumStrategy
        register_strategy("clenow_momentum", ClenowMomentumStrategy)
    except ImportError:
        pass


_load_strategies()
