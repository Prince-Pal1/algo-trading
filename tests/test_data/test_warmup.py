"""Tests for warmup module — needed_pairs filtering, callback restoration,
recency gate (2026-06-11 stale-cache bug), and phantom-signal suppression.

Recency-gate context: warmup used to check ONLY `len(df) < min_candles`
before trusting the parquet cache. A cache frozen on 2026-04-17 kept
passing the row-count check for two months, priming vol_momentum's
168-bar momentum deque with April prices on every engine restart —
live "momentum" became the price gap to April and locked the strategy
permanently short. See docs/investigations/
2026-06-11_vol_momentum_stale_warmup.md.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.feature_engine import FeatureEngine
from src.data.warmup import warmup
from src.strategies.router import StrategyRouter
from src.utils.types import Candle


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ohlcv_df(n: int = 200, end_age_min: float = 0.0) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame whose newest candle is
    `end_age_min` minutes old (default: fresh as of now)."""
    end_ms = int((time.time() - end_age_min * 60) * 1000)
    return pd.DataFrame({
        "open": [100 + i * 0.1 for i in range(n)],
        "high": [102 + i * 0.1 for i in range(n)],
        "low": [98 + i * 0.1 for i in range(n)],
        "close": [101 + i * 0.1 for i in range(n)],
        "volume": [1000 + i for i in range(n)],
        "timestamp": [end_ms - (n - 1 - i) * 3_600_000 for i in range(n)],
    })


def _mock_downloader_cls(download_result: pd.DataFrame | Exception):
    """Build a mocked BinanceDownloader class."""
    inst = MagicMock()
    if isinstance(download_result, Exception):
        inst.download = AsyncMock(side_effect=download_result)
    else:
        inst.download = AsyncMock(return_value=download_result)
    inst.close = AsyncMock()
    cls = MagicMock(return_value=inst)
    return cls, inst


# ── Tests ────────────────────────────────────────────────────────────────────

class TestNeededPairsFilters:
    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_needed_pairs_filters(self, mock_parquet_cls, mock_dl_cls):
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

    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_cartesian_fallback(self, mock_parquet_cls, mock_dl_cls):
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
    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_callback_restored(self, mock_parquet_cls, mock_dl_cls):
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

    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_callback_restored_on_error(self, mock_parquet_cls, mock_dl_cls):
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


