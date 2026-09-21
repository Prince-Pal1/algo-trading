"""Tests for src/data/depth_recorder.py — sampling, storage, heatmap gridding.

The load-bearing test is `test_multiple_levels_per_timestamp_survive`: depth
writes many rows sharing one timestamp, and the project's usual `ParquetStore`
deduplicates on timestamp alone. Routing depth through that store would keep
one level per sample and silently discard the rest.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.depth_recorder import (
    DEPTH_COLUMNS,
    DepthRecorder,
    DepthStore,
    infer_tick_size,
    to_heatmap_grid,
)
from src.utils.types import OrderBookLevel, OrderBookSnapshot

_TS = 1_757_000_000_000


def _snapshot(ts: int, mid: float = 100.0, levels: int = 3) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=[OrderBookLevel(price=round(mid - i * 0.01, 4), quantity=10.0 + i)
              for i in range(1, levels + 1)],
        asks=[OrderBookLevel(price=round(mid + i * 0.01, 4), quantity=5.0 + i)
              for i in range(1, levels + 1)],
        timestamp=ts,
    )


def _rows(n_samples: int = 3, levels: int = 2, step_ms: int = 1000) -> pd.DataFrame:
    rec = DepthRecorder("BTCUSDT", sample_ms=step_ms, depth=levels)
    for i in range(n_samples):
        rec.record(_snapshot(_TS + i * step_ms, mid=100.0 + i * 0.01, levels=levels))
    return rec.drain()


class TestDepthRecorder:
    def test_rejects_bad_sample_ms(self):
        with pytest.raises(ValueError, match="sample_ms"):
            DepthRecorder("BTCUSDT", sample_ms=0)

    def test_rejects_bad_depth(self):
        with pytest.raises(ValueError, match="depth"):
            DepthRecorder("BTCUSDT", depth=0)

    def test_symbol_uppercased(self):
        assert DepthRecorder("btcusdt").symbol == "BTCUSDT"

    def test_first_snapshot_sampled(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1000)
        assert rec.record(_snapshot(_TS)) is True
        assert rec.samples == 1

    def test_snapshot_inside_interval_skipped(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1000)
        rec.record(_snapshot(_TS))
        assert rec.record(_snapshot(_TS + 500)) is False
        assert rec.samples == 1

    def test_snapshot_after_interval_sampled(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1000)
        rec.record(_snapshot(_TS))
        assert rec.record(_snapshot(_TS + 1000)) is True
        assert rec.samples == 2

    def test_none_snapshot_ignored(self):
        assert DepthRecorder("BTCUSDT").record(None) is False

    def test_unstamped_snapshot_ignored(self):
        """A book with no timestamp cannot be placed on a time axis."""
        rec = DepthRecorder("BTCUSDT")
        assert rec.record(_snapshot(0)) is False

    def test_depth_limit_applied_per_side(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1, depth=2)
        rec.record(_snapshot(_TS, levels=10))
        df = rec.drain()
        assert len(df) == 4  # 2 bids + 2 asks
        assert (df["side"] == "bid").sum() == 2

    def test_rows_carry_expected_columns(self):
        assert list(_rows().columns) == DEPTH_COLUMNS

    def test_timestamp_dtype_is_int(self):
        assert _rows()["timestamp"].dtype == "int64"

    def test_drain_clears_buffer(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1, depth=1)
        rec.record(_snapshot(_TS))
        assert not rec.drain().empty
        assert rec.pending_rows == 0
        assert rec.drain().empty

    def test_drain_on_empty_returns_typed_frame(self):
        df = DepthRecorder("BTCUSDT").drain()
        assert df.empty
        assert list(df.columns) == DEPTH_COLUMNS

    def test_should_flush_threshold(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1, depth=5, flush_rows=10)
        assert rec.should_flush is False
        rec.record(_snapshot(_TS, levels=5))
        assert rec.should_flush is True  # 10 rows

    def test_samples_counter_independent_of_rows(self):
        rec = DepthRecorder("BTCUSDT", sample_ms=1, depth=3)
        for i in range(4):
            rec.record(_snapshot(_TS + i))
        assert rec.samples == 4
        assert rec.pending_rows == 24


class TestDepthStore:
    def test_multiple_levels_per_timestamp_survive(self, tmp_path):
        """The whole reason DepthStore exists, rather than ParquetStore."""
        store = DepthStore(data_dir=str(tmp_path))
        df = _rows(n_samples=2, levels=3)
        per_ts = df.groupby("timestamp").size()
        assert (per_ts == 6).all()  # 3 bids + 3 asks share each timestamp

        store.save(df, "BTCUSDT")
        loaded = store.load("BTCUSDT")
        assert len(loaded) == len(df)
        assert loaded.groupby("timestamp").size().eq(6).all()

    def test_roundtrip_preserves_values(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        df = _rows()
        store.save(df, "BTCUSDT")
        loaded = store.load("BTCUSDT")
        assert set(loaded["side"]) == {"bid", "ask"}
        assert loaded["size"].sum() == pytest.approx(df["size"].sum())

    def test_append_accumulates(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        store.save(_rows(n_samples=2), "BTCUSDT")
        first = len(store.load("BTCUSDT"))
        later = _rows(n_samples=2)
        later["timestamp"] = later["timestamp"] + 60_000
        store.save(later, "BTCUSDT")
        assert len(store.load("BTCUSDT")) == first * 2

    def test_dedupe_on_composite_key(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        df = _rows(n_samples=1, levels=2)
        store.save(df, "BTCUSDT")
        store.save(df, "BTCUSDT")  # exact replay
        assert len(store.load("BTCUSDT")) == len(df)

    def test_resave_updates_size_in_place(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        df = _rows(n_samples=1, levels=1)
        store.save(df, "BTCUSDT")
        updated = df.copy()
        updated["size"] = 999.0
        store.save(updated, "BTCUSDT")
        loaded = store.load("BTCUSDT")
        assert len(loaded) == len(df)
        assert (loaded["size"] == 999.0).all()

    def test_empty_save_is_noop(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        store.save(pd.DataFrame(columns=DEPTH_COLUMNS), "BTCUSDT")
        assert store.exists("BTCUSDT") is False

    def test_load_missing_returns_typed_empty(self, tmp_path):
        df = DepthStore(data_dir=str(tmp_path)).load("NOPE")
        assert df.empty
        assert list(df.columns) == DEPTH_COLUMNS

    def test_list_symbols(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        store.save(_rows(), "BTCUSDT")
        store.save(_rows(), "ETHUSDT")
        assert store.list_symbols() == ["BTCUSDT", "ETHUSDT"]

    def test_symbol_case_normalized(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        store.save(_rows(), "btcusdt")
        assert store.exists("BTCUSDT") is True

    def test_loaded_rows_sorted_by_time(self, tmp_path):
        store = DepthStore(data_dir=str(tmp_path))
        store.save(_rows(n_samples=4), "BTCUSDT")
        assert store.load("BTCUSDT")["timestamp"].is_monotonic_increasing


class TestInferTickSize:
    def test_uniform_grid(self):
        assert infer_tick_size(pd.Series([100.00, 100.01, 100.02, 100.03])) == pytest.approx(0.01)

    def test_modal_gap_wins_over_outlier(self):
        prices = pd.Series([100.00, 100.01, 100.02, 100.50])
        assert infer_tick_size(prices) == pytest.approx(0.01)

    def test_too_few_prices_returns_none(self):
        assert infer_tick_size(pd.Series([100.0, 100.1])) is None

    def test_all_identical_returns_none(self):
        assert infer_tick_size(pd.Series([100.0, 100.0, 100.0])) is None


class TestHeatmapGrid:
    def test_empty_input(self):
        grid, note = to_heatmap_grid(pd.DataFrame(columns=DEPTH_COLUMNS))
        assert grid.empty
        assert "no depth" in note

    def test_rejects_bad_price_bins(self):
        with pytest.raises(ValueError, match="price_bins"):
            to_heatmap_grid(_rows(), price_bins=1)

    def test_tick_alignment_used_when_possible(self):
        grid, note = to_heatmap_grid(_rows(n_samples=3, levels=3), price_bins=200)
        assert "tick-aligned" in note
        assert not grid.empty

    def test_falls_back_to_equal_width_when_range_too_wide(self):
        df = _rows(n_samples=2, levels=2)
        df.loc[len(df)] = [df["timestamp"].iloc[0], 500.0, 1.0, "ask"]
        grid, note = to_heatmap_grid(df, price_bins=10)
        assert "equal-width" in note
        assert len(grid) <= 12

    def test_explicit_tick_size_respected(self):
        grid, note = to_heatmap_grid(_rows(levels=3), tick_size=0.05, price_bins=500)
        assert "tick-aligned at 0.05" in note

    def test_columns_are_timestamps(self):
        df = _rows(n_samples=3)
        grid, _ = to_heatmap_grid(df)
        assert list(grid.columns) == sorted(df["timestamp"].unique())

    def test_index_ascending_by_price(self):
        grid, _ = to_heatmap_grid(_rows(levels=4))
        assert list(grid.index) == sorted(grid.index)

    def test_sizes_summed_into_buckets(self):
        df = _rows(n_samples=1, levels=2)
        grid, _ = to_heatmap_grid(df, tick_size=1000.0)  # everything into one bucket
        assert grid.to_numpy().sum() == pytest.approx(df["size"].sum())

    def test_time_downsampling_caps_columns(self):
        grid, note = to_heatmap_grid(_rows(n_samples=20), time_bins=5)
        assert grid.shape[1] <= 6
        assert "downsampled" in note

    def test_no_downsampling_when_under_cap(self):
        grid, note = to_heatmap_grid(_rows(n_samples=3), time_bins=50)
        assert "downsampled" not in note

    def test_single_price_level(self):
        df = pd.DataFrame({
            "timestamp": [_TS, _TS + 1000],
            "price": [100.0, 100.0],
            "size": [5.0, 7.0],
            "side": ["bid", "bid"],
        })
        grid, note = to_heatmap_grid(df)
        assert "single price level" in note
        assert grid.shape == (1, 2)

    def test_empty_cells_are_nan_not_zero(self):
        """Absent liquidity must render as background, not as a thin level."""
        df = pd.DataFrame({
            "timestamp": [_TS, _TS + 1000],
            "price": [100.0, 101.0],
            "size": [5.0, 7.0],
            "side": ["bid", "bid"],
        })
        grid, _ = to_heatmap_grid(df, tick_size=1.0)
        assert grid.isna().to_numpy().any()
