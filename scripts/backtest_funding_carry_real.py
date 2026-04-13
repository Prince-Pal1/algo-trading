#!/usr/bin/env python3
"""Backtest the FundingCarryStrategy on REAL BTCUSDT funding history.

Loads data/historical/funding/BTCUSDT_8h.parquet, builds the synthetic
series, runs through BacktestEngine + FundingCarryStrategy, and prints
the full metrics + per-regime slice breakdown.

This is the real-data counterpart to the synthetic A.3 gate test.
Use this BEFORE enabling funding_carry=true in config.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.funding_synthetic import load_synthetic_series
from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.utils.types import RiskProfile


def _hardened(series: pd.DataFrame) -> pd.DataFrame:
    """Add cosmetic intra-bar spread so indicator compute doesn't warn."""
    series = series.copy()
    eps = 0.00001
    series["high"] = series["close"] * (1 + eps)
    series["low"] = series["close"] * (1 - eps)
    series["open"] = series["close"] * (1 - eps / 2)
    return series


def _slice(df: pd.DataFrame, start_year: int, end_year: int) -> pd.DataFrame:
    """Slice the synthetic series to a calendar year range."""
    from datetime import datetime, timezone
    start_ms = int(datetime(start_year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(end_year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    mask = (df["timestamp"] >= start_ms) & (df["timestamp"] < end_ms)
    return df[mask].reset_index(drop=True)


def _run(df: pd.DataFrame, friction: float = 0.00005) -> dict:
    strategy = FundingCarryStrategy(
        name="funding_carry_real",
        markets=["BTCUSDT-CARRY"],
        timeframe="8h",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.01,
        friction_pct=friction,
        flip_persistence_bars=3,
        max_drawdown_kill_pct=0.03,
        cooldown_bars=3,
    )
    engine = BacktestEngine(config=BacktestConfig(
        initial_capital=10_000.0, commission_pct=0.0, slippage_pct=0.0,
    ))
    result = engine.run(strategy, df, symbol="BTCUSDT-CARRY", timeframe="8h", indicators=[])
    return {
        "sharpe": float(result.metrics.get("sharpe", 0.0)),
        "total_return_pct": float(result.metrics.get("total_return_pct", 0.0)),
        "max_dd_pct": float(result.metrics.get("max_drawdown_pct", 0.0)),
        "n_trades": len(result.trades),
        "final_equity": float(result.equity_curve.iloc[-1]) if not result.equity_curve.empty else 10_000.0,
    }


def main() -> int:
    print("=" * 70)
    print("FundingCarryStrategy — REAL DATA BACKTEST")
    print("=" * 70)

    # Load the real synthetic series (already friction-adjusted)
    series = load_synthetic_series("BTCUSDT", friction_pct=0.00005)
    series = _hardened(series)
    n = len(series)
    if n == 0:
        print("❌ No data — run download_funding_history.py first")
        return 1

    print(f"\nDataset: {n} epochs (~{n / 3 / 365:.2f} years)")
    print(f"Friction: 0.00005 per 8h (amortized)")

    # Full period
    print(f"\n─── FULL PERIOD ({n} epochs) ───")
    r = _run(series)
    print(f"  Sharpe:       {r['sharpe']:.3f}")
    print(f"  Total return: {r['total_return_pct']:.2f}%")
    print(f"  Max DD:       {r['max_dd_pct']:.2f}%")
    print(f"  Trades:       {r['n_trades']}")
    print(f"  Final equity: ${r['final_equity']:,.2f}")

    full_sharpe = r["sharpe"]

    # Regime slices
    regimes = {
        "2022 (bear)": _slice(series, 2022, 2022),
        "2023 (recovery)": _slice(series, 2023, 2023),
        "2024 (bull)": _slice(series, 2024, 2024),
        "2025 (mixed)": _slice(series, 2025, 2025),
        "2026 YTD":      _slice(series, 2026, 2026),
    }

    for name, slice_df in regimes.items():
        if len(slice_df) < 50:
            continue
        print(f"\n─── {name} ({len(slice_df)} epochs) ───")
        r = _run(slice_df)
        print(f"  Sharpe:       {r['sharpe']:.3f}")
        print(f"  Return:       {r['total_return_pct']:.2f}%")
        print(f"  Trades:       {r['n_trades']}")

    # Fee sensitivity
    print("\n─── FRICTION SENSITIVITY (full period) ───")
    for friction_label, friction_val in [
        ("optimistic 0.00003", 0.00003),
        ("baseline   0.00005", 0.00005),
        ("realistic  0.00007", 0.00007),
        ("pessimistic 0.00010", 0.00010),
    ]:
        slice_df = load_synthetic_series("BTCUSDT", friction_pct=friction_val)
        slice_df = _hardened(slice_df)
        r = _run(slice_df, friction=friction_val)
        print(f"  {friction_label}: Sharpe={r['sharpe']:+.3f} return={r['total_return_pct']:+.2f}% trades={r['n_trades']}")

    # Gate verdict
    print("\n" + "=" * 70)
    if full_sharpe >= 0.6:
        print(f"✅ GATE PASS — Sharpe {full_sharpe:.3f} >= 0.6 (proceed to paper)")
    elif full_sharpe >= 0.3:
        print(f"⚠  SOFT PASS — Sharpe {full_sharpe:.3f} in [0.3, 0.6) — proceed with caution")
    else:
        print(f"❌ KILL — Sharpe {full_sharpe:.3f} < 0.3")
    print("=" * 70)
    return 0 if full_sharpe >= 0.3 else 2


if __name__ == "__main__":
    sys.exit(main())
