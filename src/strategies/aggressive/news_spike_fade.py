"""NFP/FOMC spike-fade strategy for the aggressive retail sub-book."""

from __future__ import annotations

import pandas as pd

from src.backtest.costs import NewsWindow, load_news_calendar_csv
from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction


_PIP_SIZE = 0.10


class NewsSpikeFadeStrategy(BaseStrategy):
    def __init__(
        self,
        name: str = "news_spike_fade",
        markets: list[str] | None = None,
        timeframe: str = "5m",
        risk_profile: RiskProfile = RiskProfile.AGGRESSIVE,
        max_risk_per_trade: float = 0.05,
        *,
        leverage_range: tuple[float, float] = (500.0, 1000.0),
        news_windows: list[NewsWindow] | None = None,
        news_calendar_path: str | None = None,
        spike_trigger_pips: float = 30.0,
        target_1_pct: float = 0.5,        # 50% of spike distance
        hard_sl_pips: float = 40.0,
        max_hold_bars: int = 3,
        pip_size: float = _PIP_SIZE,
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
        )
        if news_windows is None and news_calendar_path is not None:
            news_windows = load_news_calendar_csv(news_calendar_path)
        self.news_windows = tuple(news_windows or [])
        self.spike_trigger_pips = spike_trigger_pips
        self.target_1_pct = target_1_pct
        self.hard_sl_pips = hard_sl_pips
        self.max_hold_bars = max_hold_bars
        self.pip_size = pip_size

        self._entry_price: float = 0.0
        self._spike_distance: float = 0.0
        self._bars_in_position: int = 0

    def _in_news_window(self, ts_ms: int) -> bool:
        return any(w.contains(ts_ms) for w in self.news_windows)

    def _reset_state(self) -> None:
        self._entry_price = 0.0
        self._spike_distance = 0.0
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
        ts_ms = int(features.get("timestamp", 0) or 0)

        # ── Exit management ──
        if self._position in ("LONG", "SHORT"):
            self._bars_in_position += 1

            if self._position == "LONG":  # faded a down-spike, want price to recover
                target_1 = self._entry_price + self._spike_distance * self.target_1_pct
                hard_sl = self._entry_price - self.hard_sl_pips * self.pip_size
                if high >= target_1:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=target_1,
                        metadata={"exit_reason": "target_1"},
                    )
                if low <= hard_sl:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=hard_sl,
                        metadata={"exit_reason": "hard_sl"},
                    )
            else:  # SHORT — faded an up-spike
                target_1 = self._entry_price - self._spike_distance * self.target_1_pct
                hard_sl = self._entry_price + self.hard_sl_pips * self.pip_size
                if low <= target_1:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=target_1,
                        metadata={"exit_reason": "target_1"},
                    )
                if high >= hard_sl:
                    self._reset_state()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=hard_sl,
                        metadata={"exit_reason": "hard_sl"},
                    )

            if self._bars_in_position >= self.max_hold_bars:
                self._reset_state()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.5,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "timeout"},
                )
            return None

        # ── Entry: inside a news window + spike > trigger ──
        if not self._in_news_window(ts_ms):
            return None

        spike = (close - open_)
        spike_abs = abs(spike) / self.pip_size
        if spike_abs < self.spike_trigger_pips:
            return None

        # Fade — enter opposite the spike direction
        if spike > 0:
            action = SignalAction.SHORT
            sl_price = close + self.hard_sl_pips * self.pip_size
        else:
            action = SignalAction.LONG
            sl_price = close - self.hard_sl_pips * self.pip_size

        self._entry_price = close
        self._spike_distance = abs(spike)
        self._bars_in_position = 0

        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.9,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            stop_loss=sl_price,
            risk_pct=self.max_risk_per_trade,
            metadata={"spike_pips": spike_abs},
        )
