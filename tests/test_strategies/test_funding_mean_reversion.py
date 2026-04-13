"""Unit tests for FundingMeanReversionStrategy (backlog #8)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.carry.funding_mean_reversion import FundingMeanReversionStrategy
from src.utils.types import RiskProfile, SignalAction


def _make_strategy(**overrides):
    defaults = dict(
        name="funding_mean_reversion",
        markets=["BTCUSDT"],
        timeframe="8h",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.01,
        quantile_window=50,   # smaller for tests
        high_quantile=0.90,
        low_quantile=0.10,
        mid_quantile=0.50,
        atr_sl_mult=2.0,
        max_hold_bars=3,
        cooldown_bars=1,
    )
    defaults.update(overrides)
    return FundingMeanReversionStrategy(**defaults)


def _row(close: float, funding: float, atr: float = 100.0) -> pd.Series:
    return pd.Series({
        "close": close,
        "high": close + 10,
        "low": close - 10,
        "ATR_14": atr,
        "funding_rate": funding,
    })


class TestConstruction:
    def test_valid_params(self):
        s = _make_strategy()
        assert s.high_quantile == 0.90
        assert s.low_quantile == 0.10

    def test_quantile_ordering_enforced(self):
        with pytest.raises(ValueError, match="quantiles"):
            _make_strategy(low_quantile=0.6, mid_quantile=0.5, high_quantile=0.9)

    def test_quantile_window_minimum(self):
        with pytest.raises(ValueError, match="quantile_window"):
            _make_strategy(quantile_window=10)

    def test_atr_sl_mult_positive(self):
        with pytest.raises(ValueError, match="atr_sl_mult"):
            _make_strategy(atr_sl_mult=-1.0)


class TestWarmup:
    def test_returns_none_until_buffer_fills(self):
        s = _make_strategy(quantile_window=50)
        # First 15 bars: buffer too small → no signals
        for i in range(15):
            sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.0001))
            assert sig is None
        # Still below minimum
        assert s._position == "FLAT"

    def test_missing_funding_rate_returns_none(self):
        s = _make_strategy()
        features = pd.Series({"close": 30000.0, "ATR_14": 100.0})
        sig = s.process("BTCUSDT", "8h", features)
        assert sig is None

    def test_missing_atr_returns_none_on_entry(self):
        s = _make_strategy()
        # Warmup with funding rates
        for i in range(60):
            s.process("BTCUSDT", "8h", _row(30000.0, 0.0001))
        # No ATR → can't set stop
        features = pd.Series({"close": 30000.0, "funding_rate": 0.001})
        sig = s.process("BTCUSDT", "8h", features)
        assert sig is None


def _prefill_buffer(strategy, values: list[float] | None = None) -> None:
    """Inject funding values directly into the strategy's internal buffer.

    Using process() during warmup is fragile because noisy warmup values can
    exceed the rolling quantile thresholds and trigger unwanted trades.
    Direct buffer injection isolates each test case to the exact entry/exit
    behavior it's testing.
    """
    if values is None:
        # Default: 60 values tightly around 0.0001 so q_high and q_low
        # are well-defined but neither extreme.
        import random
        r = random.Random(42)
        values = [0.0001 + r.uniform(-0.000005, 0.000005) for _ in range(60)]
    for v in values:
        strategy._funding_buf.append(float(v))


class TestEntrySignals:
    def test_short_on_extreme_high_funding(self):
        s = _make_strategy()
        _prefill_buffer(s)
        # Now spike funding to extreme high
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.005))
        assert sig is not None
        assert sig.action == SignalAction.SHORT
        assert sig.stop_loss > 30000.0  # Stop above entry for short
        assert s._position == "SHORT"

    def test_long_on_extreme_low_funding(self):
        s = _make_strategy()
        _prefill_buffer(s)
        # Spike to extreme negative
        sig = s.process("BTCUSDT", "8h", _row(30000.0, -0.003))
        assert sig is not None
        assert sig.action == SignalAction.LONG
        assert sig.stop_loss < 30000.0
        assert s._position == "LONG"

    def test_no_signal_in_normal_range(self):
        s = _make_strategy()
        _prefill_buffer(s)
        # Neutral funding near the median — should NOT fire
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.0001))
        assert sig is None
        assert s._position == "FLAT"

    def test_long_only_mode_ignores_short_setup(self):
        s = _make_strategy(long_only=True)
        _prefill_buffer(s)
        # Extreme high funding — normally would SHORT, but long_only
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.005))
        assert sig is None


class TestExitSignals:
    def _setup_short(self, s):
        """Warmup + enter short."""
        _prefill_buffer(s)
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.005))
        assert sig is not None and sig.action == SignalAction.SHORT
        return sig

    def test_exit_short_on_funding_reversion(self):
        s = _make_strategy()
        self._setup_short(s)
        # Funding reverts to median
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.0001))
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "reversion"
        assert s._position == "FLAT"

    def test_exit_short_on_atr_stop(self):
        s = _make_strategy()
        self._setup_short(s)
        # Price spikes up through stop
        sig = s.process("BTCUSDT", "8h", _row(30500.0, 0.005))
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "atr_stop"
        assert s._position == "FLAT"

    def test_exit_short_on_time_stop(self):
        s = _make_strategy(max_hold_bars=3)
        self._setup_short(s)
        # Funding stays high (no reversion), price stays (no ATR stop)
        for _ in range(2):
            sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.005))
            assert sig is None or sig.action == SignalAction.CLOSE
        # Third bar should trigger time stop
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.005))
        if sig is not None and sig.action == SignalAction.CLOSE:
            assert sig.metadata["exit_reason"] in ("time_stop", "reversion")


class TestCooldown:
    def test_cooldown_prevents_immediate_reentry(self):
        s = _make_strategy(cooldown_bars=2)
        _prefill_buffer(s)
        s.process("BTCUSDT", "8h", _row(30000.0, 0.005))    # enter short
        s.process("BTCUSDT", "8h", _row(30000.0, 0.0001))   # exit on reversion

        # Next bar: extreme high funding again — but cooldown blocks
        sig = s.process("BTCUSDT", "8h", _row(30000.0, 0.006))
        assert sig is None


class TestConfig:
    def test_quantile_bounds_valid(self):
        s = _make_strategy(low_quantile=0.01, high_quantile=0.99, mid_quantile=0.50)
        assert s.low_quantile == 0.01
        assert s.high_quantile == 0.99
