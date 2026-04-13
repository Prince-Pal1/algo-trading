"""Tests for src/data/funding_synthetic.py — Strategy A sub-phase A.2 gate.

The gate: `cumulative_return[0..n] = Σ (funding_rate[i] − friction)` within
1e-9 for a 180-day synthetic series.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.data.funding_synthetic import (
    SyntheticSeriesConfig,
    build_synthetic_series,
    cumulative_log_return,
    load_funding_parquet,
    load_synthetic_series,
)


_MS_PER_EPOCH = 8 * 3600 * 1000  # 8 hours in ms


def _make_funding_df(n: int, rate: float, start_ms: int = 1_700_000_000_000) -> pd.DataFrame:
    """Build a synthetic funding-rate DataFrame with constant rate."""
    return pd.DataFrame({
        "timestamp": [start_ms + i * _MS_PER_EPOCH for i in range(n)],
        "funding_rate": [rate] * n,
        "mark_price": [0.0] * n,
        "symbol": ["BTCUSDT"] * n,
    })


class TestBuildSyntheticSeries:
    def test_empty_input_returns_empty(self):
        empty = pd.DataFrame(columns=["timestamp", "funding_rate"])
        result = build_synthetic_series(empty)
        assert len(result) == 0
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_missing_columns_raises(self):
        bad = pd.DataFrame({"foo": [1, 2, 3]})
        with pytest.raises(ValueError, match="missing columns"):
            build_synthetic_series(bad)

    def test_zero_friction_equals_cumulative_funding(self):
        """The gate test: with zero friction, cumulative log-return must
        equal Σ log(1 + funding_rate) within 1e-9.

        180 days × 3 epochs/day = 540 epochs.
        """
        n_epochs = 540
        funding_rate = 0.0001  # 0.01% per 8h — typical BTCUSDT
        fdf = _make_funding_df(n_epochs, funding_rate)

        config = SyntheticSeriesConfig(
            start_price=100.0, friction_pct=0.0, volume_placeholder=1.0,
        )
        series = build_synthetic_series(fdf, config=config)

        assert len(series) == n_epochs

        expected_log_return = sum(math.log(1.0 + funding_rate) for _ in range(n_epochs))
        actual_log_return = cumulative_log_return(series)
        assert abs(actual_log_return - expected_log_return) < 1e-9

    def test_ohlcv_shape(self):
        fdf = _make_funding_df(10, 0.0001)
        series = build_synthetic_series(fdf)
        for col in ("timestamp", "open", "high", "low", "close", "volume"):
            assert col in series.columns
        # On the synthetic hedged pair, open == high == low == close (zero intra-bar drift).
        for i in range(len(series)):
            assert series["open"].iloc[i] == series["close"].iloc[i]
            assert series["high"].iloc[i] == series["close"].iloc[i]
            assert series["low"].iloc[i] == series["close"].iloc[i]
            assert series["volume"].iloc[i] > 0

    def test_friction_reduces_cumulative_return(self):
        """Nonzero friction must produce strictly lower cumulative return
        than zero-friction for the same funding stream."""
        n_epochs = 200
        funding_rate = 0.0001
        fdf = _make_funding_df(n_epochs, funding_rate)

        series_no_friction = build_synthetic_series(
            fdf, config=SyntheticSeriesConfig(friction_pct=0.0),
        )
        series_with_friction = build_synthetic_series(
            fdf, config=SyntheticSeriesConfig(friction_pct=0.00005),
        )

        # 0.00005 < 0.0001 so net is still positive but smaller
        final_no_friction = float(series_no_friction["close"].iloc[-1])
        final_with_friction = float(series_with_friction["close"].iloc[-1])
        assert final_with_friction < final_no_friction
        assert final_with_friction > 100.0  # still positive net

    def test_friction_larger_than_funding_produces_decay(self):
        """When friction exceeds funding, the synthetic series should decay."""
        fdf = _make_funding_df(100, 0.0001)
        series = build_synthetic_series(
            fdf, config=SyntheticSeriesConfig(friction_pct=0.0005),
        )
        # Net per-epoch = 0.0001 - 0.0005 = -0.0004
        # After 100 epochs, close = 100 × (1 - 0.0004)^100 ≈ 96.08
        final = float(series["close"].iloc[-1])
        assert final < 100.0
        assert final > 95.0  # sanity

    def test_timestamp_preserved(self):
        start_ms = 1_700_000_000_000
        fdf = _make_funding_df(5, 0.0001, start_ms=start_ms)
        series = build_synthetic_series(fdf)
        for i in range(5):
            assert int(series["timestamp"].iloc[i]) == start_ms + i * _MS_PER_EPOCH


class TestLoadFromParquet:
    def test_load_funding_parquet_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_funding_parquet("NONEXISTENT", funding_dir=tmp_path)

    def test_load_synthetic_series_roundtrip(self, tmp_path):
        # Write a funding Parquet to the tmp dir
        fdf = _make_funding_df(50, 0.00012)
        path = tmp_path / "BTCUSDT_8h.parquet"
        pq.write_table(pa.Table.from_pandas(fdf), path, compression="snappy")

        series = load_synthetic_series(
            "BTCUSDT", friction_pct=0.0, funding_dir=tmp_path,
        )
        assert len(series) == 50
        # With zero friction: final close = 100 × (1.00012)^50
        expected_final = 100.0 * (1.00012 ** 50)
        assert abs(float(series["close"].iloc[-1]) - expected_final) < 1e-6
