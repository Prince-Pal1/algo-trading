"""Unit tests for AdaptiveMomentumStrategy.

Focus areas (per plan):
  - construction / param wiring + leverage_range validation
  - from_config round-trips every param
  - deterministic signal math: skip-offset invariance (the #1 off-by-one trap),
    direction sign, Kaufman ER, and warmup gating
  - behavioral: LONG entry on a trending series, chandelier exit, and
    engine-forced-flat state resync
"""
from __future__ import annotations

import math
from collections import deque

import pandas as pd
import pytest

from src.strategies.momentum.adaptive_momentum import AdaptiveMomentumStrategy
from src.utils.types import RiskProfile, SignalAction


def _make(**kw) -> AdaptiveMomentumStrategy:
    params = dict(
        name="adaptive_momentum_test",
        markets=["DOGEUSDT"],
        timeframe="1h",
    )
    params.update(kw)
    return AdaptiveMomentumStrategy(**params)


def _drive(s: AdaptiveMomentumStrategy, closes, atr_frac=0.02):
    """Feed closes through on_features, mimicking base.process() position state."""
    out = []
    for i, c in enumerate(closes):
        feat = pd.Series({"close": float(c), "ATR_14": float(c) * atr_frac})
        sig = s.on_features("DOGEUSDT", "1h", feat)
        if sig is not None:
            if sig.action.value in ("LONG", "SHORT"):
                s._position = sig.action.value
            elif sig.action.value == "CLOSE":
                s._position = "FLAT"
            out.append((i, float(c), sig))
    return out


class TestConstruction:
    def test_defaults(self):
        s = _make()
        assert s.markets == ["DOGEUSDT"]
        assert s.timeframe == "1h"
        assert s.lookbacks == (24, 72, 168)
        assert s.skip_bars == 1
        assert s.er_window == 72
        assert s.fee_style == "swing"
        assert s.long_only is False
        # deque must hold the longest thing we read + a small buffer
        assert s._closes.maxlen == max(1 + 168, 168, 72) + 2

    def test_lookbacks_coerced_to_int_tuple(self):
        s = _make(lookbacks=[12.0, 24.0])
        assert s.lookbacks == (12, 24)

    def test_custom_leverage_range(self):
        s = _make(leverage_range=(1.0, 5.0))
        assert s.leverage_range == (1.0, 5.0)

    def test_invalid_leverage_range_rejected(self):
        with pytest.raises(ValueError, match="leverage_range"):
            _make(leverage_range=(0.5, 10.0))


class TestFromConfig:
    def test_round_trips_every_param(self):
        s = AdaptiveMomentumStrategy.from_config("adaptive_momentum")
        assert s.markets == ["DOGEUSDT", "ADAUSDT", "DOTUSDT"]
        assert s.timeframe == "1h"
        assert s.risk_profile == RiskProfile.SAFE
        assert s.lookbacks == (24, 72, 168)
        assert s.skip_bars == 1
        assert s.er_window == 72
        assert s.er_threshold == 0.30
        assert s.vol_lookback == 168
        assert s.vol_target == 0.15
        assert s.signal_gain == 2.0
        assert s.entry_threshold == 0.15
        assert s.exit_threshold == 0.10
        assert s.atr_period == 14
        assert s.sl_atr_mult == 3.0
        assert s.trail_atr_mult == 4.0
        assert s.max_hold_bars == 336
        assert s.cooldown_bars == 5
        assert s.rebalance_interval == 6
        assert s.long_only is False
        assert s.max_risk_per_trade == 0.012


