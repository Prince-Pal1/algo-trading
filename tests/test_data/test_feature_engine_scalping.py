"""Tests for scalper primitives added to feature_engine._compute_indicators.

Covers the 6 new indicators from task #102:
  - roc_N
  - velocity_N (requires atr_20 in the same request)
  - body_pct
  - close_in_range
  - volume_zscore_N
  - dollar_volume_N
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.data.feature_engine import _compute_indicators


def _make_df(n: int = 50, base: float = 100.0) -> pd.DataFrame:
    """Build a simple OHLCV frame with gentle random walk."""
    rng = np.random.default_rng(42)
    closes = base + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0.2, 0.1, n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0.2, 0.1, n))
    volumes = rng.integers(100, 1000, n).astype(float)
    return pd.DataFrame({
        "timestamp": np.arange(n) * 60_000,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class TestROC:
    def test_roc_basic(self):
        df = _make_df(30)
        out = _compute_indicators(df.copy(), ["roc_5"])
        assert "ROC_5" in out.columns
        # ROC = 100 * (close - close[-5]) / close[-5]
        for i in range(5, 30):
            expected = 100 * (df["close"].iloc[i] - df["close"].iloc[i - 5]) / df["close"].iloc[i - 5]
            assert abs(out["ROC_5"].iloc[i] - expected) < 1e-9

    def test_roc_on_flat_series_is_zero(self):
        df = _make_df(30)
        df["close"] = 100.0  # all constant
        out = _compute_indicators(df.copy(), ["roc_5"])
        assert "ROC_5" in out.columns
        # After the first 5 rows (pre-window NaN filled to 0), ROC should be 0
        assert out["ROC_5"].iloc[10:].abs().max() < 1e-9

    def test_roc_insufficient_data_returns_no_column(self):
        df = _make_df(10)  # less than roc_5 min_rows (5 + 5 = 10... so 10 works)
        out = _compute_indicators(df.copy(), ["roc_20"])
        # roc_20 needs 20 + 5 = 25 rows, df has 10 → skipped
        assert "ROC_20" not in out.columns


class TestVelocity:
    def test_velocity_requires_atr_20(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["velocity_3"])
        # Without atr_20 the indicator is silently skipped
        assert "VELOCITY_3" not in out.columns

    def test_velocity_with_atr_20_monotonic_up(self):
        # Monotonic up: every bar's close > prior close → positive velocity
        df = _make_df(50)
        df["close"] = np.linspace(100.0, 200.0, 50)
        df["high"] = df["close"] + 0.5
        df["low"] = df["close"] - 0.5
        df["open"] = df["close"] - 0.2
        out = _compute_indicators(df.copy(), ["atr_20", "velocity_3"])
        assert "VELOCITY_3" in out.columns
        # Velocity should be strictly positive in the stable-ATR region
        v = out["VELOCITY_3"].iloc[30:]
        assert (v > 0).all()

    def test_velocity_with_atr_20_monotonic_down_is_negative(self):
        df = _make_df(50)
        df["close"] = np.linspace(200.0, 100.0, 50)
        df["high"] = df["close"] + 0.5
        df["low"] = df["close"] - 0.5
        df["open"] = df["close"] + 0.2
        out = _compute_indicators(df.copy(), ["atr_20", "velocity_3"])
        v = out["VELOCITY_3"].iloc[30:]
        assert (v < 0).all()


class TestBodyPct:
    def test_body_pct_full_body(self):
        df = _make_df(5)
        # Make the last bar a full body: close == high, open == low
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 110.0
        df.loc[4, "low"] = 100.0
        df.loc[4, "close"] = 110.0
        out = _compute_indicators(df.copy(), ["body_pct"])
        assert "BODY_PCT" in out.columns
        assert abs(out["BODY_PCT"].iloc[4] - 1.0) < 1e-9

    def test_body_pct_doji(self):
        df = _make_df(5)
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 105.0
        df.loc[4, "low"] = 95.0
        df.loc[4, "close"] = 100.0  # close == open → doji
        out = _compute_indicators(df.copy(), ["body_pct"])
        assert "BODY_PCT" in out.columns
        assert out["BODY_PCT"].iloc[4] == 0.0

    def test_body_pct_zero_range_bar_is_zero(self):
        df = _make_df(5)
        # Degenerate: high == low (should not crash, just return 0)
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 100.0
        df.loc[4, "low"] = 100.0
        df.loc[4, "close"] = 100.0
        out = _compute_indicators(df.copy(), ["body_pct"])
        assert out["BODY_PCT"].iloc[4] == 0.0


class TestCloseInRange:
    def test_close_at_low_is_zero(self):
        df = _make_df(5)
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 110.0
        df.loc[4, "low"] = 95.0
        df.loc[4, "close"] = 95.0
        out = _compute_indicators(df.copy(), ["close_in_range"])
        assert "CLOSE_IN_RANGE" in out.columns
        assert out["CLOSE_IN_RANGE"].iloc[4] == 0.0

    def test_close_at_high_is_one(self):
        df = _make_df(5)
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 110.0
        df.loc[4, "low"] = 95.0
        df.loc[4, "close"] = 110.0
        out = _compute_indicators(df.copy(), ["close_in_range"])
        assert abs(out["CLOSE_IN_RANGE"].iloc[4] - 1.0) < 1e-9

    def test_close_midpoint(self):
        df = _make_df(5)
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 110.0
        df.loc[4, "low"] = 90.0
        df.loc[4, "close"] = 100.0
        out = _compute_indicators(df.copy(), ["close_in_range"])
        assert abs(out["CLOSE_IN_RANGE"].iloc[4] - 0.5) < 1e-9

    def test_zero_range_bar_default_is_0_5(self):
        df = _make_df(5)
        df.loc[4, "open"] = 100.0
        df.loc[4, "high"] = 100.0
        df.loc[4, "low"] = 100.0
        df.loc[4, "close"] = 100.0
        out = _compute_indicators(df.copy(), ["close_in_range"])
        # Degenerate: division-by-zero → fillna(0.5)
        assert out["CLOSE_IN_RANGE"].iloc[4] == 0.5


class TestVolumeZscore:
    def test_volume_zscore_parameterized(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["volume_zscore_30"])
        assert "VOLUME_Z_30" in out.columns

    def test_volume_zscore_different_n_produces_different_columns(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["volume_zscore_10", "volume_zscore_30"])
        assert "VOLUME_Z_10" in out.columns
        assert "VOLUME_Z_30" in out.columns

    def test_volume_zscore_statistics(self):
        # Constant volume → z-score undefined (std=0); after fillna should be 0
        df = _make_df(50)
        df["volume"] = 500.0
        out = _compute_indicators(df.copy(), ["volume_zscore_20"])
        assert "VOLUME_Z_20" in out.columns
        # All z-scores should be 0 (constant series → std 0 → fillna 0)
        assert out["VOLUME_Z_20"].abs().max() < 1e-9


class TestDollarVolume:
    def test_dollar_volume_rolling_sum(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["dollar_volume_10"])
        assert "DOLLAR_VOL_10" in out.columns
        # Manual rolling sum check on last bar
        expected = (df["close"] * df["volume"]).iloc[40:50].sum()
        assert abs(out["DOLLAR_VOL_10"].iloc[49] - expected) < 1e-6

    def test_dollar_volume_warmup_rows_are_zero(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["dollar_volume_20"])
        # dollar_volume_20 needs 20+5=25 rows so runs; first 19 rows are NaN → filled to 0
        assert out["DOLLAR_VOL_20"].iloc[0] == 0.0


class TestParserBackwardCompat:
    """Parser change (integer-suffix detection) should not break existing names."""

    def test_ema_9_still_works(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["ema_9"])
        assert "EMA_9" in out.columns

    def test_bbands_20_still_works(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["bbands_20"])
        assert "BBU_20" in out.columns
        assert "BBM_20" in out.columns
        assert "BBL_20" in out.columns

    def test_macd_no_length_still_works(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["macd"])
        assert "MACD" in out.columns

    def test_adx_14_still_works(self):
        df = _make_df(50)
        out = _compute_indicators(df.copy(), ["adx_14"])
        assert "ADX_14" in out.columns
