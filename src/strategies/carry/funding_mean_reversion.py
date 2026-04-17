"""Funding-Rate Mean Reversion strategy (Backlog #8).

**What it does:** Fades extreme perpetual funding spikes. When the BTC
funding rate pushes into the top of its trailing distribution, retail
longs are crowded — expected to revert — so the strategy goes SHORT.
Symmetrically, when funding flips deep-negative (shorts crowded), it
goes LONG. Holds for a few 8h epochs and exits on reversion or stop.

**Why deferred:** until Session 22 the book had `funding_carry` (which
HARVESTS structural positive funding via a hedged carry position) but
no way to monetize the *reversion* of funding extremes — a different
edge on the same data feed. This strategy is intentionally orthogonal
to funding_carry:
    funding_carry   : always-long on hedged synthetic, harvest +funding
    funding_mr      : directional on real BTC perp, fade extremes

**Shape:** signal-style (entry → exit). Uses trailing quantiles of the
funding_rate series to determine "extreme" thresholds, so the strategy
is self-calibrating across regimes (2022 bear market, 2024 ETF bull,
etc.).

**Feature dependencies (from feed or CSV):**
    close           — real BTC perp price (not the synthetic carry close)
    high, low       — for ATR stop
    ATR_14          — precomputed ATR
    funding_rate    — current epoch's funding rate (decimal, 8h)

**Expected Sharpe:** 1-2 per the research memo. Low-complexity build.
Data requirement already satisfied by data/historical/funding/.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction


class FundingMeanReversionStrategy(BaseStrategy):
    """Fade extreme BTC funding spikes.

    Decision table on each bar:

        funding_rate >= q_high  →  OPEN SHORT (expect reversion down)
        funding_rate <= q_low   →  OPEN LONG  (expect reversion up)
        funding_rate ∈ normal   →  HOLD
        in SHORT + funding back ≤ q_mid  →  CLOSE
        in LONG  + funding back ≥ q_mid  →  CLOSE
        hold bars ≥ max_hold_bars        →  CLOSE (time stop)
        price stop hit                   →  CLOSE (ATR stop)

    Quantiles (`q_high`, `q_low`, `q_mid`) are computed over a rolling
    window of the last `quantile_window` funding observations. If the
    strategy hasn't seen enough history yet, it waits.
    """
    fee_style = "intraday"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str = "8h",
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        quantile_window: int = 270,       # ~90 days of 8h epochs
        high_quantile: float = 0.95,
        low_quantile: float = 0.05,
        mid_quantile: float = 0.50,
        atr_sl_mult: float = 2.5,
        atr_period: int = 14,
        max_hold_bars: int = 6,           # ≤ 48h
        cooldown_bars: int = 2,
        long_only: bool = False,
    ):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)

        if not (0.0 < low_quantile < mid_quantile < high_quantile < 1.0):
            raise ValueError(
                f"quantiles must satisfy 0 < low < mid < high < 1, got "
                f"{low_quantile}/{mid_quantile}/{high_quantile}"
            )
        if quantile_window < 30:
            raise ValueError(f"quantile_window too small: {quantile_window}")
        if atr_sl_mult <= 0:
            raise ValueError(f"atr_sl_mult must be > 0, got {atr_sl_mult}")
        if max_hold_bars < 1:
            raise ValueError(f"max_hold_bars must be >= 1, got {max_hold_bars}")
        if cooldown_bars < 0:
            raise ValueError(f"cooldown_bars must be >= 0, got {cooldown_bars}")

        self.quantile_window = int(quantile_window)
        self.high_quantile = float(high_quantile)
        self.low_quantile = float(low_quantile)
        self.mid_quantile = float(mid_quantile)
        self.atr_sl_mult = float(atr_sl_mult)
        self.atr_period = int(atr_period)
        self.max_hold_bars = int(max_hold_bars)
        self.cooldown_bars = int(cooldown_bars)
        self.long_only = bool(long_only)

        # Rolling buffer of observed funding rates
        self._funding_buf: deque[float] = deque(maxlen=self.quantile_window)

        # Position state (beyond BaseStrategy's FLAT/LONG/SHORT string)
        self._entry_price: float = 0.0
        self._entry_stop: float = 0.0
        self._bars_in_position: int = 0
        self._bars_since_exit: int = 999  # ready to enter from the start

        self._atr_col = f"ATR_{self.atr_period}"

    # ── Internal helpers ────────────────────────────────────────────

    def _quantile(self, q: float) -> float | None:
        """Compute rolling quantile from the funding buffer."""
        n = len(self._funding_buf)
        if n < max(30, self.quantile_window // 3):
            return None
        # Pure-Python nearest-rank quantile — avoids numpy dep + deterministic
        sorted_vals = sorted(self._funding_buf)
        idx = int(max(0, min(n - 1, round(q * (n - 1)))))
        return float(sorted_vals[idx])

    def _reset_position(self) -> None:
        self._entry_price = 0.0
        self._entry_stop = 0.0
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

        funding = features.get("funding_rate")
        if funding is None or pd.isna(funding):
            return None
        funding = float(funding)

        # Update rolling buffer BEFORE computing quantiles
        # (so the current bar is included in the distribution).
        self._funding_buf.append(funding)

        atr = features.get(self._atr_col)
        atr_val: float | None
        if atr is None or pd.isna(atr):
            atr_val = None
        else:
            atr_val = float(atr)

        # ── Tick state counters ──
        if self._position == "FLAT":
            self._bars_since_exit += 1
        else:
            self._bars_in_position += 1

        # ── EXIT LOGIC (LONG) ──
        if self._position == "LONG":
            mid = self._quantile(self.mid_quantile)

            # Price stop hit
            if self._entry_stop > 0 and close <= self._entry_stop:
                self._reset_position()
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "atr_stop", "funding": round(funding, 6)},
                )

            # Reversion complete
            if mid is not None and funding >= mid:
                self._reset_position()
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.8,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "reversion", "funding": round(funding, 6)},
                )

            # Time stop
            if self._bars_in_position >= self.max_hold_bars:
                self._reset_position()
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.6,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "time_stop", "funding": round(funding, 6)},
                )

        # ── EXIT LOGIC (SHORT) ──
        if self._position == "SHORT":
            mid = self._quantile(self.mid_quantile)

            if self._entry_stop > 0 and close >= self._entry_stop:
                self._reset_position()
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.9,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "atr_stop", "funding": round(funding, 6)},
                )

            if mid is not None and funding <= mid:
                self._reset_position()
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.8,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "reversion", "funding": round(funding, 6)},
                )

            if self._bars_in_position >= self.max_hold_bars:
                self._reset_position()
                return Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    confidence=0.6,
                    strategy_name=self.name,
                    timeframe=timeframe,
                    entry_price=close,
                    metadata={"exit_reason": "time_stop", "funding": round(funding, 6)},
                )

        # ── ENTRY LOGIC ──
        if self._position != "FLAT":
            return None

        if self._bars_since_exit < self.cooldown_bars:
            return None

        q_high = self._quantile(self.high_quantile)
        q_low = self._quantile(self.low_quantile)
        if q_high is None or q_low is None:
            return None  # warming up

        # Degenerate distribution — e.g., constant funding over the warmup
        # window — produces q_high == q_low and any new value "meets" the
        # threshold trivially. Skip until real dispersion appears.
        if q_high <= q_low:
            return None

        # Need ATR for the stop — skip if missing
        if atr_val is None or atr_val <= 0:
            return None

        # SHORT entry: funding is extreme-high, fade (unless long_only)
        if funding >= q_high and not self.long_only:
            self._entry_price = close
            self._entry_stop = close + self.atr_sl_mult * atr_val
            self._bars_in_position = 0
            # Distance-above-threshold as proxy for conviction
            span = max(q_high - q_low, 1e-9)
            conviction = min(1.0, (funding - q_high) / span + 0.5)
            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=max(0.5, conviction),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(self._entry_stop, 4),
                take_profit=None,    # no hard target; exit on funding reversion
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "entry_reason": "funding_top",
                    "funding": round(funding, 6),
                    "q_high": round(q_high, 6),
                    "atr": round(atr_val, 4),
                },
            )

        # LONG entry: funding is extreme-low, fade
        if funding <= q_low and not self.long_only:
            self._entry_price = close
            self._entry_stop = close - self.atr_sl_mult * atr_val
            self._bars_in_position = 0
            span = max(q_high - q_low, 1e-9)
            conviction = min(1.0, (q_low - funding) / span + 0.5)
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=max(0.5, conviction),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(self._entry_stop, 4),
                take_profit=None,
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "entry_reason": "funding_bottom",
                    "funding": round(funding, 6),
                    "q_low": round(q_low, 6),
                    "atr": round(atr_val, 4),
                },
            )

        # The LONG-only mode still wants to enter on low-funding signal
        if funding <= q_low and self.long_only:
            self._entry_price = close
            self._entry_stop = close - self.atr_sl_mult * atr_val
            self._bars_in_position = 0
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=0.7,
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(self._entry_stop, 4),
                risk_pct=self.max_risk_per_trade,
                metadata={
                    "entry_reason": "funding_bottom_long_only",
                    "funding": round(funding, 6),
                    "q_low": round(q_low, 6),
                },
            )

        return None

    # ── Config loading ──────────────────────────────────────────────

    @classmethod
    def from_config(cls, name: str) -> "FundingMeanReversionStrategy":
        cfg = get_config()
        sc = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=sc.get("markets", ["BTCUSDT"]),
            timeframe=sc.get("timeframe", "8h"),
            risk_profile=RiskProfile(sc.get("risk_profile", "SAFE")),
            max_risk_per_trade=sc.get("max_risk_per_trade", 0.01),
            quantile_window=sc.get("quantile_window", 270),
            high_quantile=sc.get("high_quantile", 0.95),
            low_quantile=sc.get("low_quantile", 0.05),
            mid_quantile=sc.get("mid_quantile", 0.50),
            atr_sl_mult=sc.get("atr_sl_mult", 2.5),
            atr_period=sc.get("atr_period", 14),
            max_hold_bars=sc.get("max_hold_bars", 6),
            cooldown_bars=sc.get("cooldown_bars", 2),
            long_only=sc.get("long_only", False),
        )
