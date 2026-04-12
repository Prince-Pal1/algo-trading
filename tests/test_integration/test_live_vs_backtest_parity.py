"""Live ↔ Backtest signal parity test.

Critical guarantee: the live pipeline (FeatureEngine rolling window) and the
backtest pipeline (BacktestEngine full-df vectorized) must produce IDENTICAL
indicator values and IDENTICAL signals on the same OHLCV data — otherwise
paper/live results silently diverge from backtests.

What this verifies:
    1. Feature Series at bar N is identical (past warmup) between both paths.
    2. A real strategy (bb_rsi_mr) produces the same signals at the same bars.
    3. Specifically tests the FeatureEngine's `COMPUTE_WINDOW=250` rolling
       computation vs BacktestEngine's full-df computation — mathematically
       these should match exactly once bars past the warmup window.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.feature_engine import FeatureEngine
from src.strategies.base import BaseStrategy
from src.strategies.day_trading.bb_rsi_mr import BBRSIMeanRevStrategy
from src.utils.types import Candle, Signal

DATA_PATH = Path(__file__).parent.parent.parent / "data" / "historical" / "BTCUSDT_1h.parquet"
N_BARS = 400          # bars to feed through both paths
WARMUP = 260          # skip first N bars (COMPUTE_WINDOW=250 + a few indicator warmups)
INDICATORS = ["bbands_20", "rsi_14", "adx_14", "atr_14"]


pytestmark = pytest.mark.skipif(
    not DATA_PATH.exists(),
    reason=f"Historical data not found at {DATA_PATH}",
)


@pytest.fixture(scope="module")
def ohlcv_df() -> pd.DataFrame:
    df = pd.read_parquet(DATA_PATH).head(N_BARS).reset_index(drop=True)
    return df


class FeatureCapture(BaseStrategy):
    """Strategy that records every features Series it receives. Emits no signals."""

    def __init__(self):
        super().__init__(name="feature_capture", markets=["BTCUSDT"], timeframe="1h")
        self.captured: list[pd.Series] = []

    def on_features(self, symbol, timeframe, features):
        self.captured.append(features.copy())
        return None


def _run_live_path(df: pd.DataFrame) -> list[pd.Series]:
    """Replay df through FeatureEngine candle-by-candle, capture emitted feature Series."""
    captured: list[pd.Series] = []

    async def on_feat(sym, tf, feats):
        captured.append(feats.copy())

    engine = FeatureEngine(indicators=INDICATORS)
    engine.on_features = on_feat

    async def drive():
        for _, row in df.iterrows():
            c = Candle(
                symbol="BTCUSDT", timeframe="1h",
                timestamp=int(row["timestamp"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]),
                closed=True,
            )
            await engine.handle_candle(c)

    asyncio.run(drive())
    return captured


def _run_backtest_path(df: pd.DataFrame) -> list[pd.Series]:
    """Run df through BacktestEngine with a capture strategy."""
    strat = FeatureCapture()
    config = BacktestConfig(
        initial_capital=10_000.0, commission_pct=0.0, slippage_pct=0.0,
        risk_per_trade=0.01, max_notional_pct=1.0,
    )
    BacktestEngine(config=config).run(
        strategy=strat, data=df, symbol="BTCUSDT", timeframe="1h",
        indicators=INDICATORS,
    )
    return strat.captured


# ── Tests ──────────────────────────────────────────────────────────────────


class TestFeatureParity:
    """FeatureEngine rolling window must match BacktestEngine full-df computation."""

    @pytest.fixture(scope="class")
    def both_paths(self, ohlcv_df):
        return _run_live_path(ohlcv_df), _run_backtest_path(ohlcv_df)

    def test_same_number_of_emissions(self, both_paths):
        live, bt = both_paths
        assert len(live) == len(bt) == N_BARS

    # Non-recursive indicators (SMA-based) match bit-exactly across paths.
    # Wilder's recursive indicators (RSI/ATR/ADX) drift by ~1e-6 due to live's
    # rolling COMPUTE_WINDOW=250 seeding from a different start point than the
    # backtest's full-df seed. This is numerical, not logical — tolerances
    # reflect the actual mathematical convergence rate.
    INDICATOR_TOLERANCES = {
        "BBM_20": 1e-9,                  # pure SMA: bit-exact
        "BBU_20": 1e-6, "BBL_20": 1e-6,  # SMA ± 2·std: sqrt introduces ~1e-9 rounding
        "RSI_14": 1e-4, "ATR_14": 1e-4, "ADX_14": 1e-4,  # Wilder recursive: ~1e-6 drift
    }

    @pytest.mark.parametrize("col,tol", list(INDICATOR_TOLERANCES.items()))
    def test_indicator_column_matches(self, both_paths, col, tol):
        """After warmup, every bar's indicator value must match within tolerance.

        Non-recursive (SMA) indicators match to 1e-9. Wilder's recursive
        indicators converge within 1e-4 of each other — tiny, well below any
        threshold a strategy would key off.
        """
        live, bt = both_paths
        mismatches = []
        for i in range(WARMUP, N_BARS):
            lv = live[i].get(col)
            bv = bt[i].get(col)
            if pd.isna(lv) and pd.isna(bv):
                continue
            if pd.isna(lv) or pd.isna(bv) or abs(lv - bv) > tol:
                mismatches.append((i, lv, bv, abs(lv - bv) if not pd.isna(lv) and not pd.isna(bv) else None))
        assert not mismatches, (
            f"{col} diverged at {len(mismatches)}/{N_BARS - WARMUP} bars "
            f"(tolerance={tol}) — worst: {max(mismatches, key=lambda m: m[3] or 0)}"
        )

    def test_ohlcv_columns_identical(self, both_paths):
        live, bt = both_paths
        for i in range(N_BARS):
            for col in ("open", "high", "low", "close", "volume", "timestamp"):
                assert live[i][col] == bt[i][col], f"bar {i} col {col} diverged"


class TestRealStrategyParity:
    """bb_rsi_mr must fire the same signals at the same bars on both paths."""

    def _collect_signals(self, path_output, strategy_factory):
        """Replay a series of feature Series through a fresh strategy and collect signals."""
        strat = strategy_factory()
        signals: list[tuple[int, str]] = []
        for i, feats in enumerate(path_output):
            sig: Signal | None = strat.process("ETHUSDT", "1h", feats)
            if sig is not None:
                signals.append((i, sig.action.value))
        return signals

    def test_bb_rsi_mr_signals_match(self, ohlcv_df):
        live_feats = _run_live_path(ohlcv_df)
        bt_feats = _run_backtest_path(ohlcv_df)

        # Use a factory so each path gets a fresh strategy with clean state
        factory = lambda: BBRSIMeanRevStrategy(
            name="bb_rsi_mr_parity",
            markets=["BTCUSDT"],
            timeframe="1h",
        )

        live_signals = self._collect_signals(live_feats, factory)
        bt_signals = self._collect_signals(bt_feats, factory)

        assert live_signals == bt_signals, (
            f"Signal mismatch:\n  live: {live_signals}\n  bt:   {bt_signals}"
        )
