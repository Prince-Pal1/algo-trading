"""Vol-scaled momentum for XAUUSD with leverage_range + session filter."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.strategies.momentum.vol_momentum import VolMomentumStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


LONDON_START_UTC = (7, 0)
LONDON_END_UTC = (11, 0)
NY_START_UTC = (13, 30)
NY_END_UTC = (16, 30)


def _in_window(hm: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    now = hm[0] * 60 + hm[1]
    return (start[0] * 60 + start[1]) <= now <= (end[0] * 60 + end[1])


def _is_in_session(ts_ms: int) -> bool:
    if ts_ms <= 0:
        return True
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hm = (dt.hour, dt.minute)
    return _in_window(hm, LONDON_START_UTC, LONDON_END_UTC) or _in_window(
        hm, NY_START_UTC, NY_END_UTC
    )


class VolMomentumGoldStrategy(VolMomentumStrategy):
    def __init__(
        self,
        name: str = "vol_momentum_gold",
        markets: list[str] | None = None,
        timeframe: str = "1h",
        risk_profile: RiskProfile = RiskProfile.MODERATE,
        max_risk_per_trade: float = 0.01,
        *,
        leverage_range: tuple[float, float] = (10.0, 50.0),
        session_filter: bool = True,  # tuned: +20.27% / Calmar 1.365
        momentum_window: int = 240,   # tuned
        vol_lookback: int = 240,      # tuned
        vol_target: float = 0.20,     # tuned
        atr_period: int = 14,
        sl_atr_mult: float = 3.0,     # tuned
        max_hold_bars: int = 168,
        cooldown_bars: int = 5,
        long_only: bool = True,       # tuned: long-only gave better Calmar on gold uptrend
        rebalance_interval: int = 24,
        momentum_threshold: float = 0.0,
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
            tier=Tier.INSTITUTIONAL_MR,
            momentum_window=momentum_window,
            vol_lookback=vol_lookback,
            vol_target=vol_target,
            atr_period=atr_period,
            sl_atr_mult=sl_atr_mult,
            max_hold_bars=max_hold_bars,
            cooldown_bars=cooldown_bars,
            long_only=long_only,
            rebalance_interval=rebalance_interval,
            momentum_threshold=momentum_threshold,
        )
        self.session_filter = session_filter

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        signal = super().on_features(symbol, timeframe, features)
        if signal is None:
            return None

        # Always allow exits; only gate entries on session
        if signal.action in (SignalAction.LONG, SignalAction.SHORT) and self.session_filter:
            ts_ms = int(features.get("timestamp", 0) or 0)
            if not _is_in_session(ts_ms):
                return None

        return signal

    @classmethod
    def from_config(cls, name: str = "vol_momentum_gold") -> "VolMomentumGoldStrategy":
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        lr = strat_cfg.get("leverage_range", [10.0, 50.0])
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["XAUUSD"]),
            timeframe=strat_cfg.get("timeframe", "1h"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "MODERATE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.01),
            leverage_range=(float(lr[0]), float(lr[1])),
            session_filter=strat_cfg.get("session_filter", False),
            momentum_window=strat_cfg.get("momentum_window", 168),
            vol_lookback=strat_cfg.get("vol_lookback", 168),
            vol_target=strat_cfg.get("vol_target", 0.15),
            atr_period=strat_cfg.get("atr_period", 14),
            sl_atr_mult=strat_cfg.get("sl_atr_mult", 3.0),
            max_hold_bars=strat_cfg.get("max_hold_bars", 168),
            cooldown_bars=strat_cfg.get("cooldown_bars", 5),
            long_only=strat_cfg.get("long_only", False),
            rebalance_interval=strat_cfg.get("rebalance_interval", 24),
            momentum_threshold=strat_cfg.get("momentum_threshold", 0.0),
        )
