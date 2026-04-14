"""Mid-candle momentum entry for the aggressive retail sub-book.

STATUS: NOT ALPHA-READY. See task "candle_burst_hunter OBITUARY".
Short version: the strategy's core premise — "detect a fast-moving
candle mid-bar and enter in its direction" — REQUIRES tick-level
data. On bar-level (M1/M5/1h) the "burst" is measured AFTER the bar
closes, by which point it's too late to enter at the start of the
burst. The Tier 5 infra validation (G.2h.3) with an experimental
multi-timeframe EMA-trend filter still wiped at -100% / 3451 trades
over 2 years.

KILL for this phase. Revisit when:
- Tick data + tick-level execution infrastructure lands
- OR the strategy is redesigned as "enter on the CLOSE of a burst bar
  confirmed by a pullback within 1-2 bars" (fundamentally a different
  signal from mid-bar velocity)

Retained in src/strategies/aggressive/ as a reference for future work.
"""

from __future__ import annotations

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction


class CandleBurstHunterStrategy(BaseStrategy):
    def __init__(
        self,
        name: str = "candle_burst_hunter",
        markets: list[str] | None = None,
        timeframe: str = "5m",
        risk_profile: RiskProfile = RiskProfile.AGGRESSIVE,
        max_risk_per_trade: float = 0.05,
        *,
        leverage_range: tuple[float, float] = (500.0, 1000.0),
        burst_atr_mult: float = 1.5,
        atr_period: int = 20,
        hard_sl_pct: float = 0.003,       # 0.3% entry
        trail_activation_pips: float = 5.0,
        trail_distance_pips: float = 2.0,
        pip_size: float = 0.10,
        max_hold_bars: int = 18,           # ~90 seconds on M5 ≈ ok for bar-level
        use_experimental_entry: bool = False,
        ema_trend_period: int = 21,
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
        )
        self.burst_atr_mult = burst_atr_mult
        self.atr_period = atr_period
        self.hard_sl_pct = hard_sl_pct
        self.trail_activation_pips = trail_activation_pips
        self.trail_distance_pips = trail_distance_pips
        self.pip_size = pip_size
        self.max_hold_bars = max_hold_bars
        self.use_experimental_entry = use_experimental_entry
        self.ema_trend_period = ema_trend_period

        self._atr_col = f"ATR_{atr_period}"
        self._entry_price: float = 0.0
        self._best_price: float = 0.0
        self._trail_active: bool = False
        self._bars_in_position: int = 0

    def _reset_state(self) -> None:
        self._entry_price = 0.0
        self._best_price = 0.0
        self._trail_active = False
        self._bars_in_position = 0

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = float(features["close"])
        open_ = float(features["open"])
        high = float(features["high"])
        low = float(features["low"])
        atr = features.get(self._atr_col)

        if atr is None or pd.isna(atr) or atr <= 0:
            return None

        # ── Position management ──
        if self._position in ("LONG", "SHORT"):
            self._bars_in_position += 1

            if self._position == "LONG":
                self._best_price = max(self._best_price, high)
                gain = self._best_price - self._entry_price
                if gain >= self.trail_activation_pips * self.pip_size:
                    self._trail_active = True
                trail_stop = self._best_price - self.trail_distance_pips * self.pip_size
                hard_sl = self._entry_price * (1.0 - self.hard_sl_pct)

                if low <= hard_sl:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=hard_sl,
                        metadata={"exit_reason": "hard_sl"},
                    )
                if self._trail_active and low <= trail_stop:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=trail_stop,
                        metadata={"exit_reason": "trail"},
                    )
                if self._bars_in_position >= self.max_hold_bars:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.6,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=close,
                        metadata={"exit_reason": "timeout"},
                    )
            else:  # SHORT
                self._best_price = min(self._best_price, low) if self._best_price > 0 else low
                gain = self._entry_price - self._best_price
                if gain >= self.trail_activation_pips * self.pip_size:
                    self._trail_active = True
                trail_stop = self._best_price + self.trail_distance_pips * self.pip_size
                hard_sl = self._entry_price * (1.0 + self.hard_sl_pct)

                if high >= hard_sl:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=hard_sl,
                        metadata={"exit_reason": "hard_sl"},
                    )
                if self._trail_active and high >= trail_stop:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=trail_stop,
                        metadata={"exit_reason": "trail"},
                    )
                if self._bars_in_position >= self.max_hold_bars:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.6,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=close,
                        metadata={"exit_reason": "timeout"},
                    )
            return None

        # ── Entry: bar traveled > N × ATR (burst detected) ──
        bar_travel = abs(close - open_)
        if bar_travel < self.burst_atr_mult * atr:
            return None

        direction = 1 if close > open_ else -1

        # Experimental entry filter: only fire when burst direction agrees
        # with the EMA trend (multi-timeframe confirmation)
        if self.use_experimental_entry:
            ema = features.get(f"EMA_{self.ema_trend_period}")
            if ema is None or pd.isna(ema):
                return None
            ema_val = float(ema)
            if direction > 0 and close < ema_val:
                return None
            if direction < 0 and close > ema_val:
                return None

        action = SignalAction.LONG if direction > 0 else SignalAction.SHORT
        self._entry_price = close
        self._best_price = close
        self._trail_active = False
        self._bars_in_position = 0

        sl_price = close * (1.0 - self.hard_sl_pct) if direction > 0 else close * (1.0 + self.hard_sl_pct)

        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.95,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            stop_loss=sl_price,
            take_profit=None,
            risk_pct=self.max_risk_per_trade,
            metadata={"bar_travel": bar_travel, "atr": float(atr)},
        )
