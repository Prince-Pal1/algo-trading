from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest.structure_levels import (
    StructureLevel,
    collect_levels,
    fib_retracements,
    nearest_level_above,
    nearest_level_below,
    prior_day_high_low,
    round_number_levels,
    session_open_range,
    swing_high_low,
)


def _ts(day: int, hour: int, minute: int = 0) -> int:
    dt = datetime(2024, 1, day, hour, minute, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _build_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestPriorDayHighLow:
    def test_returns_prior_day_extrema(self):
        rows = [
            {"timestamp": _ts(2, h), "open": 2400, "high": 2410 + h,
             "low": 2390 - h, "close": 2400, "volume": 1.0}
            for h in range(24)  # 0..23
        ]
        rows += [
            {"timestamp": _ts(3, h), "open": 2500, "high": 2510, "low": 2490, "close": 2500, "volume": 1.0}
            for h in range(5)
        ]
        df = _build_df(rows)
        pdh_pdl = prior_day_high_low(df, _ts(3, 10))
        assert pdh_pdl is not None
        pdh, pdl = pdh_pdl
        assert pdh == pytest.approx(2410 + 23)
        assert pdl == pytest.approx(2390 - 23)

    def test_no_prior_day_returns_none(self):
        df = _build_df([
            {"timestamp": _ts(3, h), "open": 2400, "high": 2410, "low": 2390, "close": 2400, "volume": 1.0}
            for h in range(5)
        ])
        assert prior_day_high_low(df, _ts(3, 5)) is None


class TestSessionOpenRange:
    def test_london_orb(self):
        rows = [
            {"timestamp": _ts(2, 7, 0), "open": 2400, "high": 2420, "low": 2395, "close": 2410, "volume": 1.0},
            {"timestamp": _ts(2, 7, 15), "open": 2410, "high": 2425, "low": 2405, "close": 2415, "volume": 1.0},
            {"timestamp": _ts(2, 8, 0), "open": 2415, "high": 2500, "low": 2400, "close": 2490, "volume": 1.0},
        ]
        df = _build_df(rows)
        london = session_open_range(
            df, _ts(2, 9, 0),
            session_start_hour=7, session_end_hour=11, range_minutes=30,
        )
        assert london is not None
        high, low = london
        assert high == 2425
        assert low == 2395

    def test_no_bars_in_range(self):
        rows = [
            {"timestamp": _ts(2, 13, 0), "open": 2400, "high": 2410, "low": 2390, "close": 2400, "volume": 1.0},
        ]
        df = _build_df(rows)
        assert session_open_range(df, _ts(2, 13, 30), session_start_hour=7, session_end_hour=11) is None


class TestRoundNumberLevels:
    def test_gold_levels(self):
        levels = round_number_levels(2412.0, step=50.0, count=4)
        assert 2400.0 in levels
        assert 2450.0 in levels
        assert 2350.0 in levels
        assert 2500.0 in levels

    def test_zero_step_returns_empty(self):
        assert round_number_levels(2400, step=0) == []


class TestSwingHighLow:
    def test_last_n_bars(self):
        df = _build_df([
            {"timestamp": _ts(2, h), "open": 2400, "high": 2400 + h,
             "low": 2400 - h, "close": 2400, "volume": 1.0}
            for h in range(20)
        ])
        swing = swing_high_low(df, lookback=10)
        assert swing is not None
        high, low = swing
        # Last 10 bars → h in [10, 19]
        assert high == pytest.approx(2400 + 19)
        assert low == pytest.approx(2400 - 19)

    def test_insufficient_data(self):
        df = _build_df([
            {"timestamp": _ts(2, 0), "open": 2400, "high": 2410, "low": 2390, "close": 2400, "volume": 1.0}
        ])
        assert swing_high_low(df, lookback=20) is None


class TestFibRetracements:
    def test_standard_fibs(self):
        fibs = fib_retracements(2400, 2500)
        assert fibs["fib_50"] == pytest.approx(2450.0)
        assert fibs["fib_618"] == pytest.approx(2461.8)
        assert fibs["fib_786"] == pytest.approx(2478.6)

    def test_inverted_returns_empty(self):
        assert fib_retracements(2500, 2400) == {}


class TestCollectLevels:
    def test_collect_with_all_levels(self):
        rows = [
            {"timestamp": _ts(2, h), "open": 2400, "high": 2420, "low": 2380, "close": 2410, "volume": 1.0}
            for h in range(20)
        ] + [
            {"timestamp": _ts(3, h), "open": 2430, "high": 2450, "low": 2420, "close": 2440, "volume": 1.0}
            for h in range(10)
        ]
        df = _build_df(rows)
        levels = collect_levels(df, _ts(3, 9), swing_lookback=20)
        kinds = {lev.kind for lev in levels}
        assert "pdh" in kinds and "pdl" in kinds
        assert "round" in kinds
        assert "swing_high" in kinds and "swing_low" in kinds
        assert any("fib" in k for k in kinds)


class TestNearestLevel:
    def test_nearest_above(self):
        levels = [
            StructureLevel(price=2400, kind="round"),
            StructureLevel(price=2450, kind="round"),
            StructureLevel(price=2500, kind="round"),
        ]
        nearest = nearest_level_above(levels, 2420)
        assert nearest is not None
        assert nearest.price == 2450

    def test_nearest_below(self):
        levels = [
            StructureLevel(price=2400, kind="round"),
            StructureLevel(price=2450, kind="round"),
            StructureLevel(price=2500, kind="round"),
        ]
        nearest = nearest_level_below(levels, 2420)
        assert nearest is not None
        assert nearest.price == 2400

    def test_no_level_above(self):
        levels = [StructureLevel(price=2400, kind="round")]
        assert nearest_level_above(levels, 2500) is None
