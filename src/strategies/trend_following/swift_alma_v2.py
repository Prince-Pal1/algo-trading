"""swift_alma_v2 — leverage-mode-aware ALMA crossover with regime + HTF filters.

Upgrade of `swift_alma` (task #103 Pine port) designed to:

1. Produce DISTINCT P&L under all 5 leverage modes (INVARIANT / MARGIN_CAPPED /
   VOL_TARGETED / RISK_SCALED / KELLY_FRACTIONAL) — the parent collapses 4 of
   5 modes into identical outcomes because `risk_pct == sl_pct` locks notional
   to equity. v2 decouples them and branches sizing internally per mode.

2. Survive chop regimes (parent fold 3 Q1 2025 = −7.89% / 17.40% DD) via an
   ADX-based regime filter + 4h EMA 50 higher-TF trend confirmation.

3. Beat the parent on real MT4 fees (parent: −12.47%) via (a) trade-frequency
   reduction through filters + cooldown and (b) ATR-scaled TP levels that
   adapt to current volatility instead of fixed 1%/1.5%/2%.

8 research-justified upgrades (see /Users/prince/.claude/plans/parallel-noodling-goblet.md
and docs/LEVERAGE_STRATEGY_DESIGN.md §6 for citations):

    1. `max_risk_per_trade` and `sl_pct` are INDEPENDENT kwargs. Default
       k = risk/sl = 2.0 (MARGIN_CAPPED profile). Setting risk_pct == sl_pct
       reproduces parent's INVARIANT profile.
    2. ADX regime filter: skip entries when `features["adx_14"] < min_adx`.
    3. 4h EMA 50 HTF trend filter: LONG requires close > htf_ema50; SHORT
       requires close < htf_ema50. 4h bars aggregated via the same
       AlternateTimeframeBuilder used for the ALMA alt-TF.
    4. Session filter: entries only during London 07:00-11:00 UTC OR NY
       13:30-16:30 UTC overlap (matches donchian_gold).
    5. Cooldown: `min_bars_between_trades` blocks re-entry for N bars after
       any exit (default 3).
    6. Mode-aware sizing: `_effective_risk_pct()` branches on `leverage_mode`
       kwarg. The deep_backtest framework injects the mode name at config
       time via `_apply_leverage_mode()`.
    7. ATR-scaled SL/TP: SL = 1×ATR, TP = 1.5×ATR by default (both
       configurable). Replaces the fixed percent levels.
    8. Vol-target layer: `vol_target` (annualized) + `vol_lookback` bars of
       realized vol. Used by the VOL_TARGETED mode branch; ignored by others.

Leverage mode sizing matrix (default kwargs: base=0.02, sl=0.01, vol_t=0.15):

    INVARIANT         → effective = sl_pct = 0.01     (notional = equity)
    MARGIN_CAPPED     → effective = max_risk = 0.02   (notional = 2× equity)
    VOL_TARGETED      → effective = 0.02 × clamp(vol_t/vol_r, 0.5, 2.0)
    RISK_SCALED       → effective = max_risk (framework already scaled it)
    KELLY_FRACTIONAL  → effective = max_risk (framework already Kelly-sized)

Result: 5 distinct effective risk_pct values → 5 distinct P&L outcomes in
the deep_backtest matrix. That's the whole point of v2.

Core design rule (docs/LEVERAGE_STRATEGY_DESIGN.md §1):
    notional = equity × (risk_pct / sl_pct)
    margin   = notional / leverage
Decoupling risk from sl gives us leverage meaningfully.

This module is NOT Pine-faithful. Parent `swift_alma` stays as-is for
TV parity. v2 is the "break from Pine to optimize for reality" version.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.data.feature_engine import _alma
from src.data.timeframe import AlternateTimeframeBuilder
from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal, SignalAction, Tier


# ── Session filter constants (matches donchian_gold) ─────────────────────

_LONDON_START_UTC = (7, 0)
_LONDON_END_UTC = (11, 0)
_NY_START_UTC = (13, 30)
_NY_END_UTC = (16, 30)


def _in_window(hm: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    h, m = hm
    sh, sm = start
    eh, em = end
    now = h * 60 + m
    return (sh * 60 + sm) <= now <= (eh * 60 + em)


def _is_in_session(ts_ms: int) -> bool:
    """True if the timestamp falls inside London 07:00-11:00 UTC or NY 13:30-16:30 UTC."""
    if ts_ms <= 0:
        return True  # no timestamp → don't gate
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hm = (dt.hour, dt.minute)
    return _in_window(hm, _LONDON_START_UTC, _LONDON_END_UTC) or _in_window(
        hm, _NY_START_UTC, _NY_END_UTC
    )


class SwiftAlmaV2Strategy(BaseStrategy):
    """Leverage-mode-aware ALMA crossover for gold.

    k-ratio documentation (docs/LEVERAGE_STRATEGY_DESIGN.md §4 Rule 2):
        k = max_risk_per_trade / sl_pct
        Default: 0.02 / 0.01 = 2.0 → notional = 2× equity (MARGIN_CAPPED profile)
        To reproduce parent v1's leverage-invariant behavior: pass
        max_risk_per_trade=sl_pct (k=1.0) or use leverage_mode="invariant".

    See module docstring for the full leverage mode sizing matrix.
    """

    # Indicators the deep_backtest pipeline should precompute for this
    # strategy. Read by `_resolve_indicators()` in deep_backtest.py.
    # NOTE: deep_backtest's indicator resolver accepts lowercase keys (it's
    # just a naming convention for which indicators to compute). The actual
    # column names the engine materializes in the features row are UPPERCASE
    # (`ATR_14`, `ADX_14`) — matches the vol_momentum.py pattern at
    # `src/strategies/momentum/vol_momentum.py:78, 126`.
    REQUIRED_INDICATORS: list[str] = ["atr_14", "adx_14"]

    # Feature column names the engine writes (UPPERCASE per the conventions
    # used by the feature_engine). These are the keys we read in on_features().
    _ATR_COL: str = "ATR_14"
    _ADX_COL: str = "ADX_14"

    def __init__(
        self,
        name: str = "swift_alma_v2",
        markets: list[str] | None = None,
        timeframe: str = "1h",
        risk_profile: RiskProfile = RiskProfile.MODERATE,
        max_risk_per_trade: float = 0.02,  # 2% — decoupled from sl_pct
        *,
        # ── Leverage mode (injected by _apply_leverage_mode) ──
        leverage_mode: str | None = None,
        # ── ALMA parameters ──
        alma_length: int = 2,
        alma_offset: float = 0.85,
        alma_sigma: float = 5.0,
        alt_tf_multiplier: int = 4,  # 1h base → 4h alt (also serves HTF EMA)
        # ── Risk management (DECOUPLED) ──
        sl_pct: float = 0.01,  # 1% — less than max_risk, so k=2
        use_atr_ladder: bool = True,  # True → ATR-scaled SL/TP; False → percent-based
        atr_sl_mult: float = 1.0,
        atr_tp_mult: float = 1.5,  # Single-TP mode only (when use_pine_ladder=False)
        # ── 3-tier virtual-leg TP ladder (task #132, ported from parent v1) ──
        use_pine_ladder: bool = False,  # OFF by default — MVP keeps single-TP
        atr_tp1_mult: float = 2.0,   # R-ratio for TP1 (50% qty)
        atr_tp2_mult: float = 3.0,   # R-ratio for TP2 (30% qty)
        atr_tp3_mult: float = 4.0,   # R-ratio for TP3 (20% qty)
        tp1_qty: float = 0.50,       # ladder leg weights (must sum to 1.0)
        tp2_qty: float = 0.30,
        tp3_qty: float = 0.20,
        # ── Same-bar reversal (task #132, ported from parent v1) ──
        same_bar_flip: bool = False,  # OFF by default — v2 MVP waits for SL/TP
        # ── Regime filter (upgrade #2) ──
        min_adx: float = 22.0,
        # ── HTF trend filter (upgrade #3) ──
        htf_ema_period: int = 50,
        require_htf_trend: bool = True,
        # ── Session filter (upgrade #4) ──
        session_filter: bool = True,
        # ── Cooldown (upgrade #5) ──
        min_bars_between_trades: int = 3,
        # ── Vol-target layer (upgrade #8) ──
        vol_target: float = 0.15,
        vol_lookback: int = 60,
        vol_annualization_hours: float = 8760.0,
        # ── Leverage / sizing ──
        leverage_range: tuple[float, float] = (1.0, 100.0),
        # ── Direction filter ──
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
        # Mode-aware kwarg — framework injects it via _apply_leverage_mode()
        self.leverage_mode = leverage_mode

        # ALMA
        self.alma_length = alma_length
        self.alma_offset = alma_offset
        self.alma_sigma = alma_sigma
        self.alt_tf_multiplier = alt_tf_multiplier

        # Risk / exits
        self.sl_pct = sl_pct
        self.use_atr_ladder = use_atr_ladder
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

        # Ladder + reversal (task #132)
        self.use_pine_ladder = use_pine_ladder
        self.atr_tp1_mult = atr_tp1_mult
        self.atr_tp2_mult = atr_tp2_mult
        self.atr_tp3_mult = atr_tp3_mult
        self.tp1_qty = tp1_qty
        self.tp2_qty = tp2_qty
        self.tp3_qty = tp3_qty
        self.same_bar_flip = same_bar_flip

        # Sanity: ladder quantities must sum to 1.0 when ladder is enabled
        if use_pine_ladder:
            total_qty = tp1_qty + tp2_qty + tp3_qty
            if abs(total_qty - 1.0) > 1e-6:
                raise ValueError(
                    f"tp1_qty + tp2_qty + tp3_qty must sum to 1.0 when "
                    f"use_pine_ladder=True; got {total_qty:.4f}"
                )

        # Filters
        self.min_adx = min_adx
        self.htf_ema_period = htf_ema_period
        self.require_htf_trend = require_htf_trend
        self.session_filter = session_filter
        self.min_bars_between_trades = min_bars_between_trades

        # Vol-target
        self.vol_target = vol_target
        self.vol_lookback = vol_lookback
        self.vol_annualization_hours = vol_annualization_hours

        self.trade_type = trade_type

        # ── Alt-TF builder (used by BOTH ALMA crossover and 4h HTF EMA) ──
        self._alt_builder = AlternateTimeframeBuilder(
            multiplier=alt_tf_multiplier,
            history_size=max(alma_length * 4, htf_ema_period * 2, 60),
        )
        # Buffers for ALMA computation (deque-bounded to avoid unbounded growth)
        buflen = max(alma_length * 4, 20)
        self._alt_close_buffer: deque[float] = deque(maxlen=buflen)
        self._alt_open_buffer: deque[float] = deque(maxlen=buflen)

        # Buffer for HTF EMA (needs htf_ema_period + margin)
        self._htf_ema_buffer: deque[float] = deque(maxlen=htf_ema_period * 3)
        self._htf_ema_value: float | None = None  # rolling EMA value

        # Buffer for realized vol computation (base-TF close prices)
        self._vol_close_buffer: deque[float] = deque(maxlen=vol_lookback + 2)

        # ALMA crossover state (previous alt-TF values)
        self._prev_alma_close: float | None = None
        self._prev_alma_open: float | None = None

        # Position state — supports BOTH single-TP and 3-tier ladder (task #132)
        self._entry_price: float = 0.0
        self._sl_price: float = 0.0
        self._tp_price: float = 0.0  # single-TP mode
        self._entry_direction: int = 0  # +1 LONG / -1 SHORT

        # Ladder state (only used when use_pine_ladder=True)
        self._ladder_tp1_price: float = 0.0
        self._ladder_tp2_price: float = 0.0
        self._ladder_tp3_price: float = 0.0
        self._ladder_tp1_hit: bool = False
        self._ladder_tp2_hit: bool = False

        # Same-bar reversal state (only used when same_bar_flip=True)
        # When a reversal signal fires while in position, we emit CLOSE NOW
        # and set this flag to +1/−1 so the NEXT call to on_features opens
        # the opposite direction. 1 bar off from Pine's exact same-bar flip,
        # but within 1 bar of accuracy — same as parent v1.
        self._pending_reversal_direction: int = 0
        # Stash for the params we need to re-compute when the reversal opens
        # on the next bar (filter re-evaluation would be wasted work).
        self._pending_reversal_sl_distance: float = 0.0
        self._pending_reversal_tp_distance: float = 0.0
        self._pending_reversal_atr_val: float = 0.0

        # Cooldown counter — bars since last exit
        self._bars_since_exit: int = 10_000  # initialized high so first signal isn't blocked

    # ── Sizing — the mode-aware core ─────────────────────────────────

    def _effective_risk_pct(self, realized_vol: float | None) -> float:
        """Mode-aware risk_pct branching. See docstring matrix at top of file.

        Returns the fraction of equity to risk on this signal. The emitted
        `Signal.risk_pct` is this value; the engine computes
        `quantity = (equity × risk_pct) / stop_distance` from it.

        Five distinct branches → five distinct P&L outcomes, which is the
        point of v2.
        """
        mode = (self.leverage_mode or "margin_capped").lower()

        if mode == "invariant":
            # Parent's baked-in contract: risk_pct == sl_pct → notional = equity.
            # The framework leverages become pure margin gates (no sizing change).
            return self.sl_pct

        if mode == "margin_capped":
            # Decoupled sizing: risk_pct > sl_pct, notional = k × equity.
            # Position size = (equity × max_risk) / (sl_distance × price).
            return self.max_risk_per_trade

        if mode == "vol_targeted":
            # Scale base by target/realized vol, clamped [0.5, 2.0] (matches
            # vol_momentum.py:105-110 pattern). If realized vol unknown,
            # falls back to base.
            if realized_vol is None or realized_vol < 1e-10:
                return self.max_risk_per_trade
            scalar = max(0.5, min(2.0, self.vol_target / realized_vol))
            return self.max_risk_per_trade * scalar

        if mode in ("risk_scaled", "kelly_fractional"):
            # Framework already rewrote max_risk_per_trade pre-construction
            # via _apply_leverage_mode() (task #114). We just emit it as-is
            # — the transform already happened at construction time.
            return self.max_risk_per_trade

        # Unknown/future mode → fall back to base
        return self.max_risk_per_trade

    # ── Realized vol (reused pattern from vol_momentum.py) ───────────

    def _compute_realized_vol(self) -> float | None:
        """Annualized realized volatility over the last `vol_lookback` bars."""
        if len(self._vol_close_buffer) < self.vol_lookback + 1:
            return None
        prices = np.array(list(self._vol_close_buffer))
        log_ret = np.diff(np.log(prices))
        vol = float(log_ret.std())
        if vol < 1e-10:
            return None
        # Annualize — assumes base timeframe is 1h by default (8760 bars/year).
        # For other timeframes, pass a different `vol_annualization_hours`.
        return vol * np.sqrt(self.vol_annualization_hours)

    # ── ALMA pair computation (same as parent v1) ────────────────────

    def _compute_alma_pair(self) -> tuple[float | None, float | None]:
        """Compute ALMA(close) and ALMA(open) from the alt-TF buffers."""
        if len(self._alt_close_buffer) < self.alma_length:
            return (None, None)
        close_series = pd.Series(list(self._alt_close_buffer))
        open_series = pd.Series(list(self._alt_open_buffer))
        alma_close = _alma(close_series, self.alma_length, self.alma_offset, self.alma_sigma)
        alma_open = _alma(open_series, self.alma_length, self.alma_offset, self.alma_sigma)
        return (float(alma_close.iloc[-1]), float(alma_open.iloc[-1]))

    # ── HTF EMA 50 (rolling, updated on each closed alt-TF bar) ──────

    def _update_htf_ema(self, alt_close: float) -> None:
        """Advance the rolling HTF EMA using the classic EMA formula."""
        self._htf_ema_buffer.append(alt_close)
        if len(self._htf_ema_buffer) < self.htf_ema_period:
            self._htf_ema_value = None
            return
        if self._htf_ema_value is None:
            # First valid value — seed with the SMA
            self._htf_ema_value = float(
                np.mean(list(self._htf_ema_buffer)[-self.htf_ema_period:])
            )
            return
        # Standard EMA recurrence: ema_t = α × close_t + (1 − α) × ema_{t−1}
        alpha = 2.0 / (self.htf_ema_period + 1)
        self._htf_ema_value = alpha * alt_close + (1 - alpha) * self._htf_ema_value

    # ── Exit management (simple SL/TP OR 3-tier virtual-leg ladder) ──

    def _reset_position(self) -> None:
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0
        self._entry_direction = 0
        self._ladder_tp1_price = 0.0
        self._ladder_tp2_price = 0.0
        self._ladder_tp3_price = 0.0
        self._ladder_tp1_hit = False
        self._ladder_tp2_hit = False

    # ── Ladder math — ported from parent v1 _ladder_weighted_exit ────

    def _ladder_weighted_exit(
        self,
        *,
        direction: int,
        sl_hit: bool,
        tp3_hit: bool,
        opposite_signal_close: float | None = None,
    ) -> float:
        """Compute the virtual weighted-average exit price for a ladder close.

        Ported verbatim from `swift_alma.py::_ladder_weighted_exit` (task #132).
        The semantics are identical — v2 just uses ATR-scaled TP/SL levels
        which are already baked into `_ladder_tp1/2/3_price` and `_sl_price`.

        Convention: `total_return` is the position's NET GAIN as a fraction of
        entry, sign-agnostic to direction. Positive = profit, negative = loss.
          - LONG:  exit = entry × (1 + total_return)
          - SHORT: exit = entry × (1 - total_return)

        This way engine P&L = (exit-entry)×qty for long and (entry-exit)×qty
        for short both evaluate to +total_return × qty × entry.

        Cases:
          - tp3_hit: all 3 TPs filled → positive weighted return
          - sl_hit: legs that hadn't hit close at SL (loss); hit legs keep their profit
          - opposite_signal_close provided: ongoing position closed at next
            crossover's bar close; legs that hadn't hit close at the signal
            price (may be gain or loss depending on direction)
        """
        entry = self._entry_price
        if entry <= 0:
            return 0.0

        # Per-leg returns as absolute fractions (positive for profit, negative
        # for loss) regardless of direction. Derived from the cached price
        # levels vs entry, so they work whether ATR or percent SL was used.
        if direction > 0:
            tp1_ret = (self._ladder_tp1_price - entry) / entry
            tp2_ret = (self._ladder_tp2_price - entry) / entry
            tp3_ret = (self._ladder_tp3_price - entry) / entry
            sl_ret = (self._sl_price - entry) / entry  # negative
        else:
            tp1_ret = (entry - self._ladder_tp1_price) / entry
            tp2_ret = (entry - self._ladder_tp2_price) / entry
            tp3_ret = (entry - self._ladder_tp3_price) / entry
            sl_ret = (entry - self._sl_price) / entry  # negative

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
            total_return += self.tp3_qty * sl_ret  # tp3 never hits in sl-close path
        elif opposite_signal_close is not None:
            # Current position return at the opposite-signal bar close,
            # sign-agnostic via direction.
            if direction > 0:
                current_ret = (opposite_signal_close - entry) / entry
            else:
                current_ret = (entry - opposite_signal_close) / entry
            total_return += self.tp1_qty * (tp1_ret if self._ladder_tp1_hit else current_ret)
            total_return += self.tp2_qty * (tp2_ret if self._ladder_tp2_hit else current_ret)
            total_return += self.tp3_qty * current_ret

        # Sign-aware exit price so engine's direction-dependent P&L formula
        # reproduces total_return × qty × entry.
        if direction > 0:
            return entry * (1.0 + total_return)
        return entry * (1.0 - total_return)

    def _check_intrabar_exits(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        high: float,
        low: float,
    ) -> Signal | None:
        """Check SL/TP. Dispatches to ladder mode or simple single-TP based on
        `use_pine_ladder`."""
        if self._position == "FLAT":
            return None

        if self.use_pine_ladder:
            return self._check_ladder_exits(
                symbol=symbol, timeframe=timeframe,
                close=close, high=high, low=low,
            )

        # Single-TP mode (v2 MVP default)
        if self._entry_direction > 0:  # LONG
            if low <= self._sl_price:
                exit_px = self._sl_price
                self._reset_position()
                self._bars_since_exit = 0
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "sl"},
                )
            if high >= self._tp_price:
                exit_px = self._tp_price
                self._reset_position()
                self._bars_since_exit = 0
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
                self._bars_since_exit = 0
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "sl"},
                )
            if low <= self._tp_price:
                exit_px = self._tp_price
                self._reset_position()
                self._bars_since_exit = 0
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
        """Ladder intrabar exit logic — tracks TP1/TP2/TP3 hits and SL.

        Ported from `swift_alma.py::_check_ladder_exits`. A bar that touches
        TP1 marks it as hit but does NOT close (50% leg is virtually closed,
        engine still sees full position). Subsequent bars can touch TP2, then
        TP3, or SL. Only TP3 or SL trigger an actual `CLOSE` signal.
        """
        direction = self._entry_direction

        if direction > 0:
            # LONG: TPs above entry, SL below
            if not self._ladder_tp1_hit and high >= self._ladder_tp1_price:
                self._ladder_tp1_hit = True
            if self._ladder_tp1_hit and not self._ladder_tp2_hit and high >= self._ladder_tp2_price:
                self._ladder_tp2_hit = True
            # TP3: full ladder exit
            if self._ladder_tp2_hit and high >= self._ladder_tp3_price:
                exit_px = self._ladder_weighted_exit(
                    direction=+1, sl_hit=False, tp3_hit=True,
                )
                self._reset_position()
                self._bars_since_exit = 0
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=0.95,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_tp3"},
                )
            # SL: full exit of remaining legs at weighted average
            if low <= self._sl_price:
                exit_px = self._ladder_weighted_exit(
                    direction=+1, sl_hit=True, tp3_hit=False,
                )
                self._reset_position()
                self._bars_since_exit = 0
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_sl"},
                )
        else:
            # SHORT: TPs below entry, SL above
            if not self._ladder_tp1_hit and low <= self._ladder_tp1_price:
                self._ladder_tp1_hit = True
            if self._ladder_tp1_hit and not self._ladder_tp2_hit and low <= self._ladder_tp2_price:
                self._ladder_tp2_hit = True
            if self._ladder_tp2_hit and low <= self._ladder_tp3_price:
                exit_px = self._ladder_weighted_exit(
                    direction=-1, sl_hit=False, tp3_hit=True,
                )
                self._reset_position()
                self._bars_since_exit = 0
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
                self._bars_since_exit = 0
                return Signal(
                    symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                    strategy_name=self.name, timeframe=timeframe,
                    entry_price=exit_px,
                    metadata={"exit_reason": "ladder_sl"},
                )
        return None

    # ── Main signal handler ──────────────────────────────────────────

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        close = float(features["close"])
        high = float(features["high"])
        low = float(features["low"])

        # 1. Accumulate realized-vol buffer (every base bar)
        self._vol_close_buffer.append(close)

        # 2. Feed alt-TF builder (every base bar)
        closed_alt = self._alt_builder.feed(features)

        # 3. If in position, run intrabar exits FIRST
        exit_sig = self._check_intrabar_exits(
            symbol=symbol, timeframe=timeframe, close=close, high=high, low=low,
        )
        if exit_sig is not None:
            return exit_sig

        # 3.5. Resolve pending reversal (task #132 same_bar_flip=True path).
        # When a reversal fired on a previous bar, we emitted CLOSE and stashed
        # the new direction. Open the opposite position NOW that we're FLAT.
        if (self._pending_reversal_direction != 0
                and self._position == "FLAT"
                and self.same_bar_flip):
            direction = self._pending_reversal_direction
            sl_distance = self._pending_reversal_sl_distance
            tp_distance = self._pending_reversal_tp_distance
            atr_val = self._pending_reversal_atr_val
            self._pending_reversal_direction = 0
            return self._open_position(
                symbol=symbol, timeframe=timeframe, close=close,
                direction=direction, sl_distance=sl_distance,
                tp_distance=tp_distance, atr_val=atr_val,
                features=features,
            )

        # 4. Increment cooldown counter (only when FLAT)
        if self._position == "FLAT":
            self._bars_since_exit += 1

        # 5. Signals can ONLY fire on a closed alt-TF bar
        if closed_alt is None:
            return None

        # Append alt-TF close/open to ALMA buffers + update HTF EMA
        alt_close = float(closed_alt["close"])
        alt_open = float(closed_alt["open"])
        self._alt_close_buffer.append(alt_close)
        self._alt_open_buffer.append(alt_open)
        self._update_htf_ema(alt_close)

        # 6. Compute ALMA pair (may not have enough history yet)
        alma_close, alma_open = self._compute_alma_pair()
        if alma_close is None or alma_open is None:
            return None

        # 7. Detect crossover using PREVIOUS alt-TF ALMA values
        le_trigger = False
        se_trigger = False
        if self._prev_alma_close is not None and self._prev_alma_open is not None:
            if self._prev_alma_close <= self._prev_alma_open and alma_close > alma_open:
                le_trigger = True
            elif self._prev_alma_close >= self._prev_alma_open and alma_close < alma_open:
                se_trigger = True

        # Rotate previous values BEFORE any early return so state stays in sync
        self._prev_alma_close = alma_close
        self._prev_alma_open = alma_open

        # 8. Direction filter
        if self.trade_type == "LONG" and se_trigger:
            se_trigger = False
        elif self.trade_type == "SHORT" and le_trigger:
            le_trigger = False
        elif self.trade_type == "NONE":
            return None

        if not (le_trigger or se_trigger):
            return None

        new_direction = +1 if le_trigger else -1

        # Position-state dispatch:
        #   FLAT             → proceed to open (if filters pass)
        #   In same direction → ignore (already aligned)
        #   In opposite dir + same_bar_flip → reverse via _handle_reversal
        #   In opposite dir + !same_bar_flip → ignore, wait for SL/TP
        if self._position != "FLAT":
            current_direction = +1 if self._position == "LONG" else -1
            if new_direction == current_direction:
                return None  # already aligned
            if not self.same_bar_flip:
                return None  # wait for SL/TP
            # Fall through: reversal signal with same_bar_flip enabled.
            # We want the SAME filter chain applied to the reversal as to
            # a fresh entry, so we don't return here — the gates below run.

        # 9. Cooldown gate (upgrade #5) — skip for reversals (they're a close+flip)
        if self._position == "FLAT" and self._bars_since_exit < self.min_bars_between_trades:
            return None

        # 10. Session filter (upgrade #4)
        if self.session_filter:
            ts_ms = int(features.get("timestamp", 0) or 0)
            if not _is_in_session(ts_ms):
                return None

        # 11. Regime filter — ADX threshold (upgrade #2).
        # NOTE: engine writes indicators as UPPERCASE column names (ATR_14,
        # ADX_14) — see src/backtest/leveraged_engine.py `_compute_indicators`
        # and vol_momentum.py:78, 126 for the same pattern. REQUIRED_INDICATORS
        # lowercase is the RESOLVER hint; the COLUMN key is uppercase.
        adx = features.get(self._ADX_COL)
        if adx is None or pd.isna(adx) or float(adx) < self.min_adx:
            return None

        # 12. HTF trend filter — require close vs 4h EMA 50 agreement (upgrade #3)
        if self.require_htf_trend:
            if self._htf_ema_value is None:
                return None  # not enough history yet
            if le_trigger and close <= self._htf_ema_value:
                return None  # LONG rejected — below HTF trend
            if se_trigger and close >= self._htf_ema_value:
                return None  # SHORT rejected — above HTF trend

        # 13. SL/TP distance computation — two modes per `use_atr_ladder` toggle.
        # Always read ATR (used in metadata for diagnostics even when the fixed
        # percent path is active).
        atr_raw = features.get(self._ATR_COL)
        if atr_raw is None or pd.isna(atr_raw) or float(atr_raw) <= 0:
            atr_val = close * self.sl_pct  # fallback placeholder
        else:
            atr_val = float(atr_raw)

        if self.use_atr_ladder:
            # ATR-scaled (upgrade #7) — adaptive to current volatility.
            sl_distance = self.atr_sl_mult * atr_val
            tp_distance = self.atr_tp_mult * atr_val  # used for single-TP mode
        else:
            # Fixed-percent SL/TP (parent v1 style). TP distance derives from
            # sl_distance via the atr_tp/sl ratio (preserves reward:risk).
            sl_distance = close * self.sl_pct
            rr_ratio = self.atr_tp_mult / max(self.atr_sl_mult, 1e-6)
            tp_distance = sl_distance * rr_ratio

        # 14. Entry dispatch (task #132)
        if self._position == "FLAT":
            return self._open_position(
                symbol=symbol, timeframe=timeframe, close=close,
                direction=new_direction, sl_distance=sl_distance,
                tp_distance=tp_distance, atr_val=atr_val, features=features,
            )

        # Reversal path (same_bar_flip=True, already validated above)
        return self._handle_reversal(
            symbol=symbol, timeframe=timeframe, close=close,
            new_direction=new_direction, sl_distance=sl_distance,
            tp_distance=tp_distance, atr_val=atr_val,
        )

    # ── Entry helper — used by fresh entries AND reversal resolution ─

    def _open_position(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        direction: int,
        sl_distance: float,
        tp_distance: float,
        atr_val: float,
        features: pd.Series,
    ) -> Signal:
        """Emit an entry signal and record SL/TP (or ladder TPs) levels."""
        self._entry_price = close
        self._entry_direction = direction
        self._ladder_tp1_hit = False
        self._ladder_tp2_hit = False

        # Compute ladder levels if ladder mode is active.
        # atr_tp1_mult (2.0) / atr_tp2_mult (3.0) / atr_tp3_mult (4.0) are
        # R-multipliers applied to either ATR (use_atr_ladder=True) or
        # sl_distance (use_atr_ladder=False). The latter reproduces parent
        # v1's 1%/1.5%/2% ladder when sl_pct=0.005.
        if self.use_pine_ladder:
            if self.use_atr_ladder:
                t1 = self.atr_tp1_mult * atr_val
                t2 = self.atr_tp2_mult * atr_val
                t3 = self.atr_tp3_mult * atr_val
            else:
                # In percent mode: atr_tp*_mult act as R-multipliers of sl_distance
                t1 = self.atr_tp1_mult * sl_distance
                t2 = self.atr_tp2_mult * sl_distance
                t3 = self.atr_tp3_mult * sl_distance
        else:
            t1 = t2 = t3 = 0.0  # unused in single-TP mode

        if direction > 0:  # LONG
            self._sl_price = close - sl_distance
            if self.use_pine_ladder:
                self._ladder_tp1_price = close + t1
                self._ladder_tp2_price = close + t2
                self._ladder_tp3_price = close + t3
                tp_for_signal = self._ladder_tp3_price  # furthest TP for metadata
            else:
                self._tp_price = close + tp_distance
                tp_for_signal = self._tp_price
            action = SignalAction.LONG
        else:  # SHORT
            self._sl_price = close + sl_distance
            if self.use_pine_ladder:
                self._ladder_tp1_price = close - t1
                self._ladder_tp2_price = close - t2
                self._ladder_tp3_price = close - t3
                tp_for_signal = self._ladder_tp3_price
            else:
                self._tp_price = close - tp_distance
                tp_for_signal = self._tp_price
            action = SignalAction.SHORT

        # Mode-aware sizing — THE CORE of v2 (upgrade #6)
        realized_vol = self._compute_realized_vol()
        effective_risk_pct = self._effective_risk_pct(realized_vol)

        # Re-read adx for metadata (scoped to _open_position — not shared with on_features)
        adx_meta = features.get(self._ADX_COL)
        adx_for_meta = (
            float(adx_meta) if adx_meta is not None and not pd.isna(adx_meta) else None
        )

        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.85,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            stop_loss=self._sl_price,
            take_profit=tp_for_signal,
            risk_pct=effective_risk_pct,
            metadata={
                "leverage_mode": self.leverage_mode or "margin_capped",
                "effective_risk_pct": effective_risk_pct,
                "base_risk_pct": self.max_risk_per_trade,
                "sl_pct": self.sl_pct,
                "k_ratio": (
                    self.max_risk_per_trade / self.sl_pct
                    if self.sl_pct > 0 else None
                ),
                "atr_sl_mult": self.atr_sl_mult,
                "atr_tp_mult": self.atr_tp_mult,
                "atr_value": atr_val,
                "regime_adx": adx_for_meta,
                "htf_ema": self._htf_ema_value,
                "realized_vol": realized_vol,
                "vol_target": self.vol_target,
                "use_pine_ladder": self.use_pine_ladder,
                "same_bar_flip": self.same_bar_flip,
            },
        )

    # ── Reversal handler — ported from parent v1 _handle_reversal ────

    def _handle_reversal(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        new_direction: int,
        sl_distance: float,
        tp_distance: float,
        atr_val: float,
    ) -> Signal:
        """Handle an opposite-direction signal while holding a position.

        Emits CLOSE immediately with the weighted-average exit price (ladder)
        or simple close price (single-TP), AND stashes `new_direction` +
        sizing params in `_pending_reversal_*` state. The NEXT call to
        `on_features` resolves the pending reversal and opens the opposite
        position via `_open_position`.

        This produces 1-bar-off approximation of Pine's exact same-bar flip
        (engine limitation: process() returns ONE signal per call). Same
        approach as parent v1 `_handle_reversal`.
        """
        current_direction = self._entry_direction

        # Compute the exit price for the current position
        if self.use_pine_ladder:
            exit_px = self._ladder_weighted_exit(
                direction=current_direction,
                sl_hit=False, tp3_hit=False,
                opposite_signal_close=close,
            )
        else:
            exit_px = close  # simple close at current bar's close

        # Stash for the next-bar reversal resolution
        self._pending_reversal_direction = new_direction
        self._pending_reversal_sl_distance = sl_distance
        self._pending_reversal_tp_distance = tp_distance
        self._pending_reversal_atr_val = atr_val

        # Reset ladder/position state (engine will see position as FLAT next bar)
        self._reset_position()

        return Signal(
            symbol=symbol,
            action=SignalAction.CLOSE,
            confidence=0.8,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=exit_px,
            metadata={
                "exit_reason": "reversal",
                "new_direction": "LONG" if new_direction > 0 else "SHORT",
                "ladder": self.use_pine_ladder,
            },
        )
