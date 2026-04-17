"""Base strategy interface — the contract every strategy implements.

The key design principle: BaseStrategy.on_features() is called identically
in both backtest and live trading. The strategy never knows which mode it's in.
Only the data source (historical vs WebSocket) and executor (simulated vs real) differ.

Usage:
    class MyStrategy(BaseStrategy):
        def on_features(self, symbol, timeframe, features) -> Signal | None:
            if features["EMA_9"] > features["EMA_21"]:
                return Signal(action=SignalAction.LONG, ...)
            return None
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import pandas as pd

from src.utils.config import get_config
from src.utils.logger import get_logger
from src.utils.types import RiskProfile, Signal, Tier

log = get_logger("strategy")


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Subclasses must implement on_features(). The base class handles:
    - Previous features rotation (for crossover detection)
    - Position state tracking (prevents duplicate signals)
    - Config loading from strategies.toml

    Fee-system integration:
    - `fee_style` is a class attribute declaring the strategy's typical
      hold-time/behavior bucket. The FeeManager uses this to pick the
      right fee profile and cost-projection model. Valid values:
      "scalping"  — <15 min holds, commission-dominant
      "intraday"  — same-day, no swap (default)
      "swing"     — multi-day, swap-aware
      "position"  — multi-week+, swap-dominant
      "arbitrage" — paired/spread, fraction-of-pip precision
    Subclasses override by re-assigning the class attribute:
        class MyStrategy(BaseStrategy):
            fee_style = "swing"
    """

    # Default style; subclasses override. See docs/FEE_SYSTEM.md.
    fee_style: str = "intraday"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str,
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        leverage_range: tuple[float, float] = (1.0, 1.0),
        tier: Tier = Tier.UNCLASSIFIED,
    ):
        self.name = name
        self.markets = [m.upper() for m in markets]
        self.timeframe = timeframe
        self.risk_profile = risk_profile
        self.max_risk_per_trade = max_risk_per_trade
        if leverage_range[0] < 1.0 or leverage_range[1] < leverage_range[0]:
            raise ValueError(
                f"invalid leverage_range {leverage_range}: min must be >= 1.0 "
                f"and max must be >= min"
            )
        self.leverage_range = (float(leverage_range[0]), float(leverage_range[1]))
        self.tier = tier

        # State — auto-managed by process()
        self._prev_features: pd.Series | None = None
        self._position: str = "FLAT"  # FLAT, LONG, SHORT
        self._signal_count = 0

    @abstractmethod
    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        """Generate a trading signal from the latest indicator values.

        Args:
            symbol: e.g. "BTCUSDT"
            timeframe: e.g. "1m"
            features: pd.Series with OHLCV + all indicator columns
                      (EMA_9, EMA_21, RSI_7, etc.)

        Returns:
            Signal if the strategy wants to trade, None otherwise.

        Notes:
            - Access previous features via self._prev_features (None on first call)
            - Check self._position before emitting signals to avoid duplicates
            - The same method is called in backtest and live — do not add mode checks
        """
        ...

    def process(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        """Called by the StrategyRouter. Wraps on_features with state management.

        Do NOT override this method — override on_features() instead.
        """
        signal = self.on_features(symbol, timeframe, features)

        # Rotate state
        self._prev_features = features.copy()

        if signal is not None:
            self._signal_count += 1
            # Update position tracking
            if signal.action.value in ("LONG", "SHORT"):
                self._position = signal.action.value
            elif signal.action.value == "CLOSE":
                self._position = "FLAT"

            # Ensure signal has required fields
            if signal.timestamp == 0:
                signal.timestamp = int(time.time() * 1000)
            if not signal.strategy_name:
                signal.strategy_name = self.name

            log.info(
                "signal",
                strategy=self.name,
                symbol=symbol,
                action=signal.action.value,
                confidence=float(signal.confidence),
                entry=float(signal.entry_price) if signal.entry_price else None,
                sl=float(signal.stop_loss) if signal.stop_loss else None,
                tp=float(signal.take_profit) if signal.take_profit else None,
            )

        return signal

    @classmethod
    def from_config(cls, name: str) -> BaseStrategy:
        """Factory: create strategy instance from strategies.toml config.

        Subclasses should override this to extract their specific parameters.
        """
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=strat_cfg.get("markets", []),
            timeframe=strat_cfg.get("timeframe", "1m"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.01),
        )

    def matches(self, symbol: str, timeframe: str) -> bool:
        """Check if this strategy should receive data for the given symbol/timeframe."""
        return symbol.upper() in self.markets and timeframe == self.timeframe

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "position": self._position,
            "signals": self._signal_count,
            "markets": self.markets,
            "timeframe": self.timeframe,
        }
