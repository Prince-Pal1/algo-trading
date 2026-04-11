"""EMA Crossover + RSI Filter + Volume Confirmation — scalping strategy.

Entry logic:
    LONG:  EMA(fast) crosses above EMA(slow) + RSI < overbought + volume > avg * multiplier
    SHORT: EMA(fast) crosses below EMA(slow) + RSI > oversold   + volume > avg * multiplier

Exit logic:
    Stop loss at recent swing low/high. Take profit at 2R (configurable).
    CLOSE signal on opposite crossover while in position.

Config: reads from [ema_crossover] section in strategies.toml.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction


class EMAScalpStrategy(BaseStrategy):
    """EMA Crossover scalping strategy with RSI and volume filters."""

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str,
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        fast_period: int = 9,
        slow_period: int = 21,
        rsi_period: int = 7,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        volume_multiplier: float = 1.2,
        take_profit_ratio: float = 2.0,
        swing_lookback: int = 20,
        # Anti-whipsaw filters
        min_ema_gap_atr: float = 0.3,
        cooldown_bars: int = 10,
        min_sl_atr: float = 0.5,
        max_notional_pct: float = 2.0,
    ):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)

        # Strategy parameters
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.volume_multiplier = volume_multiplier
        self.take_profit_ratio = take_profit_ratio

        # Anti-whipsaw parameters
        self.min_ema_gap_atr = min_ema_gap_atr  # min EMA separation in ATR units
        self.cooldown_bars = cooldown_bars        # bars to wait after exit
        self.min_sl_atr = min_sl_atr              # min SL distance in ATR units
        self.max_notional_pct = max_notional_pct  # max position size as % of equity

        # Internal state
        self._swing_highs: deque[float] = deque(maxlen=swing_lookback)
        self._swing_lows: deque[float] = deque(maxlen=swing_lookback)
        self._volumes: deque[float] = deque(maxlen=20)
        self._bars_since_exit: int = 999  # starts high so first trade isn't blocked
        self._bar_count: int = 0

        # Column names derived from periods (must match FeatureEngine output)
        self._fast_col = f"EMA_{fast_period}"
        self._slow_col = f"EMA_{slow_period}"
        self._rsi_col = f"RSI_{rsi_period}"

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        # Track swing highs/lows and volume
        self._swing_highs.append(features["high"])
        self._swing_lows.append(features["low"])
        self._volumes.append(features["volume"])
        self._bar_count += 1

        # Tick cooldown counter when flat
        if self._position == "FLAT":
            self._bars_since_exit += 1

        # Need previous features for crossover detection
        if self._prev_features is None:
            return None

        # Get indicator values
        curr_fast = features.get(self._fast_col)
        curr_slow = features.get(self._slow_col)
        prev_fast = self._prev_features.get(self._fast_col)
        prev_slow = self._prev_features.get(self._slow_col)
        rsi = features.get(self._rsi_col)
        atr = features.get("ATR_14")

        # Skip if any indicator is missing (warmup period)
        if any(pd.isna(v) for v in [curr_fast, curr_slow, prev_fast, prev_slow, rsi, atr]):
            return None

        # Volume confirmation
        if len(self._volumes) < 5:
            return None
        avg_volume = sum(self._volumes) / len(self._volumes)
        volume = features["volume"]
        volume_confirmed = volume > avg_volume * self.volume_multiplier

        close = features["close"]

        # ── CLOSE signal (opposite crossover while in position) ──
        # Check exits FIRST — always allow exits regardless of cooldown
        if self._position == "LONG" and prev_fast >= prev_slow and curr_fast < curr_slow:
            self._bars_since_exit = 0
            return Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=0.8,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
            )

        if self._position == "SHORT" and prev_fast <= prev_slow and curr_fast > curr_slow:
            self._bars_since_exit = 0
            return Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=0.8,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
            )

        # ── Anti-whipsaw gate: skip entries during cooldown ──
        if self._bars_since_exit < self.cooldown_bars:
            return None

        # ── MACD histogram confirmation ──
        macd_hist = features.get("MACD_hist")
        has_macd = pd.notna(macd_hist) if macd_hist is not None else False

        # ── ADX trend strength filter ──
        adx = features.get("ADX_14")
        has_adx = pd.notna(adx) if adx is not None else False
        if has_adx and adx < 20:
            return None  # no trade in choppy/ranging markets

        # ── LONG signal ──
        if (prev_fast <= prev_slow and curr_fast > curr_slow  # bullish crossover
                and rsi < self.rsi_overbought                  # not overbought
                and volume_confirmed                           # volume confirms
                and (not has_macd or macd_hist > 0)            # MACD agrees (bullish)
                and self._position != "LONG"):                 # not already long

            stop_loss = min(self._swing_lows) if self._swing_lows else close * 0.99
            risk = close - stop_loss

            # Enforce minimum stop distance (prevents oversized positions)
            min_risk = self.min_sl_atr * atr
            if risk < min_risk:
                stop_loss = close - min_risk
                risk = min_risk

            if risk <= 0:
                return None

            take_profit = close + risk * self.take_profit_ratio

            # Cap position size: risk-based qty vs max notional
            risk_qty = (close * self.max_notional_pct * self.max_risk_per_trade) / risk
            max_qty = (close * self.max_notional_pct) / close  # max_notional_pct of 1 unit
            quantity_hint = min(risk_qty, max_qty)

            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=min(1.0, (rsi / 100) * 1.5),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "ema_fast": round(float(curr_fast), 4),
                    "ema_slow": round(float(curr_slow), 4),
                    "rsi": round(float(rsi), 2),
                    "atr": round(float(atr), 4),
                    "macd_hist": round(float(macd_hist), 4) if has_macd else None,
                    "volume_ratio": round(volume / avg_volume, 2) if avg_volume > 0 else 0,
                    "risk_distance": round(risk, 8),
                },
            )

        # ── SHORT signal ──
        if (prev_fast >= prev_slow and curr_fast < curr_slow  # bearish crossover
                and rsi > self.rsi_oversold                    # not oversold
                and volume_confirmed                           # volume confirms
                and (not has_macd or macd_hist < 0)            # MACD agrees (bearish)
                and self._position != "SHORT"):                # not already short

            stop_loss = max(self._swing_highs) if self._swing_highs else close * 1.01
            risk = stop_loss - close

            # Enforce minimum stop distance
            min_risk = self.min_sl_atr * atr
            if risk < min_risk:
                stop_loss = close + min_risk
                risk = min_risk

            if risk <= 0:
                return None

            take_profit = close - risk * self.take_profit_ratio

            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=min(1.0, ((100 - rsi) / 100) * 1.5),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "ema_fast": round(float(curr_fast), 4),
                    "ema_slow": round(float(curr_slow), 4),
                    "rsi": round(float(rsi), 2),
                    "atr": round(float(atr), 4),
                    "macd_hist": round(float(macd_hist), 4) if has_macd else None,
                    "volume_ratio": round(volume / avg_volume, 2) if avg_volume > 0 else 0,
                    "risk_distance": round(risk, 8),
                },
            )

        return None

    @classmethod
    def from_config(cls, name: str) -> EMAScalpStrategy:
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["BTCUSDT"]),
            timeframe=strat_cfg.get("timeframe", "1m"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.01),
            fast_period=strat_cfg.get("fast_period", 9),
            slow_period=strat_cfg.get("slow_period", 21),
            rsi_period=strat_cfg.get("rsi_period", 7),
            rsi_overbought=strat_cfg.get("rsi_overbought", 70.0),
            rsi_oversold=strat_cfg.get("rsi_oversold", 30.0),
            volume_multiplier=strat_cfg.get("volume_multiplier", 1.2),
            take_profit_ratio=strat_cfg.get("take_profit_ratio", 2.0),
            min_ema_gap_atr=strat_cfg.get("min_ema_gap_atr", 0.3),
            cooldown_bars=strat_cfg.get("cooldown_bars", 10),
            min_sl_atr=strat_cfg.get("min_sl_atr", 0.5),
            max_notional_pct=strat_cfg.get("max_notional_pct", 2.0),
        )
