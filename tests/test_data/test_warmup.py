"""Tests for warmup module — needed_pairs filtering, callback restoration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.feature_engine import FeatureEngine
from src.data.warmup import warmup
from src.strategies.router import StrategyRouter
from src.utils.types import Candle


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ohlcv_df(n: int = 200) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame for warmup."""
    return pd.DataFrame({
        "open": [100 + i * 0.1 for i in range(n)],
        "high": [102 + i * 0.1 for i in range(n)],
        "low": [98 + i * 0.1 for i in range(n)],
        "close": [101 + i * 0.1 for i in range(n)],
        "volume": [1000 + i for i in range(n)],
        "timestamp": [i * 3_600_000 for i in range(n)],
    })


# ── Tests ────────────────────────────────────────────────────────────────────

class TestNeededPairsFilters:
    @patch("src.data.warmup.ParquetStore")
    async def test_needed_pairs_filters(self, mock_parquet_cls):
        """With needed_pairs={(ETH,1h)}, only ETH_1h is warmed up."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200)
        mock_parquet_cls.return_value = mock_store

        engine = FeatureEngine(indicators=["ema_9"])
        router = StrategyRouter()

        needed = {("ethusdt", "1h")}
        result = await warmup(
            engine, router,
            symbols=["ethusdt", "btcusdt"],
            timeframes=["1h", "5m"],
            needed_pairs=needed,
        )

        # Only ETH_1h should have been warmed up
        assert "ETHUSDT_1h" in result
        assert result["ETHUSDT_1h"] == 200
        # BTC and 5m should NOT be in result
        assert "BTCUSDT_1h" not in result
        assert "ETHUSDT_5m" not in result

    @patch("src.data.warmup.ParquetStore")
    async def test_cartesian_fallback(self, mock_parquet_cls):
        """Without needed_pairs, all symbol x tf combos warmed."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200)
        mock_parquet_cls.return_value = mock_store

        engine = FeatureEngine(indicators=["ema_9"])
        router = StrategyRouter()

        result = await warmup(
            engine, router,
            symbols=["ethusdt", "btcusdt"],
            timeframes=["1h"],
            needed_pairs=None,
        )

        assert "ETHUSDT_1h" in result
        assert "BTCUSDT_1h" in result


class TestCallbackRestored:
    @patch("src.data.warmup.ParquetStore")
    async def test_callback_restored(self, mock_parquet_cls):
        """feature_engine.on_features is restored to original after warmup."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200)
        mock_parquet_cls.return_value = mock_store

        engine = FeatureEngine(indicators=["ema_9"])
        router = StrategyRouter()

        original = AsyncMock()
        engine.on_features = original

        await warmup(
            engine, router,
            symbols=["ethusdt"],
            timeframes=["1h"],
            needed_pairs={("ethusdt", "1h")},
        )

        # Original callback must be restored after warmup
        assert engine.on_features is original

    @patch("src.data.warmup.ParquetStore")
    async def test_callback_restored_on_error(self, mock_parquet_cls):
        """Callback restored even if warmup raises an exception."""
        mock_store = MagicMock()
        mock_store.load.side_effect = Exception("parquet error")
        mock_parquet_cls.return_value = mock_store

        engine = FeatureEngine(indicators=["ema_9"])
        router = StrategyRouter()

        original = AsyncMock()
        engine.on_features = original

        # parquet.load() raises before the try/except download fallback,
        # so the exception propagates — but finally block restores callback
        with pytest.raises(Exception, match="parquet error"):
            await warmup(
                engine, router,
                symbols=["ethusdt"],
                timeframes=["1h"],
                needed_pairs={("ethusdt", "1h")},
            )

        assert engine.on_features is original
