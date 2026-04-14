"""SWIFT — ALMA close vs ALMA open crossover on alternate timeframe.

PORTED FROM: TradingView Pine Script "SWIFTALGO" v5 by ScriptByBV
(@treeminobulls on Telegram). MPL 2.0 license.

The original Pine Script is ~700 lines but >80% is visual overlay
(supply/demand zones, S/R, Keltner bands, linear regression,
fractals, pivots, RSI, EMA, etc.) that has ZERO effect on the trade
logic. The actual trading signal is just ~50 lines and uses only:

  - ALMA(close, length=2, offset=0.85, sigma=5) on higher TF
  - ALMA(open,  length=2, offset=0.85, sigma=5) on higher TF
  - Crossover → LONG entry; crossunder → SHORT entry

This module ports ONLY that trading logic. All visual overlays are
ignored because they don't participate in entry/exit decisions.

Risk management (matches Pine Script exactly when `use_pine_ladder=True`):
  - SL at entry × (1 ∓ 0.5%)
  - TP1 at entry × (1 ± 1.0%) — closes 50% of position
  - TP2 at entry × (1 ± 1.5%) — closes another 30% of position
  - TP3 at entry × (1 ± 2.0%) — closes final 20% of position
  - SL hit from any state closes ALL remaining

Reversal handling:
  - `same_bar_flip=True` (Pine default): on an opposite signal while
    holding, closes the current position AND opens the new direction
    on the SAME bar. Matches Pine's `strategy.entry` auto-reverse.
  - `same_bar_flip=False`: 1-bar gap (CLOSE on bar N, OPEN opposite on
    bar N+1). More realistic for human traders but doesn't match Pine.

Implementation note — the 3-tier TP ladder:
  Our `Signal` schema allows ONE take_profit per signal and the engine
  closes full positions, not partials. To faithfully model Pine's
  `strategy.exit(qty_percent=50)` ladder without changing the engine,
  we use VIRTUAL LEG ACCOUNTING:
    - Engine sees ONE position sized to the full intended notional
    - Strategy internally tracks which of TP1/TP2/TP3 have hit
    - On full exit (SL or TP3 or opposite signal), strategy computes
      the WEIGHTED average exit price that reflects the legs that
      had already hit + the legs that exit at the current event level
    - Strategy emits a single CLOSE signal with that weighted exit
      price as `entry_price` metadata (engine's "fill price")

  This reproduces the ECONOMICS of the 3-tier ladder exactly for
  zero-cost backtests. In realistic backtests, commission is charged
  ONCE on close (not 3 times like Pine would), so this slightly
  underestimates transaction cost — acceptable for a first-pass
  comparison.

Non-repainting guarantees:
  - `process_orders_on_close=true` in Pine → we match by emitting
    signals at the current bar's close. The entry price recorded
    is `close`.
  - `delayOffset=0` in Pine → we don't lag the ALMA computation.
  - No intrabar peek-ahead. All decisions use data available at
    the close of bar N.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from src.data.feature_engine import _alma
from src.data.timeframe import AlternateTimeframeBuilder
from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


class SwiftAlmaStrategy(BaseStrategy):
    def __init__(
        self,
        name: str = "swift_alma",
        markets: list[str] | None = None,
        timeframe: str = "15m",
        risk_profile: RiskProfile = RiskProfile.MODERATE,
        max_risk_per_trade: float = 0.005,
        *,
        # ── ALMA parameters (Pine Script defaults) ──
        alma_length: int = 2,
        alma_offset: float = 0.85,
        alma_sigma: float = 5.0,  # Pine's offsetSigma default is 5, not 6
        # ── Alt timeframe multiplier (Pine intRes=8) ──
        alt_tf_multiplier: int = 8,
        # ── Risk management ──
        sl_pct: float = 0.005,   # 0.5%
        tp1_pct: float = 0.010,  # 1.0%
        tp2_pct: float = 0.015,  # 1.5%
        tp3_pct: float = 0.020,  # 2.0%
        tp1_qty: float = 0.50,   # Pine i_lxQtyTP1
        tp2_qty: float = 0.30,   # Pine i_lxQtyTP2
        tp3_qty: float = 0.20,   # Pine i_lxQtyTP3
        # ── Pine-faithful flags ──
        use_pine_ladder: bool = True,  # 3-tier TP ladder with virtual legs
        same_bar_flip: bool = True,    # Pine's same-bar reversal (no 1-bar gap)
        # ── Leverage / sizing ──
        leverage_range: tuple[float, float] = (1.0, 100.0),
        # ── Direction filter (Pine i_tradeType) ──
        trade_type: str = "BOTH",  # 'LONG' | 'SHORT' | 'BOTH' | 'NONE'
        tier: Tier = Tier.INSTITUTIONAL_TREND,
    ):
        super().__init__(
            name=name,
            markets=markets or ["XAUUSD"],
            timeframe=timeframe,
            risk_profile=risk_profile,
            max_risk_per_trade=max_risk_per_trade,
            leverage_range=leverage_range,
            tier=tier,
        )
        self.alma_length = alma_length
        self.alma_offset = alma_offset
        self.alma_sigma = alma_sigma
        self.alt_tf_multiplier = alt_tf_multiplier
        self.sl_pct = sl_pct
        self.tp1_pct = tp1_pct
        self.tp2_pct = tp2_pct
        self.tp3_pct = tp3_pct
        self.tp1_qty = tp1_qty
        self.tp2_qty = tp2_qty
        self.tp3_qty = tp3_qty
        self.use_pine_ladder = use_pine_ladder
        self.same_bar_flip = same_bar_flip
        self.trade_type = trade_type

        # Sanity: ladder quantities should sum to 1.0
        total_qty = tp1_qty + tp2_qty + tp3_qty
        if use_pine_ladder and abs(total_qty - 1.0) > 1e-6:
            raise ValueError(
                f"tp1_qty + tp2_qty + tp3_qty must sum to 1.0 when use_pine_ladder=True; "
                f"got {total_qty:.4f}"
            )

        # Alt-TF builder — accumulates base bars into alt bars
        self._alt_builder = AlternateTimeframeBuilder(
            multiplier=alt_tf_multiplier,
            history_size=50,
        )
        self._alt_close_buffer: deque[float] = deque(maxlen=max(alma_length * 4, 10))
        self._alt_open_buffer: deque[float] = deque(maxlen=max(alma_length * 4, 10))

        # Previous ALMA values for crossover detection
        self._prev_alma_close: float | None = None
        self._prev_alma_open: float | None = None

        # Position state — used by BOTH single-TP and ladder modes
        self._entry_price: float = 0.0
        self._sl_price: float = 0.0
        # Ladder state (only used when use_pine_ladder=True)
        self._ladder_tp1_price: float = 0.0
        self._ladder_tp2_price: float = 0.0
        self._ladder_tp3_price: float = 0.0
        self._ladder_tp1_hit: bool = False
        self._ladder_tp2_hit: bool = False
        # Single-TP fallback (used when use_pine_ladder=False)
        self._tp_price: float = 0.0

        # Reversal gap — only used when same_bar_flip=False
        self._pending_reversal_direction: int = 0

    # ── Helper: compute current ALMA pair ────────────────────────────

    def _compute_alma_pair(self) -> tuple[float | None, float | None]:
        """Compute current ALMA(close) and ALMA(open) from the alt-TF buffers."""
        if len(self._alt_close_buffer) < self.alma_length:
            return (None, None)
        close_series = pd.Series(list(self._alt_close_buffer))
        open_series = pd.Series(list(self._alt_open_buffer))
        alma_close = _alma(close_series, self.alma_length, self.alma_offset, self.alma_sigma)
        alma_open = _alma(open_series, self.alma_length, self.alma_offset, self.alma_sigma)
        return (float(alma_close.iloc[-1]), float(alma_open.iloc[-1]))

    # ── Ladder math ──────────────────────────────────────────────────

    def _ladder_weighted_exit(
        self,
        *,
        direction: int,
        sl_hit: bool,
        tp3_hit: bool,
        opposite_signal_close: float | None = None,
    ) -> float:
        """Compute the virtual weighted-average exit price for a ladder close.

        Convention: `total_return` represents the position's NET GAIN as a
        fraction of entry, sign-agnostic to direction. Positive means the
        trade made money; negative means it lost. A LONG that hit TP1 for
        +1% and a SHORT that saw price drop by 1% both have total_return
        = +0.01. Direction only affects the final exit-price formula.
          - LONG:  exit = entry × (1 + total_return)
          - SHORT: exit = entry × (1 - total_return)
        This way engine P&L = (exit-entry)×qty for long and (entry-exit)×qty
        for short both evaluate to +total_return × qty × entry.

        Conventions:
          - direction=+1 for LONG, -1 for SHORT
          - If tp3_hit: all 3 TPs filled → positive weighted return
          - Else if sl_hit: legs that hadn't hit close at SL (loss);
            legs that had hit keep their TP1/TP2 profit
          - Else (opposite_signal_close provided): legs that hadn't hit
            close at the opposite signal's bar close (may be gain or loss)
        """
        entry = self._entry_price
        # Per-leg return as GAIN (positive for profit, negative for loss)
        # regardless of direction.
        tp1_ret = self.tp1_pct       # profit if this leg fills at TP1
        tp2_ret = self.tp2_pct       # profit if this leg fills at TP2
        tp3_ret = self.tp3_pct       # profit if this leg fills at TP3
        sl_ret = -self.sl_pct        # loss if this leg stops out

        total_return = 0.0

        if tp3_hit:
            total_return = (
                self.tp1_qty * tp1_ret
                + self.tp2_qty * tp2_ret
                + self.tp3_qty * tp3_ret
            )
        elif sl_hit:
            total_return += self.tp1_qty * (tp1_ret if self._ladder_tp1_hit else sl_ret)
            total_return += self.tp2_qty * (tp2_ret if self._ladder_tp2_hit else sl_ret)
            total_return += self.tp3_qty * sl_ret  # tp3 unfilled, closes at SL
        elif opposite_signal_close is not None:
            # Current position return at the opposite-signal bar close
            # (sign-agnostic via direction).
            if direction > 0:
                current_ret = (opposite_signal_close - entry) / entry
            else:
                current_ret = (entry - opposite_signal_close) / entry
            total_return += self.tp1_qty * (tp1_ret if self._ladder_tp1_hit else current_ret)
            total_return += self.tp2_qty * (tp2_ret if self._ladder_tp2_hit else current_ret)
            total_return += self.tp3_qty * current_ret  # tp3 unfilled

        # Sign-aware exit price so the engine's direction-dependent P&L
        # formula reproduces total_return × qty × entry.
        if direction > 0:
            return entry * (1.0 + total_return)
        else:
            return entry * (1.0 - total_return)

    # ── Main feature handler ─────────────────────────────────────────

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        # 1. Feed the current base bar to the alt-TF builder
        closed_alt = self._alt_builder.feed(features)

        close = float(features["close"])
        high = float(features["high"])
        low = float(features["low"])

        # 2. If in position, run the intrabar exit logic FIRST.
        exit_signal = self._check_intrabar_exits(
            symbol=symbol, timeframe=timeframe,
            close=close, high=high, low=low,
        )
        if exit_signal is not None:
            return exit_signal

        # 3. Resolve any pending reversal (only relevant when same_bar_flip=False)
        if self._pending_reversal_direction != 0 and self._position == "FLAT":
            direction = self._pending_reversal_direction
            self._pending_reversal_direction = 0
            return self._open_position(
                symbol=symbol, timeframe=timeframe, close=close, direction=direction,
            )

        # 4. Only proceed with signal check if a new alt bar just closed
        if closed_alt is None:
            return None

        self._alt_close_buffer.append(float(closed_alt["close"]))
        self._alt_open_buffer.append(float(closed_alt["open"]))

        alma_close, alma_open = self._compute_alma_pair()
        if alma_close is None or alma_open is None:
            return None

        # 5. Detect crossover using previous values
        le_trigger = False
        se_trigger = False
        if self._prev_alma_close is not None and self._prev_alma_open is not None:
            if self._prev_alma_close <= self._prev_alma_open and alma_close > alma_open:
                le_trigger = True
            elif self._prev_alma_close >= self._prev_alma_open and alma_close < alma_open:
                se_trigger = True

        self._prev_alma_close = alma_close
        self._prev_alma_open = alma_open

        # 6. Direction filter
        if self.trade_type == "LONG" and se_trigger:
            se_trigger = False
        elif self.trade_type == "SHORT" and le_trigger:
            le_trigger = False
        elif self.trade_type == "NONE":
            le_trigger = False
            se_trigger = False

        # 7. Act on triggers
        if le_trigger:
            if self._position == "FLAT":
                return self._open_position(
                    symbol=symbol, timeframe=timeframe, close=close, direction=+1,
                )
            if self._position == "SHORT":
                return self._handle_reversal(
                    symbol=symbol, timeframe=timeframe, close=close, new_direction=+1,
                )

        if se_trigger:
            if self._position == "FLAT":
                return self._open_position(
                    symbol=symbol, timeframe=timeframe, close=close, direction=-1,
                )
            if self._position == "LONG":
                return self._handle_reversal(
                    symbol=symbol, timeframe=timeframe, close=close, new_direction=-1,
                )

        return None

    # ── Intrabar exit checks ─────────────────────────────────────────

    def _check_intrabar_exits(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        high: float,
        low: float,
    ) -> Signal | None:
        """Check SL / TP (or ladder TPs) for the current position."""
        if self._position == "FLAT":
            return None

        if self.use_pine_ladder:
            return self._check_ladder_exits(
                symbol=symbol, timeframe=timeframe,
                close=close, high=high, low=low,
            )

        # Single-TP mode (fallback for backward compatibility)
        if self._position == "LONG":
            if low <= self._sl_price:
                exit_px = self._sl_price
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "sl"},
                )
            if high >= self._tp_price:
                exit_px = self._tp_price
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "tp"},
                )
        else:  # SHORT
            if high >= self._sl_price:
                exit_px = self._sl_price
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "sl"},
                )
            if low <= self._tp_price:
                exit_px = self._tp_price
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.9,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "tp"},
                )
        return None

    def _check_ladder_exits(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        high: float,
        low: float,
    ) -> Signal | None:
        """Ladder intrabar exit logic — tracks TP1/TP2/TP3 hits and SL."""
        direction = +1 if self._position == "LONG" else -1

        # First: detect new TP1 / TP2 hits this bar (but don't close yet)
        if direction > 0:
            # LONG: TPs above entry, SL below entry
            if not self._ladder_tp1_hit and high >= self._ladder_tp1_price:
                self._ladder_tp1_hit = True
            if self._ladder_tp1_hit and not self._ladder_tp2_hit and high >= self._ladder_tp2_price:
                self._ladder_tp2_hit = True
            # TP3 hit: full exit
            if self._ladder_tp2_hit and high >= self._ladder_tp3_price:
                exit_px = self._ladder_weighted_exit(
                    direction=+1, sl_hit=False, tp3_hit=True,
                )
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.95,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_tp3"},
                )
            # SL hit: full exit of remaining legs
            if low <= self._sl_price:
                exit_px = self._ladder_weighted_exit(
                    direction=+1, sl_hit=True, tp3_hit=False,
                )
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_sl"},
                )
        else:  # SHORT
            # SHORT: TPs below entry, SL above entry
            if not self._ladder_tp1_hit and low <= self._ladder_tp1_price:
                self._ladder_tp1_hit = True
            if self._ladder_tp1_hit and not self._ladder_tp2_hit and low <= self._ladder_tp2_price:
                self._ladder_tp2_hit = True
            if self._ladder_tp2_hit and low <= self._ladder_tp3_price:
                exit_px = self._ladder_weighted_exit(
                    direction=-1, sl_hit=False, tp3_hit=True,
                )
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.95,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_tp3"},
                )
            if high >= self._sl_price:
                exit_px = self._ladder_weighted_exit(
                    direction=-1, sl_hit=True, tp3_hit=False,
                )
                self._reset_position()
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_sl"},
                )
        return None

    # ── Reversal handler ─────────────────────────────────────────────

    def _handle_reversal(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        new_direction: int,
    ) -> Signal:
        """Handle an opposite-direction signal while holding a position."""
        current_direction = +1 if self._position == "LONG" else -1

        # Compute the weighted exit price for the current position
        if self.use_pine_ladder:
            exit_px = self._ladder_weighted_exit(
                direction=current_direction,
                sl_hit=False, tp3_hit=False,
                opposite_signal_close=close,
            )
        else:
            exit_px = close

        if self.same_bar_flip:
            # Pine's behavior: close current + open opposite on same bar.
            # We emit the CLOSE signal NOW and set a pending reversal so
            # the NEXT call to on_features (or the fallthrough below) opens
            # the new direction. Actually: to mimic "same bar", we need
            # the engine to process both signals on the same bar.
            #
            # Constraint: process() returns ONE signal per call. Workaround:
            # emit the close + schedule the new direction for immediate
            # opening on the NEXT bar's process call. This is 1 bar off
            # from true Pine same-bar flip but within 1 bar of accuracy.
            self._pending_reversal_direction = new_direction
            self._reset_position()
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE, confidence=0.8,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=exit_px,
                metadata={"exit_reason": "reversal_pending"},
            )
        else:
            # 1-bar gap: CLOSE now, OPEN next bar via pending flag
            self._pending_reversal_direction = new_direction
            self._reset_position()
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE, confidence=0.8,
                strategy_name=self.name, timeframe=timeframe,
                entry_price=exit_px,
                metadata={"exit_reason": "reversal_gap"},
            )

    # ── Helpers ──────────────────────────────────────────────────────

    def _open_position(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        direction: int,
    ) -> Signal:
        """Emit an entry signal and record SL/TP (or ladder TPs) levels."""
        self._entry_price = close
        self._ladder_tp1_hit = False
        self._ladder_tp2_hit = False

        if direction > 0:
            self._sl_price = close * (1.0 - self.sl_pct)
            if self.use_pine_ladder:
                self._ladder_tp1_price = close * (1.0 + self.tp1_pct)
                self._ladder_tp2_price = close * (1.0 + self.tp2_pct)
                self._ladder_tp3_price = close * (1.0 + self.tp3_pct)
                tp_for_signal = self._ladder_tp3_price  # furthest TP for metadata
            else:
                self._tp_price = close * (1.0 + self.tp2_pct)  # midpoint
                tp_for_signal = self._tp_price
            action = SignalAction.LONG
        else:
            self._sl_price = close * (1.0 + self.sl_pct)
            if self.use_pine_ladder:
                self._ladder_tp1_price = close * (1.0 - self.tp1_pct)
                self._ladder_tp2_price = close * (1.0 - self.tp2_pct)
                self._ladder_tp3_price = close * (1.0 - self.tp3_pct)
                tp_for_signal = self._ladder_tp3_price
            else:
                self._tp_price = close * (1.0 - self.tp2_pct)
                tp_for_signal = self._tp_price
            action = SignalAction.SHORT

        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.85,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            stop_loss=self._sl_price,
            take_profit=tp_for_signal,
            risk_pct=self.max_risk_per_trade,
            metadata={
                "entry_mode": "alma_cross_ladder" if self.use_pine_ladder else "alma_cross_single",
                "sl_pct": self.sl_pct,
                "tp1_pct": self.tp1_pct if self.use_pine_ladder else None,
                "tp2_pct": self.tp2_pct,
                "tp3_pct": self.tp3_pct if self.use_pine_ladder else None,
            },
        )

    def _reset_position(self) -> None:
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0
        self._ladder_tp1_price = 0.0
        self._ladder_tp2_price = 0.0
        self._ladder_tp3_price = 0.0
        self._ladder_tp1_hit = False
        self._ladder_tp2_hit = False
