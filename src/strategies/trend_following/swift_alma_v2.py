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
        use_atr_ladder: bool = True,
        atr_sl_mult: float = 1.0,
        atr_tp_mult: float = 1.5,
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

        # Position state (simple single-TP; no virtual leg ladder in v2 MVP)
        self._entry_price: float = 0.0
        self._sl_price: float = 0.0
        self._tp_price: float = 0.0
        self._entry_direction: int = 0  # +1 LONG / -1 SHORT

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

    # ── Exit management (simple SL/TP — no virtual leg ladder in MVP) ─

    def _reset_position(self) -> None:
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0
        self._entry_direction = 0

    def _check_intrabar_exits(
        self,
        *,
        symbol: str,
        timeframe: str,
        close: float,
        high: float,
        low: float,
    ) -> Signal | None:
        """Check SL/TP. Single-TP mode — v2 doesn't use virtual leg accounting."""
        if self._position == "FLAT":
            return None

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

        # Only consider new entries when FLAT (v2 doesn't support reversals for MVP
        # simplicity — close at SL/TP or via cooldown gap)
        if self._position != "FLAT":
            return None

        # 9. Cooldown gate (upgrade #5)
        if self._bars_since_exit < self.min_bars_between_trades:
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

        # 13. ATR-scaled SL/TP (upgrade #7) — UPPERCASE column per engine convention
        atr = features.get(self._ATR_COL)
        if atr is None or pd.isna(atr) or float(atr) <= 0:
            # Fallback when ATR unavailable: use percent-based levels
            atr_val = close * self.sl_pct
        else:
            atr_val = float(atr)

        sl_distance = self.atr_sl_mult * atr_val
        tp_distance = self.atr_tp_mult * atr_val

        direction = +1 if le_trigger else -1

        if direction > 0:
            self._sl_price = close - sl_distance
            self._tp_price = close + tp_distance
            action = SignalAction.LONG
        else:
            self._sl_price = close + sl_distance
            self._tp_price = close - tp_distance
            action = SignalAction.SHORT

        self._entry_price = close
        self._entry_direction = direction

        # 14. Mode-aware sizing — THE CORE of v2 (upgrade #6)
        realized_vol = self._compute_realized_vol()
        effective_risk_pct = self._effective_risk_pct(realized_vol)

        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.85,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            stop_loss=self._sl_price,
            take_profit=self._tp_price,
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
                "regime_adx": float(adx) if adx is not None and not pd.isna(adx) else None,
                "htf_ema": self._htf_ema_value,
                "realized_vol": realized_vol,
                "vol_target": self.vol_target,
            },
        )
