"""Phase 4A — Indicator edge case stress tests.

Tests that indicators handle extreme/unusual market data without crashing.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.data.feature_engine import _compute_indicators


def _make_df(closes, *, highs=None, lows=None, opens=None, volumes=None):
    n = len(closes)
    return pd.DataFrame({
        "open": opens or closes,
        "high": highs or closes,
        "low": lows or closes,
        "close": closes,
        "volume": volumes or [1000.0] * n,
        "timestamp": list(range(n)),
    })


ALL_INDICATORS = [
    "ema_9", "ema_21", "sma_20", "rsi_14", "bbands_20",
    "macd", "atr_14", "adx_14", "donchian_20", "stoch_14", "vwap",
]


class TestIndicatorEdgeCases:

    def test_zero_volume_vwap(self):
        """Zero volume → VWAP should handle gracefully (try/except in engine)."""
        closes = [100 + i for i in range(50)]
        highs = [c + 2 for c in closes]
        lows = [c - 2 for c in closes]
        volumes = [0.0] * 50  # ALL zero volume

        df = _make_df(closes, highs=highs, lows=lows, volumes=volumes)
        # Should not raise — VWAP has try/except
        df = _compute_indicators(df, ["vwap"])
        # VWAP column may or may not exist (the try/except may suppress it)
        # Just verify no crash

    def test_price_gap_spike(self):
        """Price jumps 100→500 in one bar → ATR spikes, BBands expand, no crash."""
        closes = [100.0] * 30 + [500.0] + [100.0] * 19
        highs = [c + 5 for c in closes]
        lows = [c - 5 for c in closes]

        df = _make_df(closes, highs=highs, lows=lows)
        df = _compute_indicators(df, ALL_INDICATORS)

        # ATR should spike after the gap
        atr_at_gap = df["ATR_14"].iloc[31]
        atr_before = df["ATR_14"].iloc[29]
        if pd.notna(atr_at_gap) and pd.notna(atr_before):
            assert atr_at_gap > atr_before, "ATR should spike on price gap"

    def test_flat_market_no_division_by_zero(self):
        """All closes identical → RSI should be NaN or ~50, no division by zero."""
        df = _make_df([100.0] * 50)
        df = _compute_indicators(df, ["rsi_14", "bbands_20", "stoch_14"])

        # RSI with no gains or losses — should be NaN or 0 or 50, not crash
        rsi = df["RSI_14"].iloc[-1]
        assert pd.isna(rsi) or (0 <= rsi <= 100), f"Flat RSI should be NaN or bounded, got {rsi}"

    def test_very_small_prices(self):
        """Micro-prices (0.00001) → no inf/overflow."""
        closes = [0.00001 + i * 0.000001 for i in range(50)]
        highs = [c * 1.01 for c in closes]
        lows = [c * 0.99 for c in closes]

        df = _make_df(closes, highs=highs, lows=lows)
        df = _compute_indicators(df, ["ema_9", "rsi_14", "atr_14", "bbands_20"])

        # No inf values
        for col in df.columns:
            if col in ("timestamp",):
                continue
            series = df[col].dropna()
            if len(series) > 0:
                assert not series.isin([float("inf"), float("-inf")]).any(), \
                    f"Column {col} has inf values with micro-prices"

    def test_very_large_prices(self):
        """Large prices (100000+) → no underflow."""
        closes = [100000 + i * 100 for i in range(50)]
        highs = [c + 200 for c in closes]
        lows = [c - 200 for c in closes]

        df = _make_df(closes, highs=highs, lows=lows)
        df = _compute_indicators(df, ["ema_9", "rsi_14", "atr_14", "bbands_20"])

        # Values should be finite
        for col in ["EMA_9", "RSI_14", "ATR_14", "BBU_20"]:
            if col in df.columns:
                val = df[col].iloc[-1]
                if pd.notna(val):
                    assert math.isfinite(val), f"{col} is not finite: {val}"

    def test_nan_in_close_column(self):
        """NaN in close → indicators should handle gracefully."""
        closes = [100 + i for i in range(50)]
        closes[25] = float("nan")  # One NaN in the middle

        df = _make_df(closes)
        # Should not crash
        df = _compute_indicators(df, ["ema_9", "rsi_14", "sma_20"])

    def test_single_row_skipped(self):
        """Single-row DataFrame → min_rows guard prevents computation."""
        df = _make_df([100.0])
        df = _compute_indicators(df, ALL_INDICATORS)
        # No indicator columns should be added (1 row < any min_rows)
        indicator_cols = [c for c in df.columns
                         if c not in ("open", "high", "low", "close", "volume", "timestamp")]
        assert len(indicator_cols) == 0, \
            f"Single-row DF should have no indicators, got {indicator_cols}"

    def test_all_indicators_together(self):
        """All indicators computed on same data → no interference."""
        closes = [100 + 5 * math.sin(i * 0.3) + i * 0.1 for i in range(60)]
        highs = [c + 3 for c in closes]
        lows = [c - 3 for c in closes]

        df = _make_df(closes, highs=highs, lows=lows, volumes=[1000 + i * 10 for i in range(60)])
        df = _compute_indicators(df, ALL_INDICATORS)

        # Verify each indicator produces non-NaN on the last row
        expected_cols = ["EMA_9", "EMA_21", "SMA_20", "RSI_14", "BBU_20",
                         "MACD", "ATR_14"]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"
            assert pd.notna(df[col].iloc[-1]), f"{col} is NaN on last row"
