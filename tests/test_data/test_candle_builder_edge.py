"""Phase 4B — Candle builder edge case tests.

Tests the CandleBuilder and _CandleAccumulator with extreme/unusual tick data.
"""

from __future__ import annotations

import pytest

from src.data.candle_builder import CandleBuilder, _CandleAccumulator, TF_MS
from src.utils.types import Candle, Tick


class CandleCollector:
    """Helper that collects emitted candles."""
    def __init__(self):
        self.candles: list[Candle] = []

    async def __call__(self, candle: Candle):
        self.candles.append(candle)


class TestCandleBuilderEdgeCases:

    async def test_out_of_order_ticks(self):
        """Ticks arriving out of timestamp order → candle still forms."""
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        # Ticks within the same minute, out of order
        base = 60_000  # start of minute 1
        ticks = [
            Tick(symbol="BTCUSDT", price=100, quantity=1, timestamp=base + 30_000, is_buyer_maker=False),
            Tick(symbol="BTCUSDT", price=105, quantity=1, timestamp=base + 10_000, is_buyer_maker=False),
            Tick(symbol="BTCUSDT", price=95, quantity=1, timestamp=base + 50_000, is_buyer_maker=False),
        ]
        for t in ticks:
            await builder.handle_tick(t)

        # Now send tick in next minute to close the candle
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=102, quantity=1, timestamp=base + 60_000, is_buyer_maker=False)
        )

        assert len(collector.candles) == 1
        c = collector.candles[0]
        assert c.open == 100  # First tick's price (chronological by arrival)
        assert c.high == 105
        assert c.low == 95
        assert c.close == 95  # Last tick's price by arrival order

    async def test_zero_quantity_tick(self):
        """Zero quantity tick → candle still forms, volume = 0."""
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        base = 60_000
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=100, quantity=0, timestamp=base, is_buyer_maker=False)
        )
        # Close the candle
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=102, quantity=0, timestamp=base + 60_000, is_buyer_maker=False)
        )

        assert len(collector.candles) == 1
        assert collector.candles[0].volume == 0.0

    async def test_huge_time_gap(self):
        """1 hour gap between 1m ticks → candle closes on next tick."""
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        # First tick at minute 0
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=100, quantity=1, timestamp=0, is_buyer_maker=False)
        )
        # Next tick 1 hour later (60 minutes)
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=200, quantity=1, timestamp=3_600_000, is_buyer_maker=False)
        )

        # The first candle should have closed
        assert len(collector.candles) == 1
        assert collector.candles[0].close == 100
        assert collector.candles[0].open == 100

    async def test_tick_at_exact_boundary(self):
        """Tick at exactly t=60000 → belongs to next candle period."""
        builder = CandleBuilder(timeframes=["1m"])
        collector = CandleCollector()
        builder.on_candle = collector

        # Tick in minute 0
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=100, quantity=1, timestamp=0, is_buyer_maker=False)
        )
        # Tick at exact boundary (start of minute 1)
        await builder.handle_tick(
            Tick(symbol="BTCUSDT", price=105, quantity=1, timestamp=60_000, is_buyer_maker=False)
        )

        # Candle for minute 0 should have closed
        assert len(collector.candles) == 1
        c = collector.candles[0]
        assert c.close == 100  # Only first tick was in minute 0
        assert c.timestamp == 0  # Start of minute 0

    async def test_high_low_order_independence(self):
        """Same ticks in different order → same OHLCV (except O/C which depend on order)."""
        # Order 1: low first, then high
        builder1 = CandleBuilder(timeframes=["1m"])
        c1 = CandleCollector()
        builder1.on_candle = c1

        base = 60_000
        await builder1.handle_tick(Tick(symbol="X", price=90, quantity=1, timestamp=base, is_buyer_maker=False))
        await builder1.handle_tick(Tick(symbol="X", price=110, quantity=1, timestamp=base + 1, is_buyer_maker=False))
        await builder1.handle_tick(Tick(symbol="X", price=100, quantity=1, timestamp=base + 60_000, is_buyer_maker=False))

        # Order 2: high first, then low
        builder2 = CandleBuilder(timeframes=["1m"])
        c2 = CandleCollector()
        builder2.on_candle = c2

        await builder2.handle_tick(Tick(symbol="X", price=110, quantity=1, timestamp=base, is_buyer_maker=False))
        await builder2.handle_tick(Tick(symbol="X", price=90, quantity=1, timestamp=base + 1, is_buyer_maker=False))
        await builder2.handle_tick(Tick(symbol="X", price=100, quantity=1, timestamp=base + 60_000, is_buyer_maker=False))

        assert len(c1.candles) == 1
        assert len(c2.candles) == 1

        # High and low should be the same regardless of arrival order
        assert c1.candles[0].high == c2.candles[0].high == 110
        assert c1.candles[0].low == c2.candles[0].low == 90
        # Volume should be the same
        assert c1.candles[0].volume == c2.candles[0].volume == 2.0
