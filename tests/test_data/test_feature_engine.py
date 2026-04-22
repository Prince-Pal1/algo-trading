"""Tests for FeatureEngine — indicator computation, buffering, edge cases."""

from __future__ import annotations

import warnings

import pandas as pd
import pytest
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

from src.data.feature_engine import (
    COMPUTE_WINDOW,
    MAX_BUFFER_SIZE,
    FeatureEngine,
    _compute_indicators,
)
from src.utils.types import Candle


# ── Helpers ──────────────────────────────────────────────────────────────────

def _candle(symbol: str, tf: str, close: float, idx: int) -> Candle:
    """Create a synthetic candle with incrementing data."""
    return Candle(
        symbol=symbol, timeframe=tf,
        open=close - 1, high=close + 1, low=close - 2,
        close=close, volume=100.0 + idx,
        timestamp=idx * 3_600_000,  # 1h spacing
        closed=True,
    )


class FeatureCollector:
    """Async callback that collects emitted features."""
    def __init__(self):
        self.features: list[tuple[str, str, pd.Series]] = []

    async def __call__(self, symbol: str, tf: str, row: pd.Series) -> None:
        self.features.append((symbol, tf, row))


# ── Tests ────────────────────────────────────────────────────────────────────

class TestHandleCandleEmitsFeatures:
    async def test_handle_candle_emits_features(self):
        """One candle → on_features callback fires with pd.Series."""
        engine = FeatureEngine(indicators=["ema_9"])
        collector = FeatureCollector()
        engine.on_features = collector

        await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0, 0))

        assert len(collector.features) == 1
        sym, tf, row = collector.features[0]
        assert sym == "BTCUSDT"
        assert tf == "1h"
        assert isinstance(row, pd.Series)
        assert "close" in row.index


class TestBufferGrowsAndTrims:
    async def test_buffer_grows_and_trims(self):
        """Buffer grows to MAX_BUFFER_SIZE then stays at 500."""
        engine = FeatureEngine(indicators=["ema_9"])

        for i in range(MAX_BUFFER_SIZE + 50):
            await engine.handle_candle(_candle("ETHUSDT", "1h", 100.0 + i * 0.1, i))

        df = engine.get_dataframe("ETHUSDT", "1h")
        assert len(df) == MAX_BUFFER_SIZE


class TestIndicatorsUpdateWithNewData:
    async def test_indicators_update_with_new_data(self):
        """RSI_14 changes when a new candle is added (not stale/cached)."""
        engine = FeatureEngine(indicators=["rsi_14"])
        collector = FeatureCollector()
        engine.on_features = collector

        # Feed 30 candles with zigzag prices (mix of ups and downs)
        # so RSI is in the middle range, not pinned at 0 or 100
        import math
        for i in range(30):
            price = 100.0 + 5 * math.sin(i * 0.5)  # oscillates 95-105
            await engine.handle_candle(_candle("BTCUSDT", "1h", price, i))

        rsi_before = collector.features[-1][2].get("RSI_14")

        # Feed a big upward candle — RSI should increase
        await engine.handle_candle(_candle("BTCUSDT", "1h", 150.0, 30))
        rsi_after = collector.features[-1][2].get("RSI_14")

        assert pd.notna(rsi_before)
        assert pd.notna(rsi_after)
        assert rsi_after != rsi_before
        assert rsi_after > rsi_before  # Big up move → higher RSI


class TestMinRowsGuardSkipsEarly:
    async def test_min_rows_guard_skips_early(self):
        """With 10 candles, ADX_14 column is absent (needs 33+ rows)."""
        engine = FeatureEngine(indicators=["adx_14"])
        collector = FeatureCollector()
        engine.on_features = collector

        for i in range(10):
            await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0 + i, i))

        row = collector.features[-1][2]
        # ADX needs 2*14+5 = 33 rows minimum, so with only 10 it should be absent
        assert "ADX_14" not in row.index or pd.isna(row.get("ADX_14"))


