"""Minimal SMA(5) vs SMA(20) crossover for smoke-testing the pipeline.

NOT for production trading. NOT for research. This strategy exists ONLY
as the canonical minimal `BaseStrategy` subclass used by
`scripts/smoke_test_dashboard.py` to exercise:

    run_deep_backtest() → record_deep_backtest_result() → Strategies page
    → facets / keep-best / hero picker / auto-description

end-to-end. If you're looking for a real gold strategy, see
`donchian_gold`, `vol_momentum_gold`, `swift_alma`, or `swift_alma_v2`.

Design:
- SMA(5) vs SMA(20) crossover on close.
- Bullish cross → LONG. Bearish cross → SHORT. Opposite cross closes + flips.
- No filters, no regime gates, no vol targeting, no session windows.
- Guaranteed to emit ~10-30 signals per month on XAUUSD 1h.
- Rolling buffer maintained inside the strategy — no dependency on
  specific indicators being pre-computed by feature_engine.
- Declares `XAUUSD` in markets + `leverage_range=(1.0, 100.0)` so it
  passes the Run Deep Backtest page 7 picker filter.

Config: reads from `[smoke_demo]` section in strategies.toml if present,
otherwise uses the defaults in `__init__`.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


class SmokeDemoStrategy(BaseStrategy):
    """Minimal SMA crossover demo — pipeline smoke test only."""

    def __init__(
        self,
        name: str = "smoke_demo",
        markets: list[str] | None = None,
        timeframe: str = "1h",
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        sma_fast_period: int = 5,
        sma_slow_period: int = 20,
        sl_pct: float = 0.01,
        tp_pct: float = 0.02,
    ):
        super().__init__(
            name,
            markets if markets is not None else ["XAUUSD"],
            timeframe,
            risk_profile,
            max_risk_per_trade,
            leverage_range=(1.0, 100.0),
            tier=Tier.UNCLASSIFIED,
        )
        self.sma_fast_period = sma_fast_period
        self.sma_slow_period = sma_slow_period
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct

        # Rolling close-price buffer. Size = slow SMA period + 1 so we
        # always have enough history to compute both MAs on every call.
        self._closes: deque[float] = deque(maxlen=sma_slow_period + 2)
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

    def _sma(self, n: int) -> float | None:
        if len(self._closes) < n:
            return None
        tail = list(self._closes)[-n:]
        return sum(tail) / n

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = float(features["close"])
        self._closes.append(close)

        fast = self._sma(self.sma_fast_period)
        slow = self._sma(self.sma_slow_period)
        if fast is None or slow is None:
            return None

        # Crossover detection needs the previous bar's MAs
        if self._prev_fast is None or self._prev_slow is None:
            self._prev_fast, self._prev_slow = fast, slow
            return None

        bullish_cross = self._prev_fast <= self._prev_slow and fast > slow
        bearish_cross = self._prev_fast >= self._prev_slow and fast < slow
        self._prev_fast, self._prev_slow = fast, slow

        # ── EXIT on opposite cross (flip) ──
        if self._position == "LONG" and bearish_cross:
            return Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=0.8,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                metadata={"exit_reason": "bearish_cross"},
            )
        if self._position == "SHORT" and bullish_cross:
            return Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=0.8,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                metadata={"exit_reason": "bullish_cross"},
            )

        # ── ENTRY on crossover ──
        if bullish_cross and self._position != "LONG":
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=0.7,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(close * (1 - self.sl_pct), 8),
                take_profit=round(close * (1 + self.tp_pct), 8),
                risk_pct=self.max_risk_per_trade,
                metadata={"sma_fast": round(fast, 4), "sma_slow": round(slow, 4)},
            )
        if bearish_cross and self._position != "SHORT":
            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=0.7,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(close * (1 + self.sl_pct), 8),
                take_profit=round(close * (1 - self.tp_pct), 8),
                risk_pct=self.max_risk_per_trade,
                metadata={"sma_fast": round(fast, 4), "sma_slow": round(slow, 4)},
            )

        return None

    @classmethod
    def from_config(cls, name: str = "smoke_demo") -> "SmokeDemoStrategy":
        try:
            cfg = get_config()
            strat_cfg = cfg.get_strategy(name)
        except Exception:
            strat_cfg = {}
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["XAUUSD"]),
            timeframe=strat_cfg.get("timeframe", "1h"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.01),
            sma_fast_period=strat_cfg.get("sma_fast_period", 5),
            sma_slow_period=strat_cfg.get("sma_slow_period", 20),
            sl_pct=strat_cfg.get("sl_pct", 0.01),
            tp_pct=strat_cfg.get("tp_pct", 0.02),
        )
