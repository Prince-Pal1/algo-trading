"""Donchian Channel Ensemble Trend-Following Strategy.

Trades breakouts confirmed by multiple Donchian channel timeframes.
When price breaks above/below the channel on 2+ of 3 periods,
enters a trend trade with ATR-based stops and trailing exit.

Reference: SSRN:5209907 (Zarattini et al., 2025) — tested on 20 liquid cryptos since 2015.
Reported Sharpe 1.58, Sortino 2.03, 30% CAGR.

Entry logic:
    LONG:  close > DCH on 2+ of [short, medium, long] channels
    SHORT: close < DCL on 2+ of [short, medium, long] channels

Exit logic:
    - Trailing stop: short-period DCL for longs, DCH for shorts
    - Hard stop: entry ± sl_atr_mult × ATR
    - Timeout: max_hold_bars candles

Config: reads from [donchian_ensemble] section in strategies.toml.
"""

from __future__ import annotations

import pandas as pd

from src.strategies.base import BaseStrategy
from src.strategies.filters.regime_filter import VPINRegimeFilter
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


class DonchianEnsembleStrategy(BaseStrategy):
    """Multi-period Donchian channel breakout ensemble."""

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
        # Channel periods
        dc_short: int = 20,
        dc_medium: int = 55,
        dc_long: int = 120,
        # Risk management
        atr_period: int = 14,
        sl_atr_mult: float = 2.5,
        max_hold_bars: int = 120,
        cooldown_bars: int = 5,
        # Ensemble threshold: how many channels must agree
        min_channels: int = 2,
        # Optional enhancements
        long_only: bool = False,
        adx_trend_threshold: float | None = None,
        vpin_filter: VPINRegimeFilter | None = None,
    ):
        super().__init__(
            name, markets, timeframe, risk_profile, max_risk_per_trade,
            leverage_range=leverage_range, tier=tier,
        )

        self.dc_short = dc_short
        self.dc_medium = dc_medium
        self.dc_long = dc_long
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.max_hold_bars = max_hold_bars
        self.cooldown_bars = cooldown_bars
        self.min_channels = min_channels
        self.long_only = long_only
        self.adx_trend_threshold = adx_trend_threshold
        self.vpin_filter = vpin_filter

        # Internal state
        self._bars_since_exit: int = 999
        self._bars_in_position: int = 0
        self._entry_price: float = 0.0

        # Column names for each channel period
        self._channels = [dc_short, dc_medium, dc_long]
        self._dch_cols = [f"DCH_{p}" for p in self._channels]
        self._dcl_cols = [f"DCL_{p}" for p in self._channels]
        self._dcm_cols = [f"DCM_{p}" for p in self._channels]
        self._atr_col = f"ATR_{atr_period}"
        # Trailing stop uses the shortest channel
        self._trail_dcl = f"DCL_{dc_short}"
        self._trail_dch = f"DCH_{dc_short}"

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

        close = features["close"]
        atr = features.get(self._atr_col)
        prev = self._prev_features

        # Update VPIN filter if present
        if self.vpin_filter is not None:
            self.vpin_filter.update(
                features.get("open", close), features.get("high", close),
                features.get("low", close), close,
                features.get("volume", 0),
            )

        # Need previous bar's Donchian channels (current bar includes current high/low,
        # making close > DCH impossible since close <= high always)
        if prev is None:
            return None

        # Use PREVIOUS bar's channels for breakout detection
        dch_vals = [prev.get(c) for c in self._dch_cols]
        dcl_vals = [prev.get(c) for c in self._dcl_cols]

        # Current bar's trailing stop channels (for exit management)
        trail_dcl = prev.get(self._trail_dcl)
        trail_dch = prev.get(self._trail_dch)

        # Skip if any indicator still warming up
        all_vals = dch_vals + dcl_vals + [atr, trail_dcl, trail_dch]
        if any(v is None for v in all_vals):
            return None
        if any(pd.isna(v) for v in all_vals):
            return None

        # ── EXIT LOGIC (check first) ──

        if self._position == "LONG":
            # Trailing stop: price drops below short-period Donchian low
            if close <= trail_dcl:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "trailing_stop", "trail_dcl": round(float(trail_dcl), 2)},
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
            # Trailing stop: price rises above short-period Donchian high
            if close >= trail_dch:
                self._bars_since_exit = 0
                self._bars_in_position = 0
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "trailing_stop", "trail_dch": round(float(trail_dch), 2)},
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

        # VPIN gate — block entries during toxic flow
        if self.vpin_filter is not None and not self.vpin_filter.should_trade():
            return None

        # ADX trend confirmation gate
        if self.adx_trend_threshold is not None:
            adx_val = features.get("ADX_14", 0)
            if adx_val is None or pd.isna(adx_val) or adx_val < self.adx_trend_threshold:
                return None

        # Count how many channels signal breakout
        long_votes = sum(1 for dch in dch_vals if close > dch)
        short_votes = sum(1 for dcl in dcl_vals if close < dcl)

        # ── LONG ENTRY: breakout above upper channels ──
        if long_votes >= self.min_channels and self._position != "LONG":
            stop_loss = close - self.sl_atr_mult * atr
            # TP: let the trailing stop handle it — set a generous target
            take_profit = close + self.sl_atr_mult * atr * 3

            risk = close - stop_loss
            if risk <= 0:
                return None

            confidence = min(1.0, long_votes / len(self._channels))
            self._bars_in_position = 0
            self._entry_price = close

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
                    "long_votes": long_votes,
                    "atr": round(float(atr), 4),
                    "channels_agreeing": f"{long_votes}/{len(self._channels)}",
                },
            )

        # ── SHORT ENTRY: breakout below lower channels ──
        if self.long_only:
            return None
        if short_votes >= self.min_channels and self._position != "SHORT":
            stop_loss = close + self.sl_atr_mult * atr
            take_profit = close - self.sl_atr_mult * atr * 3

            risk = stop_loss - close
            if risk <= 0:
                return None

            confidence = min(1.0, short_votes / len(self._channels))
            self._bars_in_position = 0
            self._entry_price = close

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
                    "short_votes": short_votes,
                    "atr": round(float(atr), 4),
                    "channels_agreeing": f"{short_votes}/{len(self._channels)}",
                },
            )

        return None

    @classmethod
    def from_config(cls, name: str) -> DonchianEnsembleStrategy:
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["ETHUSDT"]),
            timeframe=strat_cfg.get("timeframe", "1h"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.015),
            dc_short=strat_cfg.get("dc_short", 20),
            dc_medium=strat_cfg.get("dc_medium", 55),
            dc_long=strat_cfg.get("dc_long", 120),
            atr_period=strat_cfg.get("atr_period", 14),
            sl_atr_mult=strat_cfg.get("sl_atr_mult", 2.5),
            max_hold_bars=strat_cfg.get("max_hold_bars", 120),
            cooldown_bars=strat_cfg.get("cooldown_bars", 5),
            min_channels=strat_cfg.get("min_channels", 2),
            adx_trend_threshold=strat_cfg.get("adx_trend_threshold"),
        )
