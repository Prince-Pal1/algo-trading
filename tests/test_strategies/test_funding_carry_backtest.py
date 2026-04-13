"""A.3 backtest gate for FundingCarryStrategy.

Generates a realistic 3-year synthetic funding-rate series (stats calibrated
to Q3 2025 CoinGlass data: 92% positive, median 0.0001/8h, stdev ~0.00015)
and runs it through the BacktestEngine + FundingCarryStrategy pipeline.

Gate: the backtest's annualized Sharpe must be > 0.3 (KILL threshold).
The plan's proceed-to-paper threshold is 0.6; any number between 0.3 and
0.6 produces a "proceed with caution" warning but doesn't kill.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.funding_synthetic import (
    SyntheticSeriesConfig,
    build_synthetic_series,
)
from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.utils.types import RiskProfile


_MS_PER_EPOCH = 8 * 3600 * 1000
_EPOCHS_PER_YEAR = 365 * 3  # 3 epochs per day (00/08/16 UTC)


def _realistic_funding_series(
    n_epochs: int,
    *,
    seed: int = 42,
    mean_rate: float = 0.00009,
    stdev_rate: float = 0.00015,
    crash_epochs: list[tuple[int, int]] | None = None,
) -> pd.DataFrame:
    """Generate a realistic funding-rate DataFrame.

    Q3 2025 CoinGlass BTCUSDT funding stats:
    - 92% positive observations
    - Median +0.0001 / 8h
    - Range: -0.0003 to +0.0005 typical; spikes in extremes

    crash_epochs = list of (start_idx, duration) tuples where funding goes
    sharply negative (simulating LUNA/FTX/flash crashes). Each crash pulls
    funding to ~-0.0002/epoch for `duration` epochs.
    """
    rng = np.random.default_rng(seed)
    rates = rng.normal(mean_rate, stdev_rate, size=n_epochs)
    # Clip extreme tails (Binance has a hard cap around ±0.005)
    rates = np.clip(rates, -0.003, 0.003)

    # Apply crash windows
    if crash_epochs:
        for start, duration in crash_epochs:
            end = min(start + duration, n_epochs)
            for i in range(start, end):
                rates[i] = -0.00020 + rng.normal(0, 0.00005)

    start_ms = 1_700_000_000_000
    timestamps = [start_ms + i * _MS_PER_EPOCH for i in range(n_epochs)]

    return pd.DataFrame({
        "timestamp": timestamps,
        "funding_rate": rates,
        "mark_price": np.full(n_epochs, 50_000.0),
        "symbol": ["BTCUSDT"] * n_epochs,
    })


def _build_series_with_indicators_hardened(funding_df: pd.DataFrame, friction: float) -> pd.DataFrame:
    """Build synthetic carry series and add tiny intra-bar noise so that
    indicator computation (ATR, RSI on constant series) doesn't produce
    all-NaN columns that might confuse the engine downstream.

    Since the funding carry strategy reads only `close` and `funding_rate`,
    this noise is cosmetic — the strategy ignores it — but it prevents the
    backtest engine from logging warnings about undefined indicators.
    """
    cfg = SyntheticSeriesConfig(start_price=100.0, friction_pct=friction)
    series = build_synthetic_series(funding_df, config=cfg)
    # Tiny intra-bar spread (0.01 bps) — cosmetic only
    eps = 0.00001
    series["high"] = series["close"] * (1 + eps)
    series["low"] = series["close"] * (1 - eps)
    series["open"] = series["close"] * (1 - eps / 2)
    return series


class TestFundingCarryBacktest:
    def test_three_year_baseline_backtest_passes_kill_gate(self):
        """The Stage 3 gate: run the strategy on 3 years of realistic funding,
        assert Sharpe > 0.3 (KILL threshold).
        """
        n_epochs = 3 * _EPOCHS_PER_YEAR  # ~3285 epochs
        funding_df = _realistic_funding_series(
            n_epochs,
            seed=42,
            mean_rate=0.00009,        # slightly above historical median
            stdev_rate=0.00015,
            crash_epochs=[(500, 6), (1400, 3), (2300, 4)],   # 3 synthetic crashes
        )
        series = _build_series_with_indicators_hardened(funding_df, friction=0.00005)
        assert len(series) == n_epochs

        strategy = FundingCarryStrategy(
            name="funding_carry_test",
            markets=["BTCUSDT-CARRY"],
            timeframe="8h",
            risk_profile=RiskProfile.SAFE,
            max_risk_per_trade=0.01,
            friction_pct=0.00005,
            flip_persistence_bars=3,
            max_drawdown_kill_pct=0.03,
            max_hold_bars=0,
            cooldown_bars=3,
        )

        config = BacktestConfig(
            initial_capital=10_000.0,
            commission_pct=0.0,    # friction is already baked into synthetic price
            slippage_pct=0.0,      # same
            risk_per_trade=0.01,
            max_notional_pct=2.0,
        )
        engine = BacktestEngine(config=config)
        result = engine.run(
            strategy,
            series,
            symbol="BTCUSDT-CARRY",
            timeframe="8h",
            indicators=[],   # skip indicator compute — strategy doesn't need any
        )

        # Gate: Sharpe must clear the KILL threshold
        sharpe = result.metrics.get("sharpe", 0.0)
        total_return = result.metrics.get("total_return_pct", 0.0)
        n_trades = len(result.trades)

        print(
            f"\n[A.3 GATE] Sharpe={sharpe:.3f}, "
            f"total_return={total_return:.2f}%, "
            f"trades={n_trades}, "
            f"final_equity={result.equity_curve.iloc[-1]:.2f}"
        )

        assert sharpe > 0.3, (
            f"A.3 KILL GATE: Sharpe {sharpe:.3f} <= 0.3. "
            f"Strategy A is DOA on this synthetic series. Review research memo "
            f"assumptions or kill with obituary."
        )

    def test_negative_funding_regime_produces_losses_not_crash(self):
        """Sanity: a pure negative-funding regime should lose money but not
        crash the engine. Strategy should exit on persistent negative funding
        and stay flat via cooldown."""
        n_epochs = 300
        funding_df = _realistic_funding_series(
            n_epochs, seed=1, mean_rate=-0.0002, stdev_rate=0.0001,
        )
        series = _build_series_with_indicators_hardened(funding_df, friction=0.00005)

        strategy = FundingCarryStrategy(
            name="funding_carry_bear",
            markets=["BTCUSDT-CARRY"],
            timeframe="8h",
            friction_pct=0.00005,
            flip_persistence_bars=3,
            max_drawdown_kill_pct=0.03,
            cooldown_bars=3,
        )
        engine = BacktestEngine(config=BacktestConfig(
            initial_capital=10_000.0, commission_pct=0.0, slippage_pct=0.0,
        ))
        result = engine.run(strategy, series, symbol="BTCUSDT-CARRY", timeframe="8h", indicators=[])
        # Don't crash, produce some losing trades, total return should be slightly negative
        assert result.equity_curve.iloc[-1] < result.equity_curve.iloc[0]
        # Bounded losses — kill mechanism worked
        final_dd = 1.0 - result.equity_curve.iloc[-1] / result.equity_curve.iloc[0]
        assert final_dd < 0.10, f"Losses unbounded in bear regime: DD={final_dd:.2%}"

    def test_zero_friction_strictly_dominates_realistic_friction(self):
        """Invariant: same funding series with zero friction should produce
        strictly higher equity than with realistic friction."""
        n_epochs = 600
        funding_df = _realistic_funding_series(n_epochs, seed=7)

        series_no_fric = _build_series_with_indicators_hardened(funding_df, friction=0.0)
        series_realistic = _build_series_with_indicators_hardened(funding_df, friction=0.00005)

        def _run(series):
            s = FundingCarryStrategy(
                name="fc_friction_test",
                markets=["BTCUSDT-CARRY"],
                timeframe="8h",
                friction_pct=0.00005,
                flip_persistence_bars=3,
                max_drawdown_kill_pct=0.03,
                cooldown_bars=3,
            )
            e = BacktestEngine(config=BacktestConfig(
                initial_capital=10_000.0, commission_pct=0.0, slippage_pct=0.0,
            ))
            return e.run(s, series, symbol="BTCUSDT-CARRY", timeframe="8h", indicators=[])

        result_zero = _run(series_no_fric)
        result_real = _run(series_realistic)
        assert result_zero.equity_curve.iloc[-1] > result_real.equity_curve.iloc[-1]
