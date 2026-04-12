"""ParquetStore + BinanceDownloader + warmup tests.

Covers the final gap from the Session 20 "test all" sweep:
- ParquetStore roundtrip / dedup / missing-file behavior
- BinanceDownloader pagination + date parsing (httpx mocked)
- warmup() prefers parquet, falls back to downloader, replays through engine
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from src.data.downloader import BinanceDownloader, _parse_date, _parse_klines
from src.data.storage import ParquetStore


def _make_df(start_ts: int, n: int) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [start_ts + i * 60_000 for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000.0] * n,
    })


# ── ParquetStore ───────────────────────────────────────────────────────────


class TestParquetStore:
    def test_save_load_roundtrip(self, tmp_path: Path):
        store = ParquetStore(data_dir=str(tmp_path))
        df = _make_df(1_700_000_000_000, 10)
        store.save(df, "BTCUSDT", "1m")

        loaded = store.load("BTCUSDT", "1m")
        assert len(loaded) == 10
        assert list(loaded["timestamp"]) == list(df["timestamp"])
        assert loaded["close"].iloc[0] == 100.5

    def test_save_dedupes_overlapping_timestamps(self, tmp_path: Path):
        store = ParquetStore(data_dir=str(tmp_path))
        first = _make_df(1_700_000_000_000, 5)
        store.save(first, "BTCUSDT", "1m")

        # Second batch overlaps: shares first 2 timestamps, adds 3 new
        second = _make_df(1_700_000_000_000 + 3 * 60_000, 5)
        store.save(second, "BTCUSDT", "1m")

        loaded = store.load("BTCUSDT", "1m")
        # 5 + 5 - 2 overlap = 8 unique timestamps
        assert len(loaded) == 8
        assert loaded["timestamp"].is_monotonic_increasing
        assert loaded["timestamp"].is_unique

    def test_load_missing_returns_empty_df(self, tmp_path: Path):
        store = ParquetStore(data_dir=str(tmp_path))
        df = store.load("NOPE", "1h")
        assert df.empty
        assert set(df.columns) >= {"open", "high", "low", "close", "volume", "timestamp"}

    def test_exists_and_list_files(self, tmp_path: Path):
        store = ParquetStore(data_dir=str(tmp_path))
        assert not store.exists("BTCUSDT", "1h")
        store.save(_make_df(0, 3), "BTCUSDT", "1h")
        store.save(_make_df(0, 3), "ETHUSDT", "5m")
        assert store.exists("BTCUSDT", "1h")
        files = set(store.list_files())
        assert {"BTCUSDT_1h", "ETHUSDT_5m"} <= files


# ── BinanceDownloader ──────────────────────────────────────────────────────


class TestDownloaderHelpers:
    def test_parse_date_utc_midnight(self):
        ms = _parse_date("2024-01-01")
        assert ms == 1_704_067_200_000  # 2024-01-01 00:00:00 UTC

    def test_parse_klines_shapes_dataframe(self):
        raw = [
            [1_700_000_000_000, "100.0", "101.0", "99.0", "100.5", "1000", 0, 0, 0, 0, 0, 0],
            [1_700_000_060_000, "100.5", "101.5", "99.5", "101.0", "1200", 0, 0, 0, 0, 0, 0],
        ]
        df = _parse_klines(raw)
        assert len(df) == 2
        assert df["close"].tolist() == [100.5, 101.0]
        assert df["timestamp"].tolist() == [1_700_000_000_000, 1_700_000_060_000]
        assert df["open"].dtype.kind == "f"


class TestBinanceDownloader:
    @pytest.mark.asyncio
    async def test_download_paginates_until_end(self):
        """Downloader loops through batches until it hits end_ms or empty response."""
        # Build three synthetic batches of 2 candles each, then empty
        def mk_batch(start_ts: int, n: int) -> list[list]:
            return [
                [start_ts + i * 60_000, "100", "101", "99", "100.5", "10",
                 0, 0, 0, 0, 0, 0]
                for i in range(n)
            ]

        batches = [
            mk_batch(1_704_067_200_000, 2),
            mk_batch(1_704_067_320_000, 2),
            [],  # empty → break
        ]
        call_idx = {"i": 0}

        class FakeResp:
            def __init__(self, data): self._data = data
            def raise_for_status(self): pass
            def json(self): return self._data

        async def fake_get(url, params):
            data = batches[call_idx["i"]]
            call_idx["i"] += 1
            return FakeResp(data)

        dl = BinanceDownloader()
        with patch.object(dl._client, "get", side_effect=fake_get), \
             patch("src.data.downloader.asyncio.sleep", new=AsyncMock()):
            df = await dl.download("BTCUSDT", "1m", "2024-01-01", "2024-01-02")
        await dl.close()

        assert len(df) == 4  # 2 + 2 unique
        assert df["timestamp"].is_monotonic_increasing
        assert df["timestamp"].is_unique

    @pytest.mark.asyncio
    async def test_download_empty_returns_empty_df(self):
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return []

        async def fake_get(url, params):
            return FakeResp()

        dl = BinanceDownloader()
        with patch.object(dl._client, "get", side_effect=fake_get), \
             patch("src.data.downloader.asyncio.sleep", new=AsyncMock()):
            df = await dl.download("BTCUSDT", "1m", "2024-01-01", "2024-01-02")
        await dl.close()

        assert df.empty
        assert set(df.columns) == {"timestamp", "open", "high", "low", "close", "volume"}


# ── Warmup ─────────────────────────────────────────────────────────────────


class TestWarmup:
    @pytest.mark.asyncio
    async def test_warmup_uses_parquet_when_available(self, tmp_path: Path, monkeypatch):
        """When parquet has enough candles, warmup should NOT hit the downloader."""
        from src.data import warmup as warmup_mod

        # Point ParquetStore default dir at tmp
        monkeypatch.chdir(tmp_path)
        store = ParquetStore(data_dir=str(tmp_path / "data" / "historical"))
        store.save(_make_df(1_700_000_000_000, 20), "BTCUSDT", "1h")

        # Fake feature_engine + router that just count handle_candle calls
        handled: list = []

        class FakeFE:
            on_features = None
            async def handle_candle(self, candle):
                handled.append(candle)

        class FakeRouter:
            async def on_features(self, sym, tf, feats): pass

        # Sentinel: downloader should NOT be constructed
        def explode(*a, **kw):
            raise AssertionError("BinanceDownloader should not be called — parquet had enough candles")

        monkeypatch.setattr(warmup_mod, "BinanceDownloader", explode)

        result = await warmup_mod.warmup(
            feature_engine=FakeFE(),
            router=FakeRouter(),
            symbols=["BTCUSDT"],
            timeframes=["1h"],
            min_candles=10,
        )
        assert result == {"BTCUSDT_1h": 10}
        assert len(handled) == 10

    @pytest.mark.asyncio
    async def test_warmup_falls_back_to_downloader(self, tmp_path: Path, monkeypatch):
        """Empty parquet → warmup falls back to downloader, feeds candles to engine."""
        from src.data import warmup as warmup_mod

        monkeypatch.chdir(tmp_path)
        # No parquet written → load() returns empty

        handled: list = []

        class FakeFE:
            on_features = None
            async def handle_candle(self, candle):
                handled.append(candle)

        class FakeRouter:
            async def on_features(self, sym, tf, feats): pass

        class FakeDownloader:
            async def download(self, symbol, tf, start):
                return _make_df(1_700_000_000_000, 15)
            async def close(self):
                pass

        monkeypatch.setattr(warmup_mod, "BinanceDownloader", FakeDownloader)

        result = await warmup_mod.warmup(
            feature_engine=FakeFE(),
            router=FakeRouter(),
            symbols=["BTCUSDT"],
            timeframes=["1h"],
            min_candles=10,
        )
        assert result == {"BTCUSDT_1h": 10}
        assert len(handled) == 10
        # Candles should be typed correctly
        assert handled[0].symbol == "BTCUSDT"
        assert handled[0].timeframe == "1h"
        assert handled[0].closed is True
