"""Liquidation Cascade Reversion strategy (Backlog #6).

**What it does:** Fades forced-liquidation cascades on BTCUSDT. When the
market cascades through a chain of margin stop-outs + liquidation-engine
fills, the terminal tick is an overshoot driven by momentary imbalance.
Price retraces as normal liquidity replenishes, typically within 3-30
minutes. We detect cascades via an extreme negative 1-minute return
z-score (proxy for actual liquidation events — they correlate by
construction) and go LONG into the overshoot.

**Why orthogonal to existing book:** event-driven, not bar-driven. Fires
~10×/month during chaos events when trend/momentum strategies hit risk-
reject gates. Target Sharpe 1.5-2.5 with episodic P&L (90% zero days +
10% high-return events). Acts as a drawdown hedge on the rest of the
book.

**Shape:** signal-style (entry → exit). Entry on cascade-tick close,
exit on SL / TP / 30-minute time stop. Stage 1 best-found config (from
6.5mo BTCUSDT 1m research): SL −200 bps, TP +100 bps, 30m horizon.
Wide SL is critical because cascades overshoot bidirectionally before
retracing; tight brackets get whipsawed.

**Feature dependencies:**
    close  — latest 1-minute close price

**Expected Sharpe:** 1.0-2.0 net-of-fees per Stage 1 research. Low-
complexity build; the only state is a rolling deque of 1m returns.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction


class LiquidationCascadeStrategy(BaseStrategy):
    """Fade BTCUSDT liquidation cascades via price-proxy detection.

    Decision table on each 1-minute bar:

        cooling down OR warming up        → HOLD
        position FLAT + cascade detected  → LONG (or SHORT if !long_only)
        position LONG + price <= SL       → CLOSE (exit_reason=sl)
        position LONG + price >= TP       → CLOSE (exit_reason=tp)
        position LONG + held max_hold     → CLOSE (exit_reason=time_stop)
        position SHORT (symmetric)        → same pattern

    Cascade detection uses a rolling deque of 1m returns (size
    `rolling_window_bars`). A cascade is the event where the current
    bar's return is both:
      - |z-score| > `cascade_sigma`, AND
      - magnitude > `cascade_min_move_bps`
    (both conditions required to avoid false positives when rolling std
    is tiny during quiet regimes).
    """
    fee_style = "scalping"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str = "1m",
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        cascade_sigma: float = 4.0,
        cascade_min_move_bps: float = 50.0,
        rolling_window_bars: int = 1440,    # 1 day of 1m bars
        sl_bps: float = 200.0,
        tp_bps: float = 100.0,
        max_hold_bars: int = 30,
        cooldown_bars: int = 60,            # 1 hour gap between trades
        long_only: bool = True,
    ):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)

        if cascade_sigma <= 0:
            raise ValueError(f"cascade_sigma must be > 0, got {cascade_sigma}")
        if cascade_min_move_bps <= 0:
            raise ValueError(f"cascade_min_move_bps must be > 0, got {cascade_min_move_bps}")
        if rolling_window_bars < 30:
            raise ValueError(f"rolling_window_bars too small: {rolling_window_bars}")
        if sl_bps <= 0:
            raise ValueError(f"sl_bps must be > 0, got {sl_bps}")
        if tp_bps <= 0:
            raise ValueError(f"tp_bps must be > 0, got {tp_bps}")
        if max_hold_bars < 1:
            raise ValueError(f"max_hold_bars must be >= 1, got {max_hold_bars}")
        if cooldown_bars < 0:
            raise ValueError(f"cooldown_bars must be >= 0, got {cooldown_bars}")

        self.cascade_sigma = float(cascade_sigma)
        self.cascade_min_move_bps = float(cascade_min_move_bps)
        self.rolling_window_bars = int(rolling_window_bars)
        self.sl_bps = float(sl_bps)
        self.tp_bps = float(tp_bps)
        self.max_hold_bars = int(max_hold_bars)
        self.cooldown_bars = int(cooldown_bars)
        self.long_only = bool(long_only)

        # Rolling buffer of observed 1-minute returns (decimal, e.g. -0.005 = -50 bps)
        self._return_buf: deque[float] = deque(maxlen=self.rolling_window_bars)

        # Position state
        self._entry_price: float = 0.0
        self._bars_in_position: int = 0
        self._bars_since_exit: int = cooldown_bars  # ready to enter from the start once warm

        # Previous bar's close — needed to compute 1-minute return inside the strategy
        self._prev_close: float = 0.0

    # ── Internal helpers ────────────────────────────────────────────

    def _rolling_std(self) -> float | None:
        """Sample std of the return buffer. Returns None during warmup."""
        n = len(self._return_buf)
        warmup_threshold = max(60, self.rolling_window_bars // 4)
        if n < warmup_threshold:
            return None
        mean = sum(self._return_buf) / n
        var = sum((r - mean) ** 2 for r in self._return_buf) / (n - 1)
        return var ** 0.5

    def _reset_position(self) -> None:
        self._entry_price = 0.0
        self._bars_in_position = 0
        self._bars_since_exit = 0

    # ── Strategy contract ───────────────────────────────────────────

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = float(features.get("close", 0.0))
        if close <= 0:
            return None

        # Compute 1-minute return from previous close (first bar has no return)
        if self._prev_close > 0:
            ret_1m = (close - self._prev_close) / self._prev_close
            self._return_buf.append(ret_1m)
        else:
            ret_1m = 0.0
        self._prev_close = close

        # Tick state counters
        if self._position == "FLAT":
            self._bars_since_exit += 1
        else:
            self._bars_in_position += 1

        # ── EXIT LOGIC (LONG) ──
        if self._position == "LONG":
            # SL check
            sl_level = self._entry_price * (1 - self.sl_bps / 10_000.0)
            if close <= sl_level:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE,
                    confidence=0.9, strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "sl", "entry": round(self._entry_price, 2)},
                )
            # TP check
            tp_level = self._entry_price * (1 + self.tp_bps / 10_000.0)
            if close >= tp_level:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE,
                    confidence=0.9, strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "tp", "entry": round(self._entry_price, 2)},
                )
            # Time stop
            if self._bars_in_position >= self.max_hold_bars:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE,
                    confidence=0.6, strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "time_stop", "entry": round(self._entry_price, 2)},
                )

        # ── EXIT LOGIC (SHORT) ──
        if self._position == "SHORT":
            sl_level = self._entry_price * (1 + self.sl_bps / 10_000.0)
            if close >= sl_level:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE,
                    confidence=0.9, strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "sl", "entry": round(self._entry_price, 2)},
                )
            tp_level = self._entry_price * (1 - self.tp_bps / 10_000.0)
            if close <= tp_level:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE,
                    confidence=0.9, strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "tp", "entry": round(self._entry_price, 2)},
                )
            if self._bars_in_position >= self.max_hold_bars:
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE,
                    confidence=0.6, strategy_name=self.name, timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "time_stop", "entry": round(self._entry_price, 2)},
                )

        # ── ENTRY LOGIC ──
        if self._position != "FLAT":
            return None

        if self._bars_since_exit < self.cooldown_bars:
            return None

        std = self._rolling_std()
        if std is None or std <= 0:
            return None  # warmup or degenerate distribution

        ret_bps = ret_1m * 10_000.0
        z = ret_bps / (std * 10_000.0)

        # LONG entry: extreme negative move → cascade → expect retracement up
        if (
            z <= -self.cascade_sigma
            and ret_bps <= -self.cascade_min_move_bps
        ):
            self._entry_price = close
            self._bars_in_position = 0
            sl_level = close * (1 - self.sl_bps / 10_000.0)
            tp_level = close * (1 + self.tp_bps / 10_000.0)
            return Signal(
                symbol=symbol, action=SignalAction.LONG,
                confidence=min(1.0, abs(z) / (self.cascade_sigma * 2)),
                strategy_name=self.name, timeframe=timeframe,
                entry_price=close,
                stop_loss=round(sl_level, 2),
                take_profit=round(tp_level, 2),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "entry_reason": "cascade_down",
                    "z_score": round(z, 2),
                    "ret_bps": round(ret_bps, 1),
                },
            )

        # SHORT entry: extreme positive move → short-squeeze → expect retracement down
        if (
            not self.long_only
            and z >= self.cascade_sigma
            and ret_bps >= self.cascade_min_move_bps
        ):
            self._entry_price = close
            self._bars_in_position = 0
            sl_level = close * (1 + self.sl_bps / 10_000.0)
            tp_level = close * (1 - self.tp_bps / 10_000.0)
            return Signal(
                symbol=symbol, action=SignalAction.SHORT,
                confidence=min(1.0, abs(z) / (self.cascade_sigma * 2)),
                strategy_name=self.name, timeframe=timeframe,
                entry_price=close,
                stop_loss=round(sl_level, 2),
                take_profit=round(tp_level, 2),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "entry_reason": "cascade_up",
                    "z_score": round(z, 2),
                    "ret_bps": round(ret_bps, 1),
                },
            )

        return None

    # ── Config loading ──────────────────────────────────────────────

    @classmethod
    def from_config(cls, name: str) -> "LiquidationCascadeStrategy":
        cfg = get_config()
        sc = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=sc.get("markets", ["BTCUSDT"]),
            timeframe=sc.get("timeframe", "1m"),
            risk_profile=RiskProfile(sc.get("risk_profile", "SAFE")),
            max_risk_per_trade=sc.get("max_risk_per_trade", 0.01),
            cascade_sigma=sc.get("cascade_sigma", 4.0),
            cascade_min_move_bps=sc.get("cascade_min_move_bps", 50.0),
            rolling_window_bars=sc.get("rolling_window_bars", 1440),
            sl_bps=sc.get("sl_bps", 200.0),
            tp_bps=sc.get("tp_bps", 100.0),
            max_hold_bars=sc.get("max_hold_bars", 30),
            cooldown_bars=sc.get("cooldown_bars", 60),
            long_only=sc.get("long_only", True),
        )
