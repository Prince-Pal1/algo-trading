"""Bollinger Band + RSI Mean Reversion with ADX Regime Filter.

Trades mean reversion in RANGING markets only (ADX < threshold).
When price touches outer BB and RSI confirms oversold/overbought,
enters expecting reversion to the middle band (20-SMA).

Entry logic:
    LONG:  close <= BBL_20 AND RSI_14 < rsi_oversold AND ADX_14 < adx_threshold
    SHORT: close >= BBU_20 AND RSI_14 > rsi_overbought AND ADX_14 < adx_threshold

Exit logic:
    - Target: price reverts to BBM_20 (middle band)
    - Stop loss: entry ± sl_atr_mult × ATR_14
    - Timeout: max_hold_bars candles

Config: reads from [bb_rsi_mr] section in strategies.toml.
"""

from __future__ import annotations

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction


class BBRSIMeanRevStrategy(BaseStrategy):
    """Bollinger Band + RSI mean reversion strategy with ADX regime filter."""

    def __init__(
        self,
        name: str = "bb_rsi_mr",
        markets: list[str] | None = None,
        timeframe: str = "1h",
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        # Indicator periods
        bb_period: int = 20,
        rsi_period: int = 14,
        adx_period: int = 14,
        atr_period: int = 14,
        # Signal thresholds
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        adx_threshold: float = 25.0,
        # Risk management
        sl_atr_mult: float = 2.0,
        max_hold_bars: int = 48,
        cooldown_bars: int = 5,
        max_notional_pct: float = 2.0,
    ):
        super().__init__(
            name,
            markets if markets is not None else ["BTCUSDT"],
            timeframe,
            risk_profile,
            max_risk_per_trade,
        )

        # Indicator settings
        self.bb_period = bb_period
        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.atr_period = atr_period

        # Signal thresholds
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.adx_threshold = adx_threshold

        # Risk management
        self.sl_atr_mult = sl_atr_mult
        self.max_hold_bars = max_hold_bars
        self.cooldown_bars = cooldown_bars
        self.max_notional_pct = max_notional_pct

        # Internal state
        self._bars_since_exit: int = 999
        self._bars_in_position: int = 0
        self._entry_bbm: float = 0.0  # middle band at entry (reversion target)

        # Column names
        self._bbu_col = f"BBU_{bb_period}"
        self._bbm_col = f"BBM_{bb_period}"
        self._bbl_col = f"BBL_{bb_period}"
        self._rsi_col = f"RSI_{rsi_period}"
        self._adx_col = f"ADX_{adx_period}"
        self._atr_col = f"ATR_{atr_period}"

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

        # Read indicators
        close = features["close"]
        bbu = features.get(self._bbu_col)
        bbm = features.get(self._bbm_col)
        bbl = features.get(self._bbl_col)
        rsi = features.get(self._rsi_col)
        adx = features.get(self._adx_col)
        atr = features.get(self._atr_col)

        # Skip if any indicator is still warming up
        if any(pd.isna(v) for v in [bbu, bbm, bbl, rsi, adx, atr] if v is not None):
            return None
        if any(v is None for v in [bbu, bbm, bbl, rsi, adx, atr]):
            return None

        # ── EXIT LOGIC (check first) ──

        if self._position == "LONG":
            # Exit when price reverts to middle band
            if close >= bbm:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "mean_reversion_target", "rsi": round(float(rsi), 1)},
                )

            # Timeout exit
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
            # Exit when price reverts to middle band
            if close <= bbm:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "mean_reversion_target", "rsi": round(float(rsi), 1)},
                )

            # Timeout exit
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

        # Cooldown gate
        if self._bars_since_exit < self.cooldown_bars:
            return None

        # ADX regime filter: only trade in ranging markets
        if adx >= self.adx_threshold:
            return None

        # ── LONG ENTRY: price at/below lower BB + RSI oversold ──
        if (close <= bbl
                and rsi < self.rsi_oversold
                and self._position != "LONG"):

            stop_loss = close - self.sl_atr_mult * atr
            # TP = middle band (the reversion target)
            take_profit = bbm

            risk = close - stop_loss
            if risk <= 0:
                return None

            # Confidence: more extreme RSI = higher confidence
            confidence = min(1.0, (self.rsi_oversold - rsi) / self.rsi_oversold * 2)

            self._bars_in_position = 0
            self._entry_bbm = bbm

            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=max(0.5, confidence),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "rsi": round(float(rsi), 1),
                    "adx": round(float(adx), 1),
                    "bb_position": "below_lower",
                    "atr": round(float(atr), 4),
                    "distance_to_mean": round(float(bbm - close), 2),
                },
            )

        # ── SHORT ENTRY: price at/above upper BB + RSI overbought ──
        if (close >= bbu
                and rsi > self.rsi_overbought
                and self._position != "SHORT"):

            stop_loss = close + self.sl_atr_mult * atr
            take_profit = bbm

            risk = stop_loss - close
            if risk <= 0:
                return None

            confidence = min(1.0, (rsi - self.rsi_overbought) / (100 - self.rsi_overbought) * 2)

            self._bars_in_position = 0
            self._entry_bbm = bbm

            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=max(0.5, confidence),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "rsi": round(float(rsi), 1),
                    "adx": round(float(adx), 1),
                    "bb_position": "above_upper",
                    "atr": round(float(atr), 4),
                    "distance_to_mean": round(float(close - bbm), 2),
                },
            )

        return None

    @classmethod
    def from_config(cls, name: str) -> BBRSIMeanRevStrategy:
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["BTCUSDT"]),
            timeframe=strat_cfg.get("timeframe", "15m"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.01),
            bb_period=strat_cfg.get("bb_period", 20),
            rsi_period=strat_cfg.get("rsi_period", 14),
            adx_period=strat_cfg.get("adx_period", 14),
            atr_period=strat_cfg.get("atr_period", 14),
            rsi_overbought=strat_cfg.get("rsi_overbought", 70.0),
            rsi_oversold=strat_cfg.get("rsi_oversold", 30.0),
            adx_threshold=strat_cfg.get("adx_threshold", 25.0),
            sl_atr_mult=strat_cfg.get("sl_atr_mult", 2.0),
            max_hold_bars=strat_cfg.get("max_hold_bars", 48),
            cooldown_bars=strat_cfg.get("cooldown_bars", 5),
            max_notional_pct=strat_cfg.get("max_notional_pct", 2.0),
        )