class TestRecencyGate:
    """The 2026-06-11 bug class: enough rows is NOT enough — rows must be recent."""

    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_fresh_cache_skips_download(self, mock_parquet_cls, mock_dl_cls):
        """A fresh full cache is used directly; downloader never constructed."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200, end_age_min=60)
        mock_parquet_cls.return_value = mock_store

        result = await warmup(
            FeatureEngine(indicators=["ema_9"]), StrategyRouter(),
            symbols=["ethusdt"], timeframes=["1h"],
            needed_pairs={("ethusdt", "1h")},
        )

        assert result["ETHUSDT_1h"] == 200
        mock_dl_cls.assert_not_called()

    @patch("src.data.warmup.ParquetStore")
    async def test_stale_cache_triggers_download_and_persist(self, mock_parquet_cls):
        """A stale-but-full cache must NOT be trusted: fresh data is
        downloaded, used, and persisted back to parquet."""
        mock_store = MagicMock()
        # 2 months stale — the exact April-cache scenario
        mock_store.load.return_value = _make_ohlcv_df(200, end_age_min=60 * 24 * 60)
        mock_parquet_cls.return_value = mock_store

        fresh = _make_ohlcv_df(200, end_age_min=60)
        dl_cls, dl = _mock_downloader_cls(fresh)

        with patch("src.data.warmup.BinanceDownloader", dl_cls):
            result = await warmup(
                FeatureEngine(indicators=["ema_9"]), StrategyRouter(),
                symbols=["ethusdt"], timeframes=["1h"],
                needed_pairs={("ethusdt", "1h")},
            )

        assert result["ETHUSDT_1h"] == 200
        dl.download.assert_awaited_once()
        # Downloaded data persisted back so next restart has a fresh cache
        mock_store.save.assert_called_once()
        saved_df = mock_store.save.call_args[0][0]
        assert saved_df["timestamp"].max() == fresh["timestamp"].max()

    @patch("src.data.warmup.ParquetStore")
    async def test_download_fail_within_tolerance_uses_cache(self, mock_parquet_cls):
        """Download unavailable + cache moderately stale (weekend gap) →
        cache is tolerated. XAUUSD Sunday-relaunch path."""
        mock_store = MagicMock()
        # 49h stale ≈ gold weekend closure; default tolerance floor is 60h
        mock_store.load.return_value = _make_ohlcv_df(200, end_age_min=49 * 60)
        mock_parquet_cls.return_value = mock_store

        dl_cls, dl = _mock_downloader_cls(RuntimeError("not on binance"))

        with patch("src.data.warmup.BinanceDownloader", dl_cls):
            result = await warmup(
                FeatureEngine(indicators=["ema_9"]), StrategyRouter(),
                symbols=["xauusd"], timeframes=["1h"],
                needed_pairs={("xauusd", "1h")},
            )

        assert result["XAUUSD_1h"] == 200

    @patch("src.data.warmup.ParquetStore")
    async def test_download_fail_beyond_tolerance_skips_warmup(self, mock_parquet_cls):
        """Download unavailable + cache too old → warmup SKIPPED (never feed
        poison). Strategies fill from live candles only."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200, end_age_min=60 * 24 * 60)
        mock_parquet_cls.return_value = mock_store

        dl_cls, dl = _mock_downloader_cls(RuntimeError("not on binance"))

        with patch("src.data.warmup.BinanceDownloader", dl_cls):
            result = await warmup(
                FeatureEngine(indicators=["ema_9"]), StrategyRouter(),
                symbols=["xauusd"], timeframes=["1h"],
                needed_pairs={("xauusd", "1h")},
            )

        assert result["XAUUSD_1h"] == 0

    @patch("src.data.warmup.ParquetStore")
    async def test_short_cache_download_fail_skips(self, mock_parquet_cls):
        """Cache below min_candles + download fail → skip (legacy behavior)."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(50, end_age_min=60)
        mock_parquet_cls.return_value = mock_store

        dl_cls, dl = _mock_downloader_cls(RuntimeError("network down"))

        with patch("src.data.warmup.BinanceDownloader", dl_cls):
            result = await warmup(
                FeatureEngine(indicators=["ema_9"]), StrategyRouter(),
                symbols=["ethusdt"], timeframes=["1h"],
                needed_pairs={("ethusdt", "1h")},
            )

        assert result["ETHUSDT_1h"] == 0

    @patch("src.data.warmup.ParquetStore")
    async def test_stale_tolerance_override(self, mock_parquet_cls):
        """Explicit stale_tolerance_min overrides the default floor."""
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200, end_age_min=10 * 60)
        mock_parquet_cls.return_value = mock_store

        dl_cls, dl = _mock_downloader_cls(RuntimeError("down"))

        with patch("src.data.warmup.BinanceDownloader", dl_cls):
            result = await warmup(
                FeatureEngine(indicators=["ema_9"]), StrategyRouter(),
                symbols=["ethusdt"], timeframes=["1h"],
                needed_pairs={("ethusdt", "1h")},
                stale_tolerance_min=5 * 60,  # 5h — tighter than the 10h age
            )

        assert result["ETHUSDT_1h"] == 0


class TestWarmupSignalSuppression:
    """Warmup replay must not write phantom rows to the live signals table."""

    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_no_signal_logging_during_warmup(self, mock_parquet_cls, mock_dl_cls):
        mock_store = MagicMock()
        mock_store.load.return_value = _make_ohlcv_df(200)
        mock_parquet_cls.return_value = mock_store

        storage = MagicMock()
        storage.trade_log.log_signal = AsyncMock()
        router = StrategyRouter(storage=storage)

        # Strategy that fires a signal on every candle during replay
        from src.utils.types import Signal, SignalAction
        strategy = MagicMock()
        strategy.name = "always_long"
        strategy.markets = ["ETHUSDT"]
        strategy.timeframe = "1h"
        strategy.process.return_value = Signal(
            symbol="ETHUSDT", action=SignalAction.LONG,
            confidence=1.0, strategy_name="always_long", timeframe="1h",
        )
        router.register(strategy)

        engine = FeatureEngine(indicators=["ema_9"])
        await warmup(
            engine, router,
            symbols=["ethusdt"], timeframes=["1h"],
            needed_pairs={("ethusdt", "1h")},
        )

        # Strategy state was warmed (process called) but nothing was logged
        assert strategy.process.called
        storage.trade_log.log_signal.assert_not_awaited()
        # Storage reattached for live operation
        assert router._storage is storage

    @patch("src.data.warmup.BinanceDownloader")
    @patch("src.data.warmup.ParquetStore")
    async def test_storage_restored_on_error(self, mock_parquet_cls, mock_dl_cls):
        """Router storage is reattached even if warmup raises."""
        mock_store = MagicMock()
        mock_store.load.side_effect = Exception("parquet error")
        mock_parquet_cls.return_value = mock_store

        storage = MagicMock()
        router = StrategyRouter(storage=storage)

        with pytest.raises(Exception, match="parquet error"):
            await warmup(
                FeatureEngine(indicators=["ema_9"]), router,
                symbols=["ethusdt"], timeframes=["1h"],
                needed_pairs={("ethusdt", "1h")},
            )

        assert router._storage is storage