class TestCompositeSignal:
    def test_warmup_returns_none(self):
        s = _make(lookbacks=(3,), skip_bars=1)
        s._closes = deque([100.0, 101.0, 102.0, 103.0])  # need skip+L+1 = 5
        assert s._compute_composite_signal() is None

    def test_skip_invariance_off_by_one_guard(self):
        # skip_bars=1 means the most recent bar is excluded from the signal.
        # Changing ONLY the last close must NOT change the composite.
        a = _make(lookbacks=(3,), skip_bars=1)
        b = _make(lookbacks=(3,), skip_bars=1)
        a._closes = deque([100.0, 101.0, 103.0, 106.0, 110.0, 120.0])
        b._closes = deque([100.0, 101.0, 103.0, 106.0, 110.0, 999.0])
        sa, _ = a._compute_composite_signal()
        sb, _ = b._compute_composite_signal()
        assert sa == pytest.approx(sb)

    def test_skip_uses_correct_anchors(self):
        # recent anchor = close[-(skip+1)], far anchor = close[-(skip+L+1)].
        s = _make(lookbacks=(3,), skip_bars=1)
        closes = [100.0, 101.0, 103.0, 106.0, 110.0, 120.0]
        s._closes = deque(closes)
        comp, per = s._compute_composite_signal()
        # Independent recompute on the explicit window [101,103,106,110]
        window = [101.0, 103.0, 106.0, 110.0]
        import numpy as np
        lw = np.log(window)
        expected = float((lw[-1] - lw[0]) / (np.diff(lw).std() * np.sqrt(3)))
        assert per["s_3"] == pytest.approx(round(expected, 4))
        assert comp == pytest.approx(expected, abs=1e-3)

    def test_direction_sign(self):
        up = _make(lookbacks=(4,), skip_bars=1)
        up._closes = deque([100.0, 101.5, 101.0, 103.0, 103.5, 105.5])
        s_up, _ = up._compute_composite_signal()
        assert s_up > 0

        down = _make(lookbacks=(4,), skip_bars=1)
        down._closes = deque([110.0, 108.0, 108.5, 106.0, 105.0, 103.0])
        s_down, _ = down._compute_composite_signal()
        assert s_down < 0

    def test_flat_horizon_contributes_zero(self):
        # perfectly constant prices -> per-bar vol 0 -> s_l guarded to 0.0
        s = _make(lookbacks=(3,), skip_bars=1)
        s._closes = deque([50.0] * 8)
        comp, per = s._compute_composite_signal()
        assert comp == 0.0
        assert per["s_3"] == 0.0


class TestEfficiencyRatio:
    def test_monotonic_trend_is_one(self):
        s = _make(er_window=3)
        s._closes = deque([100.0, 101.0, 102.0, 103.0])
        assert s._compute_efficiency_ratio() == pytest.approx(1.0)

    def test_choppy_is_low(self):
        s = _make(er_window=3)
        s._closes = deque([100.0, 101.0, 100.0, 101.0])
        assert s._compute_efficiency_ratio() == pytest.approx(1.0 / 3.0)

    def test_flat_returns_none(self):
        s = _make(er_window=3)
        s._closes = deque([100.0, 100.0, 100.0, 100.0])
        assert s._compute_efficiency_ratio() is None


class TestBehavioral:
    def _uptrend(self, n=140):
        return [100.0 * (1.02 ** i) * (1 + 0.02 * math.sin(i)) for i in range(n)]

    def test_enters_long_on_uptrend(self):
        s = _make(
            lookbacks=(6, 12), skip_bars=1, er_window=12, er_threshold=0.20,
            vol_lookback=24, entry_threshold=0.15, cooldown_bars=2,
            rebalance_interval=3,
        )
        sigs = _drive(s, self._uptrend())
        longs = [x for x in sigs if x[2].action == SignalAction.LONG]
        assert longs, "expected at least one LONG on a strong uptrend"
        _, entry_px, sig = longs[0]
        assert sig.stop_loss is not None and sig.stop_loss < entry_px
        assert sig.take_profit is None          # let chandelier/timeout run winners
        assert sig.risk_pct is not None and sig.risk_pct > 0
        assert 0.5 <= sig.confidence <= 1.0

    def test_chandelier_exit_fires(self):
        s = _make(lookbacks=(6, 12), skip_bars=1, er_window=12,
                  vol_lookback=24, trail_atr_mult=4.0)
        # Warm the price buffer so signal/vol are valid.
        for c in self._uptrend():
            s._closes.append(float(c))
        hwm = s._closes[-1]
        s._position = "LONG"
        s._was_in_position = True
        s._hwm_close = hwm
        s._bars_in_position = 20
        # A 20% drop far exceeds the 4*ATR (=8% of close) chandelier distance;
        # one down-bar won't flip the long-horizon composite negative.
        low = hwm * 0.80
        feat = pd.Series({"close": low, "ATR_14": low * 0.02})
        sig = s.on_features("DOGEUSDT", "1h", feat)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "chandelier"

    def test_engine_forced_flat_resyncs_state(self):
        # entry_threshold huge -> no new entry, so we can observe the resync.
        s = _make(lookbacks=(6, 12), skip_bars=1, er_window=12,
                  vol_lookback=24, entry_threshold=999.0)
        for c in self._uptrend():
            s._closes.append(float(c))
        # Simulate the engine hard-stopping us: it sets _position FLAT directly,
        # our _close() never ran, so _was_in_position is still True.
        s._position = "FLAT"
        s._was_in_position = True
        s._hwm_close = s._closes[-1]
        nxt = s._closes[-1] * 1.001
        feat = pd.Series({"close": nxt, "ATR_14": nxt * 0.02})
        s.on_features("DOGEUSDT", "1h", feat)
        assert s._was_in_position is False
        assert s._hwm_close == 0.0
        assert s._bars_since_exit == 0
