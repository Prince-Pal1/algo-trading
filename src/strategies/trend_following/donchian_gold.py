"""Session-filtered Donchian ensemble for XAUUSD with a declared leverage range."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.strategies.trend_following.donchian_ensemble import DonchianEnsembleStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction


LONDON_START_UTC = (7, 0)
LONDON_END_UTC = (11, 0)
NY_START_UTC = (13, 30)
NY_END_UTC = (16, 30)


def _in_window(hm: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    h, m = hm
    sh, sm = start
    eh, em = end
    now = h * 60 + m
    return (sh * 60 + sm) <= now <= (eh * 60 + em)


def _is_in_session(ts_ms: int) -> bool:
    if ts_ms <= 0:
        return True
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hm = (dt.hour, dt.minute)
    return _in_window(hm, LONDON_START_UTC, LONDON_END_UTC) or _in_window(
        hm, NY_START_UTC, NY_END_UTC
    )


class DonchianGoldStrategy(DonchianEnsembleStrategy):
    def __init__(
        self,
        name: str = "donchian_gold",
        markets: list[str] | None = None,
        timeframe: str = "1h",
        risk_profile: RiskProfile = RiskProfile.MODERATE,
        max_risk_per_trade: float = 0.02,  # tuned: +38% / 12% DD / Calmar 1.675
        *,
        leverage_range: tuple[float, float] = (10.0, 50.0),
        session_filter: bool = True,
        dc_short: int = 20,
        dc_medium: int = 55,
        dc_long: int = 120,
        atr_period: int = 14,
        sl_atr_mult: float = 3.0,  # tuned
        max_hold_bars: int = 120,
        cooldown_bars: int = 5,
        min_channels: int = 2,
        long_only: bool = False,
        adx_trend_threshold: float | None = 25.0,  # tuned
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
            dc_short=dc_short,
            dc_medium=dc_medium,
            dc_long=dc_long,
            atr_period=atr_period,
            sl_atr_mult=sl_atr_mult,
            max_hold_bars=max_hold_bars,
            cooldown_bars=cooldown_bars,
            min_channels=min_channels,
            long_only=long_only,
            adx_trend_threshold=adx_trend_threshold,
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

        # Always allow exits (CLOSE) regardless of session; only gate entries.
        if signal.action in (SignalAction.LONG, SignalAction.SHORT) and self.session_filter:
            ts_ms = int(features.get("timestamp", 0) or 0)
            if not _is_in_session(ts_ms):
                # Roll back the position-bookkeeping that the parent did.
                self._bars_in_position = 0
                return None

        return signal

    @classmethod
    def from_config(cls, name: str = "donchian_gold") -> "DonchianGoldStrategy":
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
            session_filter=strat_cfg.get("session_filter", True),
            dc_short=strat_cfg.get("dc_short", 20),
            dc_medium=strat_cfg.get("dc_medium", 55),
            dc_long=strat_cfg.get("dc_long", 120),
            atr_period=strat_cfg.get("atr_period", 14),
            sl_atr_mult=strat_cfg.get("sl_atr_mult", 2.5),
            max_hold_bars=strat_cfg.get("max_hold_bars", 120),
            cooldown_bars=strat_cfg.get("cooldown_bars", 5),
            min_channels=strat_cfg.get("min_channels", 2),
            long_only=strat_cfg.get("long_only", False),
            adx_trend_threshold=strat_cfg.get("adx_trend_threshold", 20.0),
        )
