"""Tests for CandleBuilder — tick aggregation + kline passthrough + dedup."""

from __future__ import annotations

import pytest

from src.data.candle_builder import CandleBuilder, TF_MS
from src.utils.types import Candle, Tick


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tick(symbol: str, price: float, qty: float, ts: int) -> Tick:
    return Tick(symbol=symbol, price=price, quantity=qty,
                timestamp=ts, is_buyer_maker=False)


def _candle(symbol: str, tf: str, o: float, h: float, l: float, c: float,
            v: float, ts: int, closed: bool = True) -> Candle:
    return Candle(symbol=symbol, timeframe=tf, open=o, high=h, low=l,
                  close=c, volume=v, timestamp=ts, closed=closed)


class CandleCollector:
    """Async callback that collects emitted candles."""
    def __init__(self):
        self.candles: list[Candle] = []

    async def __call__(self, candle: Candle) -> None:
        self.candles.append(candle)


# ── Tests ────────────────────────────────────────────────────────────────────

class TestTickBuildsCandle:
    """N ticks across a timeframe boundary → one closed candle emitted."""

    async def test_tick_builds_candle(self):
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        base_ts = 0
        await builder.handle_tick(_tick("BTCUSDT", 100.0, 1.0, base_ts + 1000))
        await builder.handle_tick(_tick("BTCUSDT", 105.0, 2.0, base_ts + 30_000))
        await builder.handle_tick(_tick("BTCUSDT", 102.0, 0.5, base_ts + 50_000))

        assert len(collector.candles) == 0  # Still in first window

        # Tick in next minute triggers close of previous candle
        await builder.handle_tick(_tick("BTCUSDT", 110.0, 1.0, base_ts + 60_001))

        assert len(collector.candles) == 1
        c = collector.candles[0]
        assert c.symbol == "BTCUSDT"
        assert c.timeframe == "1m"
        assert c.closed is True

    async def test_candle_ohlcv_correct(self):
        """Open = first tick, High = max, Low = min, Close = last, Volume = sum."""
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        base_ts = 0
        await builder.handle_tick(_tick("ETHUSDT", 50.0, 1.0, base_ts + 1000))   # Open
        await builder.handle_tick(_tick("ETHUSDT", 55.0, 2.0, base_ts + 20_000))  # High
        await builder.handle_tick(_tick("ETHUSDT", 48.0, 0.5, base_ts + 40_000))  # Low
        await builder.handle_tick(_tick("ETHUSDT", 52.0, 1.5, base_ts + 55_000))  # Close

        # Trigger close
        await builder.handle_tick(_tick("ETHUSDT", 60.0, 1.0, base_ts + 60_001))

        c = collector.candles[0]
        assert c.open == 50.0
        assert c.high == 55.0
        assert c.low == 48.0
        assert c.close == 52.0
        assert c.volume == pytest.approx(1.0 + 2.0 + 0.5 + 1.5)


class TestKlinePassthrough:
    """Exchange klines are forwarded when closed, ignored when open."""

    async def test_kline_passthrough_closed(self):
        builder = CandleBuilder(timeframes=["1h"])
        collector = CandleCollector()
        builder.on_candle = collector

        closed = _candle("BTCUSDT", "1h", 100, 110, 95, 105, 500, 0, closed=True)
        not_closed = _candle("BTCUSDT", "1h", 100, 108, 98, 103, 300, 0, closed=False)

        await builder.handle_candle(not_closed)
        assert len(collector.candles) == 0

        await builder.handle_candle(closed)
        assert len(collector.candles) == 1
        assert collector.candles[0].close == 105.0


class TestDedupSuppression:
    """After a kline arrives for (SYM, tf), tick-built candles stop for that pair."""

    async def test_dedup_suppresses_tick_candles(self):
        builder = CandleBuilder(timeframes=["1h"])
        collector = CandleCollector()
        builder.on_candle = collector

        # Send a kline — registers (BTCUSDT, 1h) as kline pair
        kline = _candle("BTCUSDT", "1h", 100, 110, 95, 105, 500, 0, closed=True)
        await builder.handle_candle(kline)
        assert len(collector.candles) == 1

        # Ticks for (BTCUSDT, 1h) should be suppressed
        await builder.handle_tick(_tick("BTCUSDT", 100.0, 1.0, 1000))
        await builder.handle_tick(_tick("BTCUSDT", 105.0, 1.0, 3_600_001))

        # No new candle from ticks
        assert len(collector.candles) == 1

    async def test_custom_tf_not_suppressed(self):
        """Tick-built candles for TFs without klines (e.g. 5s) still emit."""
        builder = CandleBuilder(timeframes=["5s", "1h"])
        collector = CandleCollector()
        builder.on_candle = collector

        # Register 1h as kline pair
        kline = _candle("ETHUSDT", "1h", 100, 110, 95, 105, 500, 0, closed=True)
        await builder.handle_candle(kline)

        # Ticks: 5s should still build, 1h suppressed
        await builder.handle_tick(_tick("ETHUSDT", 100.0, 1.0, 1000))
        await builder.handle_tick(_tick("ETHUSDT", 102.0, 1.0, 6000))  # >5s later

        # 1 from kline + 1 from 5s tick = 2
        assert len(collector.candles) == 2
        assert collector.candles[1].timeframe == "5s"


class TestMultipleTimeframes:
    """(ETH, 1m) and (ETH, 5m) have separate accumulators, emit independently."""

    async def test_multiple_timeframes_independent(self):
        builder = CandleBuilder(timeframes=["1m", "5m"])
        collector = CandleCollector()
        builder.on_candle = collector

        base_ts = 0
        await builder.handle_tick(_tick("ETHUSDT", 100.0, 1.0, base_ts + 1000))
        await builder.handle_tick(_tick("ETHUSDT", 105.0, 1.0, base_ts + 30_000))

        # Tick in second minute — triggers 1m close but NOT 5m close
        await builder.handle_tick(_tick("ETHUSDT", 110.0, 1.0, base_ts + 60_001))

        candle_tfs = [c.timeframe for c in collector.candles]
        assert "1m" in candle_tfs
        assert "5m" not in candle_tfs

        # Tick past 5m boundary — triggers 5m close
        await builder.handle_tick(_tick("ETHUSDT", 115.0, 1.0, base_ts + 300_001))
        candle_tfs = [c.timeframe for c in collector.candles]
        assert "5m" in candle_tfs


class TestBoundaryAlignment:
    """start_ts aligns to exact timeframe boundary (not first tick time)."""

    async def test_boundary_alignment(self):
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        # First tick at 15s into the minute
        await builder.handle_tick(_tick("BTCUSDT", 100.0, 1.0, 15_000))
        # Tick in next minute to close
        await builder.handle_tick(_tick("BTCUSDT", 105.0, 1.0, 60_001))

        c = collector.candles[0]
        # start_ts aligned to minute boundary (0), not first tick (15000)
        assert c.timestamp == 0


class TestZeroTickCandle:
    """No candle emitted if zero ticks in period."""

    async def test_zero_tick_candle_not_emitted(self):
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        # Single tick, then nothing — no candle emitted
        await builder.handle_tick(_tick("BTCUSDT", 100.0, 1.0, 1000))
        assert len(collector.candles) == 0

        acc = builder._accumulators.get(("BTCUSDT", "1m"))
        assert acc is not None
        assert acc.tick_count == 1
