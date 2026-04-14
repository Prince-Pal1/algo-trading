"""Verify LeveragedBacktestEngine attaches intrabar_sub_bars when M1PathModel.

Task #102: when the engine runs with an `M1PathModel` as its path_model,
each bar's row (passed to strategy.process) must carry an
`intrabar_sub_bars` field — a list of `M1SubBar` named tuples for the
M1 sub-bars of the current trade-timeframe bar. Strategies running
against the bridge model should NOT see this field (so they don't
accidentally use stale / uninitialized sub-bar data).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.backtest.path import BrownianBridgeModel, M1PathModel, M1SubBar
from src.strategies.base import BaseStrategy
from src.utils.types import RiskProfile, Signal


class _SpyStrategy(BaseStrategy):
    """Records every row it sees for later inspection."""

    def __init__(self) -> None:
        super().__init__(
            name="spy",
            markets=["XAUUSD"],
            timeframe="5m",
            risk_profile=RiskProfile.SAFE,
            max_risk_per_trade=0.01,
            leverage_range=(1.0, 5.0),
        )
        self.rows_seen: list[pd.Series] = []

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        self.rows_seen.append(features.copy())
        return None


def _make_m5_df(n: int = 60) -> pd.DataFrame:
    """Build a minimal M5 dataframe. Timestamps start at 0 and advance 300_000 ms."""
    rows = []
    for i in range(n):
        close = 2400.0 + (i * 0.1)
        rows.append({
            "timestamp": i * 300_000,  # 5 minutes = 300_000 ms
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def _make_m1_lookup_for_m5(m5_df: pd.DataFrame) -> dict[int, M1SubBar]:
    """Build an M1 lookup that covers every M5 bar in the dataframe.

    Each M5 bar [ts_ms, ts_ms + 300k) gets 5 M1 sub-bars at
    ts_ms, ts_ms + 60k, ts_ms + 120k, ts_ms + 180k, ts_ms + 240k.
    """
    lookup: dict[int, M1SubBar] = {}
    for _, row in m5_df.iterrows():
        base_ts = int(row["timestamp"])
        # Fake 5 M1 sub-bars across the M5 range
        base_close = float(row["close"])
        for offset in range(5):
            ts = base_ts + offset * 60_000
            lookup[ts] = M1SubBar(
                open=base_close + offset * 0.01,
                high=base_close + 0.2 + offset * 0.01,
                low=base_close - 0.2 + offset * 0.01,
                close=base_close + 0.05 + offset * 0.01,
                volume=200.0,
            )
    return lookup


class TestIntrabarAttachment:
    def test_m1_path_model_attaches_sub_bars(self):
        m5 = _make_m5_df(n=60)
        lookup = _make_m1_lookup_for_m5(m5)
        pm = M1PathModel(m1_lookup=lookup, sub_bar_count=5, run_id="test_intrabar")

        engine = LeveragedBacktestEngine(path_model=pm, run_id="test_intrabar_attach")
        spy = _SpyStrategy()
        engine.run(spy, m5, symbol="XAUUSD", timeframe="5m", leverage=1.0,
                   indicators=["atr_20"])

        assert len(spy.rows_seen) == 60
        # Every row should have intrabar_sub_bars attached
        with_sub_bars = [r for r in spy.rows_seen if "intrabar_sub_bars" in r]
        assert len(with_sub_bars) == 60

    def test_intrabar_sub_bars_are_m1_sub_bar_named_tuples(self):
        m5 = _make_m5_df(n=60)
        lookup = _make_m1_lookup_for_m5(m5)
        pm = M1PathModel(m1_lookup=lookup, sub_bar_count=5)

        engine = LeveragedBacktestEngine(path_model=pm, run_id="test_intrabar_types")
        spy = _SpyStrategy()
        engine.run(spy, m5, symbol="XAUUSD", timeframe="5m", leverage=1.0)

        # Pick a row that's well past warmup
        row = spy.rows_seen[50]
        sub_bars = row["intrabar_sub_bars"]
        assert isinstance(sub_bars, list)
        assert len(sub_bars) == 5
        # Each should be an M1SubBar with attribute access
        sb = sub_bars[0]
        assert isinstance(sb, M1SubBar)
        assert hasattr(sb, "open")
        assert hasattr(sb, "high")
        assert hasattr(sb, "low")
        assert hasattr(sb, "close")
        assert hasattr(sb, "volume")

    def test_bridge_model_does_not_attach_sub_bars(self):
        m5 = _make_m5_df(n=60)
        pm = BrownianBridgeModel(run_id="test_no_intrabar")

        engine = LeveragedBacktestEngine(path_model=pm, run_id="test_bridge")
        spy = _SpyStrategy()
        engine.run(spy, m5, symbol="XAUUSD", timeframe="5m", leverage=1.0,
                   indicators=["atr_20"])

        # No row should have intrabar_sub_bars (bridge model doesn't populate it)
        for row in spy.rows_seen:
            assert "intrabar_sub_bars" not in row

    def test_missing_m1_coverage_no_attachment(self):
        """If M1PathModel has NO coverage for a bar's ts, the field is not attached."""
        m5 = _make_m5_df(n=60)
        # Empty lookup — no M1 data at all
        pm = M1PathModel(m1_lookup={}, sub_bar_count=5)

        engine = LeveragedBacktestEngine(path_model=pm, run_id="test_empty_m1")
        spy = _SpyStrategy()
        engine.run(spy, m5, symbol="XAUUSD", timeframe="5m", leverage=1.0,
                   indicators=["atr_20"])

        # With an empty lookup, _sub_bars_for returns [] so attach is skipped
        for row in spy.rows_seen:
            assert "intrabar_sub_bars" not in row
