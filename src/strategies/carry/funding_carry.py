"""Perpetual Funding Rate Carry strategy (Strategy A — Phase 3b-3).

**What it does:** Harvests the structural funding rate that retail longs
pay to shorts on crypto perpetual futures. Trades a delta-neutral hedge
(`short BTCUSDT perp + long BTCUSDT spot`) and collects funding each 8h
epoch until a kill condition fires.

**v1 scope hack:** the hedged pair is represented as a single synthetic
OHLCV series (`BTCUSDT-CARRY`) built by `src/data/funding_synthetic.py`.
The synthetic close price drifts by `funding_rate − friction_pct` per 8h
epoch, which captures the economics of the hedged pair exactly without
needing multi-leg Binance Futures infrastructure in v1. Real Futures
execution is deferred to v2 after paper trading validates the edge.

**Strategy shape:** position-style, NOT signal-style. The strategy opens
LONG on the first synthetic bar, stays in the position indefinitely, and
closes only when a kill condition triggers:

    1. `flip_persistence_bars` consecutive negative-funding epochs → CLOSE
    2. Unrealized drawdown > `max_drawdown_kill_pct` → CLOSE
    3. Time-since-entry exceeds `max_hold_bars` (optional safety stop)

After CLOSE, the strategy re-enters LONG on the next bar if the kill
condition has cleared (funding back to positive). This implements the
"always carry unless stopped out" behavior.

**Integration with M3S:** the strategy emits normal `Signal` objects
through `BaseStrategy.on_features()`. `PortfolioTracker.on_trade_close`
records each CLOSE as a realized trade with pnl = funding earned − friction.
M3S allocator treats `funding_carry` as a single strategy bucket.

**Config (strategies.toml `[funding_carry]` section):**

    enabled = true
    risk_profile = "SAFE"
    markets = ["BTCUSDT-CARRY"]        # synthetic symbol
    timeframe = "8h"
    max_risk_per_trade = 0.01
    friction_pct = 0.00005             # 0.005% per 8h epoch, amortized (see funding_synthetic.py)
    flip_persistence_bars = 3          # 3 epochs = 24h of negative funding
    max_drawdown_kill_pct = 0.03       # 3% unrealized DD
    max_hold_bars = 0                  # 0 = no time stop
    cooldown_bars = 3                  # after kill, wait 3 epochs before re-entry
"""

from __future__ import annotations

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction


