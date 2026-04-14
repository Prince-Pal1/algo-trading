"""News spike fade strategy for gold.

STATUS: NOT ALPHA-READY (2026-04-14 — M1 peak-reversal revival also failed).

History:
1. 2026-04-13 M5 kill (task #92): stop-entry at trigger crossing
   was still in the continuation phase, before peak, and got
   stopped out. Stage 1 research had confirmed the fade premise
   (95.7% of >=30 pip XAUUSD news spikes retrace >= 50% within
   30 min) but bar-level execution couldn't capture it.
2. 2026-04-14 M1 revival with `require_peak_reversal=True`
   (task #100, after G.5b unlocked tick-adjacent M1 resolution):
   all 7 grid configs lost -21% to -47% over 12mo of 350k M1
   bars, with 53-78 trades per config. The peak-reversal pattern
   fails because: gold M1 noise is ~2-5 pips per minute, so the
   "close drops reversal_pips below peak_high" trigger fires on
   normal mean-reversion inside news windows, not on genuine
   peak stalls. Entering on a "stall bar" that's really just
   normal M1 noise captures the fade of a still-extending spike.

Root cause: the 95.7% retracement statistic measures retracement
from PEAK, but the strategy (even with peak_reversal) enters on
the first bar where a micro-reversal pattern appears, which is
usually still before the true peak. At 500× leverage even a
10-pip continuation wipes the trade. Needs either:
- A much longer "stall confirmation" window (e.g., 3+ bars of
  no new high), OR
- Volume-confirmation that the spike is exhausted, OR
- Real tick-level entry with a Δprice/Δt velocity threshold

KILL permanently. Revisit as a new strategy with a volume or
tick-velocity-based stall detection, not as a bar pattern.

Design reference (kept alive for the unit tests; do not tune):
- Track the pre-window price at the moment a news window starts
- On each bar inside the window, measure the cumulative excursion
  (max_high vs pre_price for up-side, min_low vs pre_price for down)
- When high >= pre + spike_trigger_pips:
  * Classic mode (require_peak_reversal=False): enter SHORT AT the
    trigger level via stop-entry (legacy M5 behavior)
  * Peak-reversal mode (require_peak_reversal=True): wait until the
    current bar's close is below the prior bar's high by
    `reversal_pips` or more, then enter SHORT at the current close.
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
        require_peak_reversal: bool = False,
        reversal_pips: float = 3.0,
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
        self.require_peak_reversal = require_peak_reversal
        self.reversal_pips = reversal_pips

        self._pre_window_price: float | None = None
        self._active_window: NewsWindow | None = None
        self._entry_price: float = 0.0
        self._entry_spike_pips: float = 0.0
        self._spike_direction: int = 0
        self._bars_in_position: int = 0
        # Peak-reversal tracking: once cumulative spike exceeds trigger,
        # remember the peak high/low and wait for a reversal confirmation
        # bar before entering the fade.
        self._spike_armed_dir: int = 0  # 0 = no spike armed, 1 = up, -1 = down
        self._peak_high: float = 0.0
        self._peak_low: float = 0.0

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
        self._spike_armed_dir = 0
        self._peak_high = 0.0
        self._peak_low = 0.0

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
            # Clear peak tracker on window transition
            self._spike_armed_dir = 0
            self._peak_high = 0.0
            self._peak_low = 0.0
            return None

        if self._pre_window_price is None:
            return None

        # ── Entry ──
        # Two modes:
        # 1. Classic stop-entry at trigger (require_peak_reversal=False):
        #    legacy M5 behavior — enter at the trigger price on first cross
        # 2. Peak-reversal pattern (require_peak_reversal=True):
        #    wait for spike to "stall" via a reversal bar, then enter at
        #    its close. Cleaner on M1 cadence where the stall bar is
        #    1 minute after the peak, well inside the retracement window.
        trigger_up = self._pre_window_price + self.spike_trigger_pips * self.pip_size
        trigger_down = self._pre_window_price - self.spike_trigger_pips * self.pip_size

        if self.require_peak_reversal:
            return self._peak_reversal_entry(
                symbol=symbol, timeframe=timeframe,
                close=close, high=high, low=low,
                trigger_up=trigger_up, trigger_down=trigger_down,
                window=window, prev=prev,
            )

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

    def _peak_reversal_entry(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        high: float,
        low: float,
        trigger_up: float,
        trigger_down: float,
        window: NewsWindow,
        prev: pd.Series | None,
    ) -> Signal | None:
        """Peak-reversal entry: wait for the spike to stall, then fade.

        State machine:
          UNARMED → cumulative excursion crosses trigger → ARMED (up or down)
          ARMED up → track peak_high → on first bar where close drops
            reversal_pips below peak_high, enter SHORT at close
          ARMED down → track peak_low → on first bar where close rises
            reversal_pips above peak_low, enter LONG at close
        """
        assert self._pre_window_price is not None
        pre = self._pre_window_price

        # Arm the state machine on first trigger crossing
        if self._spike_armed_dir == 0:
            if high >= trigger_up:
                self._spike_armed_dir = 1
                self._peak_high = high
                return None
            if low <= trigger_down:
                self._spike_armed_dir = -1
                self._peak_low = low
                return None
            return None

        # State machine active — track peak + look for reversal
        if self._spike_armed_dir == 1:
            # Extend peak if this bar made a new high
            if high > self._peak_high:
                self._peak_high = high
                return None
            # Reversal: close must have dropped reversal_pips below peak
            reversal_threshold = self._peak_high - self.reversal_pips * self.pip_size
            if close <= reversal_threshold:
                entry_price = close
                spike_pips = (self._peak_high - pre) / self.pip_size
                self._entry_price = entry_price
                self._entry_spike_pips = spike_pips
                self._spike_direction = 1
                self._bars_in_position = 0
                sl_price = self._peak_high + (spike_pips * (self.sl_mult - 1.0)) * self.pip_size
                return Signal(
                    symbol=symbol, action=SignalAction.SHORT, confidence=0.9,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=entry_price, stop_loss=sl_price,
                    risk_pct=self.max_risk_per_trade,
                    metadata={
                        "spike_pips": spike_pips,
                        "pre_window_price": pre,
                        "peak_high": self._peak_high,
                        "entry_mode": "peak_reversal",
                        "label": window.label,
                    },
                )
            return None

        if self._spike_armed_dir == -1:
            if low < self._peak_low:
                self._peak_low = low
                return None
            reversal_threshold = self._peak_low + self.reversal_pips * self.pip_size
            if close >= reversal_threshold:
                entry_price = close
                spike_pips = (pre - self._peak_low) / self.pip_size
                self._entry_price = entry_price
                self._entry_spike_pips = spike_pips
                self._spike_direction = -1
                self._bars_in_position = 0
                sl_price = self._peak_low - (spike_pips * (self.sl_mult - 1.0)) * self.pip_size
                return Signal(
                    symbol=symbol, action=SignalAction.LONG, confidence=0.9,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=entry_price, stop_loss=sl_price,
                    risk_pct=self.max_risk_per_trade,
                    metadata={
                        "spike_pips": spike_pips,
                        "pre_window_price": pre,
                        "peak_low": self._peak_low,
                        "entry_mode": "peak_reversal",
                        "label": window.label,
                    },
                )
            return None

        return None