class TestComputeWindowMatchesFull:
    async def test_compute_window_matches_full(self):
        """With 400 rows: COMPUTE_WINDOW=250 tail vs full-buffer gives same last-row values."""
        indicators = ["ema_9", "ema_21", "rsi_14"]

        # Build a reference DataFrame with 400 rows
        data = {
            "open": [100 + i * 0.1 for i in range(400)],
            "high": [102 + i * 0.1 for i in range(400)],
            "low": [98 + i * 0.1 for i in range(400)],
            "close": [101 + i * 0.1 for i in range(400)],
            "volume": [1000 + i for i in range(400)],
            "timestamp": [i * 3_600_000 for i in range(400)],
        }
        full_df = pd.DataFrame(data)
        tail_df = full_df.iloc[-COMPUTE_WINDOW:].copy().reset_index(drop=True)

        full_computed = _compute_indicators(full_df.copy(), indicators)
        tail_computed = _compute_indicators(tail_df.copy(), indicators)

        full_last = full_computed.iloc[-1]
        tail_last = tail_computed.iloc[-1]

        for col in ["EMA_9", "EMA_21", "RSI_14"]:
            if pd.notna(full_last.get(col)) and pd.notna(tail_last.get(col)):
                diff_pct = abs(full_last[col] - tail_last[col]) / abs(full_last[col]) * 100
                assert diff_pct < 0.01, f"{col} diverged: full={full_last[col]}, tail={tail_last[col]}, diff={diff_pct:.4f}%"


class TestInplaceAppendNoWarning:
    async def test_inplace_append_no_warning(self):
        """No FutureWarning from deprecated pd.concat (we use df.loc[] now)."""
        engine = FeatureEngine(indicators=["ema_9"])

        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            for i in range(20):
                await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0 + i, i))


class TestNanHandling:
    async def test_nan_handling(self):
        """Features with NaN indicators don't crash downstream."""
        engine = FeatureEngine(indicators=["ema_9", "rsi_14", "adx_14"])
        collector = FeatureCollector()
        engine.on_features = collector

        # Only 5 candles — most indicators will be NaN
        for i in range(5):
            await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0 + i, i))

        assert len(collector.features) == 5
        row = collector.features[-1][2]
        # Should have close but indicators should be NaN or absent (not crash)
        assert row["close"] == 104.0


class TestTimestampDedup:
    """Regression tests for the 2026-04-22 cTrader partial-bar fix —
    defense-in-depth at the FeatureEngine layer.
    """

    async def test_duplicate_timestamp_dropped(self):
        """Same (symbol, tf, timestamp) fed twice → second one silently dropped."""
        engine = FeatureEngine(indicators=["ema_9"])
        collector = FeatureCollector()
        engine.on_features = collector

        c = _candle("BTCUSDT", "1h", 100.0, idx=5)  # ts = 18_000_000
        await engine.handle_candle(c)
        assert len(collector.features) == 1
        assert engine._dedup_dropped_count == 0

        # Second emission with same timestamp
        await engine.handle_candle(c)
        assert len(collector.features) == 1  # not emitted
        assert engine._dedup_dropped_count == 1

    async def test_backwards_timestamp_dropped(self):
        """A candle with ts < last is dropped, not treated as a re-emission."""
        engine = FeatureEngine(indicators=["ema_9"])
        collector = FeatureCollector()
        engine.on_features = collector

        await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0, idx=10))  # ts=36M
        await engine.handle_candle(_candle("BTCUSDT", "1h", 99.0, idx=5))    # ts=18M < 36M
        assert len(collector.features) == 1
        assert engine._dedup_dropped_count == 1

    async def test_different_pairs_have_independent_dedup(self):
        """Dedup is per-(symbol, tf) — BTCUSDT ts=X doesn't block ETHUSDT ts=X."""
        engine = FeatureEngine(indicators=["ema_9"])
        collector = FeatureCollector()
        engine.on_features = collector

        await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0, idx=5))
        await engine.handle_candle(_candle("ETHUSDT", "1h", 200.0, idx=5))  # same ts, different symbol
        assert len(collector.features) == 2
        assert engine._dedup_dropped_count == 0

        # But a second BTCUSDT at idx=5 IS dropped
        await engine.handle_candle(_candle("BTCUSDT", "1h", 101.0, idx=5))
        assert len(collector.features) == 2
        assert engine._dedup_dropped_count == 1

    async def test_different_timeframes_have_independent_dedup(self):
        """Same symbol, different timeframe → independent dedup."""
        engine = FeatureEngine(indicators=["ema_9"])
        collector = FeatureCollector()
        engine.on_features = collector

        await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0, idx=5))
        # Same symbol + ts but different tf → not a duplicate
        await engine.handle_candle(_candle("BTCUSDT", "1m", 100.0, idx=5))
        assert len(collector.features) == 2
        assert engine._dedup_dropped_count == 0

    async def test_monotonic_progression_all_emit(self):
        """Strictly increasing timestamps all pass through."""
        engine = FeatureEngine(indicators=["ema_9"])
        collector = FeatureCollector()
        engine.on_features = collector

        for i in range(10):
            await engine.handle_candle(_candle("BTCUSDT", "1h", 100.0 + i, i))
        assert len(collector.features) == 10
        assert engine._dedup_dropped_count == 0
