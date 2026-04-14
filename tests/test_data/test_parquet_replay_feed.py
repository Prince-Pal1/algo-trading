from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.feeds.parquet_replay_feed import ParquetReplayFeed
from src.utils.types import Candle


def _write_parquet(tmp_path: Path, n: int = 20) -> Path:
    ts = np.arange(n, dtype=np.int64) * 3_600_000 + 1_700_000_000_000
    df = pd.DataFrame({
        "timestamp": ts,
        "open": np.full(n, 2400.0),
        "high": np.full(n, 2410.0),
        "low": np.full(n, 2395.0),
        "close": np.arange(n) + 2400.0,
        "volume": np.full(n, 100.0),
    })
    path = tmp_path / "XAUUSD_1h.parquet"
    df.to_parquet(path)
    return path


class TestParquetReplayFeed:
    @pytest.mark.asyncio
    async def test_emits_all_bars_in_order(self, tmp_path):
        path = _write_parquet(tmp_path, n=20)
        feed = ParquetReplayFeed(path, symbol="XAUUSD", timeframe="1h")

        received: list[Candle] = []

        async def on_candle(c: Candle) -> None:
            received.append(c)

        feed.on_candle = on_candle
        await feed.start()

        assert len(received) == 20
        timestamps = [c.timestamp for c in received]
        assert timestamps == sorted(timestamps)
        closes = [c.close for c in received]
        assert closes == [2400.0 + i for i in range(20)]

    @pytest.mark.asyncio
    async def test_candle_fields_populated(self, tmp_path):
        path = _write_parquet(tmp_path, n=5)
        feed = ParquetReplayFeed(path, symbol="xauusd", timeframe="1h")
        received: list[Candle] = []

        async def on_candle(c: Candle) -> None:
            received.append(c)

        feed.on_candle = on_candle
        await feed.start()

        c = received[0]
        assert c.symbol == "XAUUSD"  # uppercased
        assert c.timeframe == "1h"
        assert c.open == 2400.0
        assert c.high == 2410.0
        assert c.low == 2395.0
        assert c.close == 2400.0
        assert c.volume == 100.0
        assert c.closed is True

    @pytest.mark.asyncio
    async def test_stop_halts_mid_stream(self, tmp_path):
        path = _write_parquet(tmp_path, n=100)
        # speed_multiplier 360000 makes 1h = 0.01s, fast enough for the test
        feed = ParquetReplayFeed(
            path, symbol="XAUUSD", timeframe="1h",
            realtime_mode=True, speed_multiplier=360000.0,
        )

        received: list[Candle] = []

        async def on_candle(c: Candle) -> None:
            received.append(c)
            if len(received) == 3:
                await feed.stop()

        feed.on_candle = on_candle
        await feed.start()

        assert len(received) >= 1
        assert len(received) < 100

    @pytest.mark.asyncio
    async def test_no_callback_is_noop(self, tmp_path):
        path = _write_parquet(tmp_path, n=5)
        feed = ParquetReplayFeed(path, symbol="XAUUSD", timeframe="1h")
        # No on_candle set — should run without errors
        await feed.start()

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path):
        feed = ParquetReplayFeed(tmp_path / "nope.parquet", symbol="XAUUSD")
        with pytest.raises(FileNotFoundError):
            await feed.start()

    @pytest.mark.asyncio
    async def test_missing_columns_raises(self, tmp_path):
        df = pd.DataFrame({"ts": [1, 2, 3], "close": [100, 101, 102]})
        path = tmp_path / "bad.parquet"
        df.to_parquet(path)
        feed = ParquetReplayFeed(path, symbol="XAUUSD")
        with pytest.raises(ValueError, match="missing columns"):
            await feed.start()

    @pytest.mark.asyncio
    async def test_public_surface_matches_binance_feed(self, tmp_path):
        """ParquetReplayFeed must expose the same public attributes + async
        methods as BinanceWebSocketFeed so the shadow orchestrator can swap
        feeds without code changes."""
        from src.data.feeds.binance_ws import BinanceWebSocketFeed

        path = _write_parquet(tmp_path, n=2)
        parquet_feed = ParquetReplayFeed(path, symbol="XAUUSD")
        binance_feed = BinanceWebSocketFeed(symbols=["BTCUSDT"])

        for attr in ("on_tick", "on_candle"):
            assert hasattr(parquet_feed, attr)
            assert hasattr(binance_feed, attr)

        import inspect
        assert inspect.iscoroutinefunction(parquet_feed.start)
        assert inspect.iscoroutinefunction(binance_feed.start)
        assert inspect.iscoroutinefunction(parquet_feed.stop)
        assert inspect.iscoroutinefunction(binance_feed.stop)