class FundingCarryStrategy(BaseStrategy):
    """Perpetual funding rate carry — always-long on synthetic carry asset."""
    fee_style = "position"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str = "8h",
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        friction_pct: float = 0.00005,
        flip_persistence_bars: int = 3,
        max_drawdown_kill_pct: float = 0.03,
        max_hold_bars: int = 0,
        cooldown_bars: int = 3,
    ):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)

        if friction_pct < 0:
            raise ValueError(f"friction_pct must be >= 0, got {friction_pct}")
        if flip_persistence_bars < 1:
            raise ValueError(f"flip_persistence_bars must be >= 1, got {flip_persistence_bars}")
        if max_drawdown_kill_pct <= 0 or max_drawdown_kill_pct >= 1:
            raise ValueError(
                f"max_drawdown_kill_pct must be in (0, 1), got {max_drawdown_kill_pct}"
            )
        if cooldown_bars < 0:
            raise ValueError(f"cooldown_bars must be >= 0, got {cooldown_bars}")

        self.friction_pct = float(friction_pct)
        self.flip_persistence_bars = int(flip_persistence_bars)
        self.max_drawdown_kill_pct = float(max_drawdown_kill_pct)
        self.max_hold_bars = int(max_hold_bars)
        self.cooldown_bars = int(cooldown_bars)

        # Position state
        self._entry_price: float = 0.0
        self._peak_price: float = 0.0
        self._bars_in_position: int = 0
        self._bars_since_exit: int = 999                  # start ready to enter
        self._negative_funding_streak: int = 0

    # ── Strategy contract ───────────────────────────────────────────

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        """Decide whether to enter, hold, or exit the carry position.

        Features expected (from the synthetic series):
            close            — current synthetic close price
            (optional) funding_rate — per-epoch funding for this bar

        The synthetic close already bakes in `funding_rate − friction` per
        epoch, so drawdown tracking is straightforward (peak-to-current).
        If the features include an explicit `funding_rate` column the
        strategy uses it for the flip-persistence gate; otherwise it
        infers the sign from the close-vs-previous change.
        """
        close = float(features.get("close", 0.0))
        if close <= 0:
            return None

        # Infer funding sign: prefer explicit `funding_rate` column if present,
        # else compare close to previous close (approximation).
        if "funding_rate" in features.index:
            funding_rate = float(features["funding_rate"])
        elif self._prev_features is not None:
            prev_close = float(self._prev_features.get("close", close))
            inferred_return = (close / prev_close) - 1.0 if prev_close > 0 else 0.0
            funding_rate = inferred_return + self.friction_pct
        else:
            funding_rate = 0.0

        # ── State rotation before any decision ────────────────────
        if self._position == "LONG":
            self._bars_in_position += 1
            if close > self._peak_price:
                self._peak_price = close
            if funding_rate < 0:
                self._negative_funding_streak += 1
            else:
                self._negative_funding_streak = 0
        else:
            self._bars_since_exit += 1

        # ── EXIT logic (check exits first) ────────────────────────
        if self._position == "LONG":
            if self._negative_funding_streak >= self.flip_persistence_bars:
                return self._exit_signal(symbol, close, "negative_funding")
            if self._peak_price > 0:
                dd = 1.0 - (close / self._peak_price)
                if dd >= self.max_drawdown_kill_pct:
                    return self._exit_signal(symbol, close, "drawdown_kill")
            if self.max_hold_bars > 0 and self._bars_in_position >= self.max_hold_bars:
                return self._exit_signal(symbol, close, "time_stop")
            return None  # hold

        # ── ENTRY logic ─────────────────────────────────────────────
        if self._position == "FLAT":
            if self._bars_since_exit < self.cooldown_bars:
                return None
            if funding_rate < 0:
                return None
            return self._entry_signal(symbol, close)

        return None

    # ── Signal builders ─────────────────────────────────────────────

    def _entry_signal(self, symbol: str, close: float) -> Signal:
        self._entry_price = close
        self._peak_price = close
        self._bars_in_position = 0
        self._negative_funding_streak = 0
        return Signal(
            symbol=symbol,
            action=SignalAction.LONG,
            confidence=1.0,
            strategy_name=self.name,
            timeframe=self.timeframe,
            entry_price=close,
            stop_loss=close * (1.0 - self.max_drawdown_kill_pct),
            take_profit=None,
            risk_pct=self.max_risk_per_trade,
            metadata={"entry_reason": "carry_open"},
        )

    def _exit_signal(self, symbol: str, close: float, reason: str) -> Signal:
        self._bars_since_exit = 0
        self._entry_price = 0.0
        self._peak_price = 0.0
        self._bars_in_position = 0
        self._negative_funding_streak = 0
        return Signal(
            symbol=symbol,
            action=SignalAction.CLOSE,
            confidence=1.0,
            strategy_name=self.name,
            timeframe=self.timeframe,
            entry_price=close,
            stop_loss=None,
            take_profit=None,
            risk_pct=None,
            metadata={"exit_reason": reason},
        )

    # ── Factory from TOML config ───────────────────────────────────

    @classmethod
    def from_config(cls, name: str) -> FundingCarryStrategy:
        cfg = get_config()
        section = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=section.get("markets", ["BTCUSDT-CARRY"]),
            timeframe=section.get("timeframe", "8h"),
            risk_profile=RiskProfile(section.get("risk_profile", "SAFE")),
            max_risk_per_trade=section.get("max_risk_per_trade", 0.01),
            friction_pct=section.get("friction_pct", 0.00005),
            flip_persistence_bars=section.get("flip_persistence_bars", 3),
            max_drawdown_kill_pct=section.get("max_drawdown_kill_pct", 0.03),
            max_hold_bars=section.get("max_hold_bars", 0),
            cooldown_bars=section.get("cooldown_bars", 3),
        )
