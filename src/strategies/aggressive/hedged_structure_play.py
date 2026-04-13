"""Hedged structure-play state-machine strategy for the aggressive sub-book.

State:
    INITIAL → primary open
    LONG/SHORT → primary running; if primary adverse by > hedge_trigger_pct,
        open opposite hedge → enter HEDGED state
    HEDGED → wait for structure level; on trigger, close the leg going
        against the structure direction → enter UNHEDGED state
    UNHEDGED → trail the remaining leg → exit

This strategy needs the structure_levels helpers + the backtest engine's
ability to manage multiple positions per strategy. For MVP we run the state
machine inside on_features and emit signals as LONG/SHORT/CLOSE — the engine
tracks positions but doesn't know about "primary vs hedge" semantics. We
fake it by having the strategy track both legs internally and synthesize
the right CLOSE events.

Deferred refinement: when the engine supports multi-position-per-strategy
cleanly (G.2c+), this strategy will emit explicit HEDGE signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from src.backtest.structure_levels import (
    StructureLevel,
    nearest_level_above,
    nearest_level_below,
)
from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction


class HedgeState(str, Enum):
    FLAT = "FLAT"
    PRIMARY_LONG = "PRIMARY_LONG"
    PRIMARY_SHORT = "PRIMARY_SHORT"
    HEDGED_FROM_LONG = "HEDGED_FROM_LONG"
    HEDGED_FROM_SHORT = "HEDGED_FROM_SHORT"
    UNHEDGED_LONG = "UNHEDGED_LONG"
    UNHEDGED_SHORT = "UNHEDGED_SHORT"


@dataclass
class _Leg:
    entry_price: float
    direction: int   # +1 LONG, -1 SHORT


@dataclass
class _State:
    state: HedgeState = HedgeState.FLAT
    primary: _Leg | None = None
    hedge: _Leg | None = None
    bars_in_state: int = 0
    structure_levels: list[StructureLevel] = field(default_factory=list)


class HedgedStructurePlayStrategy(BaseStrategy):
    def __init__(
        self,
        name: str = "hedged_structure_play",
        markets: list[str] | None = None,
        timeframe: str = "5m",
        risk_profile: RiskProfile = RiskProfile.AGGRESSIVE,
        max_risk_per_trade: float = 0.05,
        *,
        leverage_range: tuple[float, float] = (500.0, 1000.0),
        hedge_trigger_pct: float = 0.01,       # 1% adverse → hedge
        max_bars_in_hedge: int = 288,          # ~24h on M5 = 288 bars
        max_bars_in_primary: int = 72,         # ~6h on M5
        structure_proximity_pips: float = 5.0,
        pip_size: float = 0.10,
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
        )
        self.hedge_trigger_pct = hedge_trigger_pct
        self.max_bars_in_hedge = max_bars_in_hedge
        self.max_bars_in_primary = max_bars_in_primary
        self.structure_proximity_pips = structure_proximity_pips
        self.pip_size = pip_size

        self._s = _State()

    def set_structure_levels(self, levels: list[StructureLevel]) -> None:
        self._s.structure_levels = list(levels)

    def _reset_to_flat(self) -> None:
        self._s = _State()

    def _open_primary(
        self,
        symbol: str,
        timeframe: str,
        close: float,
        atr: float,
        direction: int,
    ) -> Signal:
        self._s.primary = _Leg(entry_price=close, direction=direction)
        self._s.state = HedgeState.PRIMARY_LONG if direction > 0 else HedgeState.PRIMARY_SHORT
        self._s.bars_in_state = 0
        sl_offset = 0.004 * close  # wide SL — relies on broker stop-out as backstop
        sl = close - sl_offset if direction > 0 else close + sl_offset
        action = SignalAction.LONG if direction > 0 else SignalAction.SHORT
        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.85,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            stop_loss=sl,
            risk_pct=self.max_risk_per_trade,
            metadata={"leg": "primary"},
        )

    def _close_current(self, symbol: str, timeframe: str, close: float, reason: str) -> Signal:
        self._reset_to_flat()
        return Signal(
            symbol=symbol,
            action=SignalAction.CLOSE,
            confidence=1.0,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            metadata={"exit_reason": reason},
        )

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = float(features["close"])
        high = float(features["high"])
        low = float(features["low"])
        atr = float(features.get("ATR_14", 0.0) or 0.0)
        s = self._s
        s.bars_in_state += 1

        # ── FLAT → enter primary on a simple seed (first bar after reset) ──
        if s.state == HedgeState.FLAT:
            if atr <= 0:
                return None
            # Simple seed: go with the direction of the current bar
            open_ = float(features["open"])
            direction = 1 if close >= open_ else -1
            return self._open_primary(symbol, timeframe, close, atr, direction)

        # ── PRIMARY running ──
        if s.state in (HedgeState.PRIMARY_LONG, HedgeState.PRIMARY_SHORT):
            if s.primary is None:
                self._reset_to_flat()
                return None

            primary_dir = s.primary.direction
            adverse_distance = (
                (s.primary.entry_price - low) if primary_dir > 0
                else (high - s.primary.entry_price)
            )
            adverse_pct = adverse_distance / max(s.primary.entry_price, 1e-9)

            # Favorable exit: if we're up a full 1%, close primary with gain
            favorable_distance = (
                (high - s.primary.entry_price) if primary_dir > 0
                else (s.primary.entry_price - low)
            )
            favorable_pct = favorable_distance / max(s.primary.entry_price, 1e-9)
            if favorable_pct >= 0.01:
                return self._close_current(symbol, timeframe, close, "primary_gain")

            # Hedge trigger: adverse by hedge_trigger_pct → open opposite leg
            if adverse_pct >= self.hedge_trigger_pct:
                s.hedge = _Leg(entry_price=close, direction=-primary_dir)
                s.state = (
                    HedgeState.HEDGED_FROM_LONG if primary_dir > 0
                    else HedgeState.HEDGED_FROM_SHORT
                )
                s.bars_in_state = 0
                action = SignalAction.SHORT if primary_dir > 0 else SignalAction.LONG
                return Signal(
                    symbol=symbol, action=action, confidence=0.85,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    risk_pct=self.max_risk_per_trade,
                    metadata={"leg": "hedge"},
                )

            # Timeout
            if s.bars_in_state >= self.max_bars_in_primary:
                return self._close_current(symbol, timeframe, close, "primary_timeout")

            return None

        # ── HEDGED — wait for structure level ──
        if s.state in (HedgeState.HEDGED_FROM_LONG, HedgeState.HEDGED_FROM_SHORT):
            # Timeout — force-close both
            if s.bars_in_state >= self.max_bars_in_hedge:
                return self._close_current(symbol, timeframe, close, "hedge_timeout")

            # Check for structure-level proximity. For MVP: if price is within
            # `structure_proximity_pips` of ANY known level, consider that the
            # "structure decision." The level above/below determines which leg
            # to keep.
            touching_level = None
            prox = self.structure_proximity_pips * self.pip_size
            for lev in s.structure_levels:
                if abs(close - lev.price) <= prox:
                    touching_level = lev
                    break

            if touching_level is None:
                return None

            # Structure decision: the closer level above says price is breaking
            # up → keep the LONG leg. A level below says breaking down → keep SHORT.
            above = nearest_level_above(s.structure_levels, close)
            below = nearest_level_below(s.structure_levels, close)
            dist_above = (above.price - close) if above else float("inf")
            dist_below = (close - below.price) if below else float("inf")

            if dist_above < dist_below:
                # Keep long leg
                if s.state == HedgeState.HEDGED_FROM_LONG:
                    # Primary is long, hedge is short → close hedge, keep primary
                    s.state = HedgeState.UNHEDGED_LONG
                    s.hedge = None
                else:
                    # Primary is short, hedge is long → close primary, keep hedge as new "primary"
                    s.primary = s.hedge
                    s.hedge = None
                    s.state = HedgeState.UNHEDGED_LONG
                s.bars_in_state = 0
                return self._close_current(symbol, timeframe, close, "structure_keep_long")
            else:
                if s.state == HedgeState.HEDGED_FROM_SHORT:
                    s.state = HedgeState.UNHEDGED_SHORT
                    s.hedge = None
                else:
                    s.primary = s.hedge
                    s.hedge = None
                    s.state = HedgeState.UNHEDGED_SHORT
                s.bars_in_state = 0
                return self._close_current(symbol, timeframe, close, "structure_keep_short")

        # ── UNHEDGED — single leg remains; trail to exit ──
        if s.state in (HedgeState.UNHEDGED_LONG, HedgeState.UNHEDGED_SHORT):
            # Simple exit: time-out after max_bars_in_primary bars
            if s.bars_in_state >= self.max_bars_in_primary:
                return self._close_current(symbol, timeframe, close, "unhedged_timeout")
            return None

        return None
