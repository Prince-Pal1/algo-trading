"""Adaptive Momentum Strategy — a modern CTA-style trend engine.

An advanced evolution of `vol_momentum`. Where vol_momentum uses a single
168h log-return with binary long/short and sign-flip exits, this strategy:

  1. Multi-horizon blend — equal-weight mean of risk-adjusted momentum across
     several lookbacks (default 24h/72h/168h). Removes single-lookback timing
     luck (AQR "A Century of Evidence on Trend-Following").
  2. Volatility-normalized signal — each horizon's signal is
     log_return / (per-bar_vol * sqrt(L)), a z-score/t-stat-like measure so
     calm and wild regimes are comparable and noise stops firing big signals.
  3. Skip-recent-period — drops the most recent `skip_bars` bars to dodge the
     sharp short-horizon reversal crypto alts show (Jegadeesh-Titman; Wisselink
     2018 for crypto).
  4. Trend-quality gate — Kaufman Efficiency Ratio: only enter when the market
     is actually trending (ER > er_threshold), suppressing chop losses.
  5. Continuous tanh conviction sizing — risk_pct scales smoothly with signal
     strength instead of binary all-in/all-out.
  6. ATR chandelier trailing stop — lets winners run (no fixed take-profit)
     while locking in trend gains.
  7. Hysteresis — exit-on-reversal only when the signal crosses the *opposite*
     band, not merely zero, cutting fee churn.
  8. Inverse-vol position scaling (vol_target / realized_vol, clamped) — kept
     from vol_momentum.

Time-series (per-symbol) by design — NOT cross-sectional (see clenow_momentum
obituary: cross-sectional needs a dense universe). The router instantiates one
instance per market, so all rolling state below is per-symbol-isolated.

Exit logic (checked before entries):
    - Chandelier: price retraces trail_atr_mult * ATR from the in-trade extreme
    - Momentum reversal (hysteresis): composite signal crosses opposite band
    - Timeout: max_hold_bars
    - Hard stop_loss (entry +/- sl_atr_mult * ATR) — enforced intrabar by the
      engine; the true catastrophic-gap cap.
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy
from src.utils.config import get_config
from src.utils.types import RiskProfile, Signal, SignalAction, Tier

# Hours per year for annualizing realized vol (~8760h/yr).
_HOURS_PER_YEAR = 8760.0


def _bars_per_hour(timeframe: str) -> float:
    """Bars per hour for a timeframe string (e.g. '1h'->1, '15m'->4, '4h'->0.25)."""
    tf = timeframe.strip().lower()
    if tf.endswith("m"):
        minutes = int(tf[:-1])
    elif tf.endswith("h"):
        minutes = int(tf[:-1]) * 60
    elif tf.endswith("d"):
        minutes = int(tf[:-1]) * 1440
    elif tf.endswith("w"):
        minutes = int(tf[:-1]) * 10080
    else:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return 60.0 / minutes


class AdaptiveMomentumStrategy(BaseStrategy):
    """Multi-horizon, volatility-normalized, regime-gated trend strategy."""

    fee_style = "swing"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str,
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.012,
        *,
        leverage_range: tuple[float, float] = (1.0, 1.0),
        tier: Tier = Tier.UNCLASSIFIED,
        lookbacks: tuple[int, ...] = (24, 72, 168),
        skip_bars: int = 1,
        er_window: int = 72,
        er_threshold: float = 0.30,
        vol_lookback: int = 168,
        vol_target: float = 0.15,
        signal_gain: float = 2.0,
        entry_threshold: float = 0.15,
        exit_threshold: float = 0.10,
        atr_period: int = 14,
        sl_atr_mult: float = 3.0,
        trail_atr_mult: float = 4.0,
        max_hold_bars: int = 336,
        cooldown_bars: int = 5,
        rebalance_interval: int = 6,
        long_only: bool = False,
    ):
        super().__init__(
            name, markets, timeframe, risk_profile, max_risk_per_trade,
            leverage_range=leverage_range, tier=tier,
        )

        # Time-based params are specified in HOURS and converted to bars for the
        # active timeframe, so the strategy behaves consistently across TFs.
        # At 1h, hours == bars — the validated default behavior is preserved.
        bph = _bars_per_hour(timeframe)
        self._bars_per_hour = bph
        self._bars_per_year = _HOURS_PER_YEAR * bph

        def _to_bars(hours: float, floor: int = 1) -> int:
            return max(floor, round(float(hours) * bph))

        # Keep the raw hour specs for metadata/repr.
        self._lookbacks_h = tuple(int(lb) for lb in lookbacks)
        self._skip_h = int(skip_bars)
        self._er_window_h = int(er_window)
        self._vol_lookback_h = int(vol_lookback)
        self._max_hold_h = int(max_hold_bars)
        self._cooldown_h = int(cooldown_bars)
        self._rebalance_h = int(rebalance_interval)

        self.lookbacks = tuple(_to_bars(lb) for lb in self._lookbacks_h)
        self.skip_bars = _to_bars(self._skip_h)
        self.er_window = _to_bars(self._er_window_h, floor=2)
        self.vol_lookback = _to_bars(self._vol_lookback_h, floor=2)
        self.max_hold_bars = _to_bars(self._max_hold_h)
        self.cooldown_bars = _to_bars(self._cooldown_h)
        self.rebalance_interval = _to_bars(self._rebalance_h)

        # Scale-free / non-time params unchanged.
        self.er_threshold = er_threshold
        self.vol_target = vol_target
        self.signal_gain = signal_gain
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.trail_atr_mult = trail_atr_mult
        self.long_only = long_only

        # Internal counters / state
        self._bars_since_exit: int = 999
        self._bars_in_position: int = 0
        self._bars_since_rebalance: int = 999
        self._was_in_position: bool = False
        self._hwm_close: float = 0.0   # in-trade high-water (longs)
        self._lwm_close: float = 0.0   # in-trade low-water  (shorts)
        self._atr_col = f"ATR_{atr_period}"

        # Rolling price buffer sized to the longest thing we read.
        max_need = max(
            self.skip_bars + max(self.lookbacks),
            self.vol_lookback,
            self.er_window,
        )
        self._closes: deque[float] = deque(maxlen=max_need + 2)

    # ── signal computation (all inline from _closes) ──────────────────────

    def _compute_composite_signal(self) -> tuple[float, dict] | None:
        """Equal-weight mean of per-horizon volatility-normalized momentum.

        Per horizon L (skipping the most recent skip_bars bars):
            s_L = log(recent / far) / (per_bar_vol * sqrt(L))
        where recent = close[-(skip+1)], far = close[-(skip+L+1)].
        Returns (composite, per_horizon) or None until warmed up.
        """
        n = len(self._closes)
        skip = self.skip_bars
        needed = skip + max(self.lookbacks) + 1
        if n < needed:
            return None

        arr = np.asarray(self._closes, dtype=float)
        per_horizon: dict[str, float] = {}
        signals: list[float] = []
        for L in self.lookbacks:
            start = n - (skip + L + 1)
            end = n - skip  # exclusive; window has L+1 prices
            window = arr[start:end]
            if window.size < L + 1 or window[0] <= 0 or window[-1] <= 0:
                return None
            log_window = np.log(window)
            sum_return = float(log_window[-1] - log_window[0])
            per_bar_vol = float(np.diff(log_window).std())
            if per_bar_vol < 1e-10:
                s_l = 0.0  # flat horizon → no momentum contribution
            else:
                s_l = sum_return / (per_bar_vol * np.sqrt(L))
            per_horizon[f"s_{L}"] = round(s_l, 4)
            signals.append(s_l)

        composite = float(np.mean(signals))
        return composite, per_horizon

    def _compute_efficiency_ratio(self) -> float | None:
        """Kaufman Efficiency Ratio over er_window: |net move| / sum(|moves|)."""
        if len(self._closes) < self.er_window + 1:
            return None
        seg = np.asarray(self._closes, dtype=float)[-(self.er_window + 1):]
        change = abs(float(seg[-1] - seg[0]))
        volatility = float(np.sum(np.abs(np.diff(seg))))
        if volatility < 1e-12:
            return None
        return change / volatility

    def _compute_realized_vol(self) -> float | None:
        """Annualized realized volatility over vol_lookback (for sizing)."""
        if len(self._closes) < self.vol_lookback + 1:
            return None
        prices = np.asarray(self._closes, dtype=float)[-self.vol_lookback - 1:]
        log_ret = np.diff(np.log(prices))
        vol = float(log_ret.std())
        if vol < 1e-10:
            return None
        return vol * np.sqrt(self._bars_per_year)

    def _vol_scalar(self, realized_vol: float) -> float:
        """Inverse-vol position scalar, clamped 0.5x-2.0x (kept from vol_momentum)."""
        if realized_vol < 1e-10:
            return 1.0
        return max(0.5, min(2.0, self.vol_target / realized_vol))

    # ── exit helper ───────────────────────────────────────────────────────

    def _close(self, symbol: str, timeframe: str, close: float,
               reason: str, signal: float) -> Signal:
        self._bars_since_exit = 0
        self._bars_in_position = 0
        self._was_in_position = False
        self._hwm_close = 0.0
        self._lwm_close = 0.0
        return Signal(
            symbol=symbol,
            action=SignalAction.CLOSE,
            confidence=0.9 if reason != "timeout" else 0.7,
            strategy_name=self.name,
            timeframe=timeframe,
            entry_price=close,
            metadata={"exit_reason": reason, "signal": round(signal, 4)},
        )

    # ── main entrypoint ─────────────────────────────────────────────────────

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        # Tick counters
        if self._position == "FLAT":
            self._bars_since_exit += 1
        else:
            self._bars_in_position += 1
        self._bars_since_rebalance += 1

        # Detect engine-forced flat (hard SL/TP fired before on_features ran,
        # so our _close() never executed). Resync our internal state.
        if self._position == "FLAT" and self._was_in_position:
            self._was_in_position = False
            self._bars_since_exit = 0
            self._bars_in_position = 0
            self._hwm_close = 0.0
            self._lwm_close = 0.0

        close = features["close"]
        atr = features.get(self._atr_col)
        if atr is None or pd.isna(atr):
            return None

        self._closes.append(float(close))
        close = float(close)
        atr = float(atr)

        composite = self._compute_composite_signal()
        vol = self._compute_realized_vol()
        if composite is None or vol is None:
            return None
        S, per_horizon = composite
        er = self._compute_efficiency_ratio()

        # ── EXIT LOGIC (before entries) ──
        if self._position == "LONG":
            self._hwm_close = max(self._hwm_close, close) if self._hwm_close else close
            if close <= self._hwm_close - self.trail_atr_mult * atr:
                return self._close(symbol, timeframe, close, "chandelier", S)
            if S < -self.exit_threshold:
                return self._close(symbol, timeframe, close, "momentum_reversal", S)
            if self._bars_in_position >= self.max_hold_bars:
                return self._close(symbol, timeframe, close, "timeout", S)

        if self._position == "SHORT":
            self._lwm_close = min(self._lwm_close, close) if self._lwm_close else close
            if close >= self._lwm_close + self.trail_atr_mult * atr:
                return self._close(symbol, timeframe, close, "chandelier", S)
            if S > self.exit_threshold:
                return self._close(symbol, timeframe, close, "momentum_reversal", S)
            if self._bars_in_position >= self.max_hold_bars:
                return self._close(symbol, timeframe, close, "timeout", S)

        # ── ENTRY FILTERS ──
        if self._bars_since_exit < self.cooldown_bars:
            return None
        if self._bars_since_rebalance < self.rebalance_interval and self._position != "FLAT":
            return None
        if er is None or er < self.er_threshold:        # trend-quality gate
            return None
        if abs(S) < self.entry_threshold:               # no-trade band
            return None

        conviction = float(np.tanh(self.signal_gain * S))   # (-1, 1)
        vol_scalar = self._vol_scalar(vol)
        risk_pct = self.max_risk_per_trade * abs(conviction) * vol_scalar

        meta = {
            "signal": round(S, 4),
            "er": round(er, 3),
            "vol": round(vol, 4),
            "vol_scalar": round(vol_scalar, 2),
            "conviction": round(conviction, 3),
            "atr": round(atr, 6),
            **per_horizon,
        }

        # ── LONG ENTRY ──
        if S > 0 and self._position != "LONG":
            stop_loss = close - self.sl_atr_mult * atr
            if close - stop_loss <= 0:
                return None
            self._hwm_close = close
            self._was_in_position = True
            self._bars_in_position = 0
            self._bars_since_rebalance = 0
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=max(0.5, abs(conviction)),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=None,   # let chandelier / timeout run winners
                risk_pct=risk_pct,
                metadata=meta,
            )

        # ── SHORT ENTRY ──
        if S < 0 and not self.long_only and self._position != "SHORT":
            stop_loss = close + self.sl_atr_mult * atr
            if stop_loss - close <= 0:
                return None
            self._lwm_close = close
            self._was_in_position = True
            self._bars_in_position = 0
            self._bars_since_rebalance = 0
            return Signal(
                symbol=symbol,
                action=SignalAction.SHORT,
                confidence=max(0.5, abs(conviction)),
                strategy_name=self.name,
                timeframe=timeframe,
                entry_price=close,
                stop_loss=round(stop_loss, 8),
                take_profit=None,
                risk_pct=risk_pct,
                metadata=meta,
            )

        return None

    @classmethod
    def from_config(cls, name: str) -> AdaptiveMomentumStrategy:
        cfg = get_config()
        strat_cfg = cfg.get_strategy(name)
        return cls(
            name=name,
            markets=strat_cfg.get("markets", ["DOGEUSDT"]),
            timeframe=strat_cfg.get("timeframe", "1h"),
            risk_profile=RiskProfile(strat_cfg.get("risk_profile", "SAFE")),
            max_risk_per_trade=strat_cfg.get("max_risk_per_trade", 0.012),
            lookbacks=tuple(strat_cfg.get("lookbacks", [24, 72, 168])),
            skip_bars=strat_cfg.get("skip_bars", 1),
            er_window=strat_cfg.get("er_window", 72),
            er_threshold=strat_cfg.get("er_threshold", 0.30),
            vol_lookback=strat_cfg.get("vol_lookback", 168),
            vol_target=strat_cfg.get("vol_target", 0.15),
            signal_gain=strat_cfg.get("signal_gain", 2.0),
            entry_threshold=strat_cfg.get("entry_threshold", 0.15),
            exit_threshold=strat_cfg.get("exit_threshold", 0.10),
            atr_period=strat_cfg.get("atr_period", 14),
            sl_atr_mult=strat_cfg.get("sl_atr_mult", 3.0),
            trail_atr_mult=strat_cfg.get("trail_atr_mult", 4.0),
            max_hold_bars=strat_cfg.get("max_hold_bars", 336),
            cooldown_bars=strat_cfg.get("cooldown_bars", 5),
            rebalance_interval=strat_cfg.get("rebalance_interval", 6),
            long_only=strat_cfg.get("long_only", False),
        )
