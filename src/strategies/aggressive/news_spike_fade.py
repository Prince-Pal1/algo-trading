"""News spike fade strategy for gold.

STATUS: NOT ALPHA-READY. See task "news_spike_fade OBITUARY" for the
full reasoning. Short version: Stage 1 research (scripts/research_
news_spike_fade.py) CONFIRMED the fade premise is real — 95.7% of
>= 30 pip XAUUSD news spikes retrace >= 50% within 30 min — but
bar-level backtesting CANNOT capture this edge because the strategy
enters at the first trigger crossing (still in the continuation phase,
before peak) and gets stopped out before the retracement starts.

Retained in src/strategies/aggressive/ with the current cumulative-
excursion-from-pre-window design for future research after tick-level
infrastructure lands (task #91 M1 path model OR real tick entry
granularity).

Design (for reference):
- Track the pre-window price at the moment a news window starts
- On each bar inside the window, measure the cumulative excursion
  (max_high vs pre_price for up-side, min_low vs pre_price for down)
- When high >= pre + spike_trigger_pips: enter SHORT AT THE TRIGGER
  LEVEL (stop-entry pattern, not bar close — avoids re-fading bars
  that already reverted)
- Exit at target_pct of the realized excursion OR hard SL at
  sl_mult × spike_pips OR time_stop_bars bars
"""

from __future__ import annotations

import pandas as pd

from src.backtest.costs import NewsWindow, load_news_calendar_csv
from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


_PIP_SIZE = 0.10


class NewsSpikeFadeStrategy(BaseStrategy):
    def __init__(
        self,
        name: str = "news_spike_fade",
        markets: list[str] | None = None,
        timeframe: str = "5m",
        risk_profile: RiskProfile = RiskProfile.AGGRESSIVE,
        max_risk_per_trade: float = 0.02,
        *,
        leverage_range: tuple[float, float] = (100.0, 500.0),
        news_windows: list[NewsWindow] | None = None,
        news_calendar_path: str | None = None,
        spike_trigger_pips: float = 30.0,
        target_pct: float = 0.5,
        sl_mult: float = 1.2,
        time_stop_bars: int = 6,   # 30 min on M5
        pip_size: float = _PIP_SIZE,
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
            tier=Tier.AGGRESSIVE_RETAIL,
        )
        if news_windows is None and news_calendar_path is not None:
            news_windows = load_news_calendar_csv(news_calendar_path)
        self.news_windows = tuple(news_windows or [])
        self.spike_trigger_pips = spike_trigger_pips
        self.target_pct = target_pct
        self.sl_mult = sl_mult
        self.time_stop_bars = time_stop_bars
        self.pip_size = pip_size

        self._pre_window_price: float | None = None
        self._active_window: NewsWindow | None = None
        self._entry_price: float = 0.0
        self._entry_spike_pips: float = 0.0
        self._spike_direction: int = 0
        self._bars_in_position: int = 0

    def _find_active_window(self, ts_ms: int) -> NewsWindow | None:
        for w in self.news_windows:
            if w.contains(ts_ms):
                return w
        return None

    def _reset_position(self) -> None:
        self._entry_price = 0.0
        self._entry_spike_pips = 0.0
        self._spike_direction = 0
        self._bars_in_position = 0

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = float(features["close"])
        high = float(features["high"])
        low = float(features["low"])
        prev = self._prev_features
        ts_ms = int(features.get("timestamp", 0) or 0)

        # ── Position management ──
        if self._position in ("LONG", "SHORT"):
            self._bars_in_position += 1
            target_pips = self._entry_spike_pips * self.target_pct
            sl_pips = self._entry_spike_pips * self.sl_mult

            entry_price_snapshot = self._entry_price
            if self._position == "LONG":
                max_gain_pips = (high - entry_price_snapshot) / self.pip_size
                max_loss_pips = (entry_price_snapshot - low) / self.pip_size
                if max_gain_pips >= target_pips:
                    exit_price = entry_price_snapshot + target_pips * self.pip_size
                    self._reset_position()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=exit_price,
                        metadata={"exit_reason": "target"},
                    )
                if max_loss_pips >= sl_pips:
                    exit_price = entry_price_snapshot - sl_pips * self.pip_size
                    self._reset_position()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=exit_price,
                        metadata={"exit_reason": "hard_sl"},
                    )
            else:  # SHORT
                max_gain_pips = (entry_price_snapshot - low) / self.pip_size
                max_loss_pips = (high - entry_price_snapshot) / self.pip_size
                if max_gain_pips >= target_pips:
                    exit_price = entry_price_snapshot - target_pips * self.pip_size
                    self._reset_position()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=exit_price,
                        metadata={"exit_reason": "target"},
                    )
                if max_loss_pips >= sl_pips:
                    exit_price = entry_price_snapshot + sl_pips * self.pip_size
                    self._reset_position()
                    return Signal(
                        symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                        strategy_name=self.name, timeframe=timeframe,
                        entry_price=exit_price,
                        metadata={"exit_reason": "hard_sl"},
                    )

            if self._bars_in_position >= self.time_stop_bars:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.5,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "time_stop"},
                )
            return None

        # ── Window tracking ──
        window = self._find_active_window(ts_ms)
        if window is None:
            self._active_window = None
            self._pre_window_price = None
            return None

        # New window started — capture pre-window price from prev bar
        if self._active_window is None or self._active_window != window:
            self._active_window = window
            if prev is not None:
                self._pre_window_price = float(prev["close"])
            else:
                self._pre_window_price = None
            return None

        if self._pre_window_price is None:
            return None

        # ── Entry: stop-entry at trigger level, not at bar close ──
        # On the FIRST bar where high/low crosses the trigger level, enter
        # at the trigger level itself. This is a stop-buy/stop-sell pattern:
        # the fade is armed at a fixed price, the order fills when that
        # price is touched. Entering at bar.close is wrong because an M5
        # bar whose high spiked through the trigger may already have
        # reverted below pre_window_price by the close — we'd be entering
        # a "fade" at a price that's already on the wrong side.
        trigger_up = self._pre_window_price + self.spike_trigger_pips * self.pip_size
        trigger_down = self._pre_window_price - self.spike_trigger_pips * self.pip_size

        if high >= trigger_up:
            # Bar touched the up-trigger — enter SHORT AT THE TRIGGER LEVEL
            entry_price = trigger_up
            spike_pips = self.spike_trigger_pips  # realized at trigger
            self._entry_price = entry_price
            self._entry_spike_pips = spike_pips
            self._spike_direction = 1
            self._bars_in_position = 0
            sl_price = entry_price + (spike_pips * self.sl_mult) * self.pip_size
            return Signal(
                symbol=symbol, action=SignalAction.SHORT, confidence=0.9,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=entry_price, stop_loss=sl_price,
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "spike_pips": spike_pips,
                    "pre_window_price": self._pre_window_price,
                    "label": window.label,
                },
            )

        if low <= trigger_down:
            entry_price = trigger_down
            spike_pips = self.spike_trigger_pips
            self._entry_price = entry_price
            self._entry_spike_pips = spike_pips
            self._spike_direction = -1
            self._bars_in_position = 0
            sl_price = entry_price - (spike_pips * self.sl_mult) * self.pip_size
            return Signal(
                symbol=symbol, action=SignalAction.LONG, confidence=0.9,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=entry_price, stop_loss=sl_price,
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "spike_pips": spike_pips,
                    "pre_window_price": self._pre_window_price,
                    "label": window.label,
                },
            )

        return None
