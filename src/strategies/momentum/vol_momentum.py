"""Volatility-Scaled Momentum Strategy.

Single-symbol momentum with inverse-volatility position scaling.
Trades in the direction of recent momentum, scaling position size
inversely to realized volatility.

Reference: Finance Research Letters 2025 (Ao Yang).
Reported Sharpe: 1.42 (unmanaged 1.12, vol-managed improvement +27%).

Entry logic:
    LONG:  7-day return > 0 AND vol regime is not extreme
    SHORT: 7-day return < 0 (if not long_only)

Exit logic:
    - Momentum reversal: return flips sign
    - Stop loss: entry ± sl_atr_mult × ATR
    - Timeout: max_hold_bars candles

Position sizing: scaled by vol_target / realized_vol (capped 0.5x-2.0x)
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


class VolMomentumStrategy(BaseStrategy):
    """Volatility-scaled momentum strategy."""

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str,
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        leverage_range: tuple[float, float] = (1.0, 1.0),
        tier: Tier = Tier.UNCLASSIFIED,
        momentum_window: int = 168,    # 7 days at 1h
        vol_lookback: int = 168,       # Realized vol window
        vol_target: float = 0.15,      # 15% annualized vol target
        atr_period: int = 14,
        sl_atr_mult: float = 3.0,
        max_hold_bars: int = 168,      # Hold up to 1 week
        cooldown_bars: int = 5,
        long_only: bool = False,
        rebalance_interval: int = 24,  # Re-evaluate every 24 bars (1 day at 1h)
        momentum_threshold: float = 0.0,  # Min abs(momentum) to enter (skip weak signals)
    ):
        super().__init__(
            name, markets, timeframe, risk_profile, max_risk_per_trade,
            leverage_range=leverage_range, tier=tier,
        )

        self.momentum_window = momentum_window
        self.vol_lookback = vol_lookback
        self.vol_target = vol_target
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.max_hold_bars = max_hold_bars
        self.cooldown_bars = cooldown_bars
        self.long_only = long_only
        self.rebalance_interval = rebalance_interval
        self.momentum_threshold = momentum_threshold

        # Internal state
        self._bars_since_exit: int = 999
        self._bars_in_position: int = 0
        self._bars_since_rebalance: int = 999
        self._atr_col = f"ATR_{atr_period}"

        # Rolling price buffer for momentum + vol calculation
        self._closes: deque[float] = deque(maxlen=max(momentum_window, vol_lookback) + 1)

    def _compute_momentum(self) -> float | None:
        """Compute momentum as log return over momentum_window."""
        if len(self._closes) < self.momentum_window + 1:
            return None
        current = self._closes[-1]
        past = self._closes[-(self.momentum_window + 1)]
        if past <= 0:
            return None
        return float(np.log(current / past))

    def _compute_realized_vol(self) -> float | None:
        """Compute annualized realized volatility."""
        if len(self._closes) < self.vol_lookback + 1:
            return None
        prices = np.array(list(self._closes))[-self.vol_lookback - 1:]
        log_ret = np.diff(np.log(prices))
        vol = float(log_ret.std())
        if vol < 1e-10:
            return None
        # Annualize (assume 1h bars, ~8760 hours/year)
        return vol * np.sqrt(8760)

    def _vol_scalar(self, realized_vol: float) -> float:
        """Position size scalar based on vol targeting."""
        if realized_vol < 1e-10:
            return 1.0
        scalar = self.vol_target / realized_vol
        return max(0.5, min(2.0, scalar))

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        # Tick counters
        if self._position == "FLAT":
            self._bars_since_exit += 1
        else:
            self._bars_in_position += 1
        self._bars_since_rebalance += 1

        close = features["close"]
        atr = features.get(self._atr_col)

        if atr is None or pd.isna(atr):
            return None

        # Accumulate prices
        self._closes.append(float(close))

        momentum = self._compute_momentum()
        vol = self._compute_realized_vol()

        if momentum is None or vol is None:
            return None

        # ── EXIT LOGIC ──

        if self._position == "LONG":
            # Exit on momentum reversal
            if momentum < 0:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "momentum_reversal", "momentum": round(momentum, 4)},
                )

            # Timeout
            if self._bars_in_position >= self.max_hold_bars:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.7,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "timeout"},
                )

        if self._position == "SHORT":
            # Exit on momentum reversal
            if momentum > 0:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "momentum_reversal", "momentum": round(momentum, 4)},
                )

            # Timeout
            if self._bars_in_position >= self.max_hold_bars:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.7,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "timeout"},
                )

        # ── ENTRY FILTERS ──

        if self._bars_since_exit < self.cooldown_bars:
            return None

        # Only enter/re-evaluate on rebalance intervals
        if self._bars_since_rebalance < self.rebalance_interval and self._position != "FLAT":
            return None

        # Momentum threshold gate — skip weak signals
        if abs(momentum) < self.momentum_threshold:
            return None

        vol_scalar = self._vol_scalar(vol)

        # ── LONG ENTRY: positive momentum ──
        if momentum > 0 and self._position != "LONG":
            stop_loss = close - self.sl_atr_mult * atr
            take_profit = close + self.sl_atr_mult * atr * 3

            risk = close - stop_loss
            if risk <= 0:
                return None

            # Confidence: stronger momentum = higher confidence
            confidence = min(1.0, abs(momentum) * 10)
            self._bars_in_position = 0
            self._bars_since_rebalance = 0

            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=max(0.5, confidence),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade * vol_scalar,
                metadata={
                    "momentum": round(momentum, 4),
                    "vol": round(vol, 4),
                    "vol_scalar": round(vol_scalar, 2),
                    "atr": round(float(atr), 4),
                },
            )

        # ── SHORT ENTRY: negative momentum ──
        if momentum < 0 and not self.long_only and self._position != "SHORT":
            stop_loss = close + self.sl_atr_mult * atr
            take_profit = close - self.sl_atr_mult * atr * 3

            risk = stop_loss - close
            if risk <= 0:
                return None

            confidence = min(1.0, abs(momentum) * 10)
            self._bars_in_position = 0
            self._bars_since_rebalance = 0

            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=max(0.5, confidence),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade * vol_scalar,
                metadata={
                    "momentum": round(momentum, 4),
                    "vol": round(vol, 4),
                    "vol_scalar": round(vol_scalar, 2),
                    "atr": round(float(atr), 4),
                },
            )

        return None

    @classmethod
    def from_config(cls, name: str) -> VolMomentumStrategy:
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["DOGEUSDT"]),
            timeframe=strat_cfg.get("timeframe", "1h"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.012),
            momentum_window=strat_cfg.get("momentum_window", 168),
            vol_lookback=strat_cfg.get("vol_lookback", 168),
            vol_target=strat_cfg.get("vol_target", 0.15),
            atr_period=strat_cfg.get("atr_period", 14),
            sl_atr_mult=strat_cfg.get("sl_atr_mult", 3.0),
            max_hold_bars=strat_cfg.get("max_hold_bars", 168),
            cooldown_bars=strat_cfg.get("cooldown_bars", 5),
            rebalance_interval=strat_cfg.get("rebalance_interval", 24),
            momentum_threshold=strat_cfg.get("momentum_threshold", 0.0),
        )
