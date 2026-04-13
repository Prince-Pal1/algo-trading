#!/usr/bin/env python3
"""Research script for FundingMeanReversionStrategy (Stage 3 validation).

Loads BTCUSDT OHLCV + BTCUSDT funding rate history, merges them on 8h bars,
runs the strategy manually (bar-by-bar through `process()`), and reports
trade-level metrics: total signals, trade count, win rate, average PnL,
Sharpe, max drawdown.

This is a pre-engine validation. Once the strategy clears Stage 3 here,
the next step is wiring a funding-enriched data feed into the production
backtest engine for Stage 4/5.

Usage:
    python3 scripts/research_funding_mr.py
    python3 scripts/research_funding_mr.py --verbose
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.strategies.carry.funding_mean_reversion import FundingMeanReversionStrategy
from src.utils.types import RiskProfile, SignalAction


def load_data() -> pd.DataFrame:
    """Load and merge BTCUSDT 1h OHLCV + BTCUSDT 8h funding rates onto 8h bars."""
    ohlcv = pd.read_parquet(REPO_ROOT / "data/historical/BTCUSDT_1h.parquet")
    funding = pd.read_parquet(REPO_ROOT / "data/historical/funding/BTCUSDT_8h.parquet")

    # Convert timestamps → datetime index
    ohlcv["ts"] = pd.to_datetime(ohlcv["timestamp"], unit="ms", utc=True)
    ohlcv = ohlcv.set_index("ts")
    funding["ts"] = pd.to_datetime(funding["timestamp"], unit="ms", utc=True)
    funding = funding.set_index("ts")

    # Resample OHLCV 1h → 8h
    agg = ohlcv.resample("8h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()

    # Compute ATR_14 on the resampled 8h OHLCV
    high = agg["high"]
    low = agg["low"]
    prev_close = agg["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    agg["ATR_14"] = tr.rolling(14).mean()

    # Merge funding rates: reindex funding to 8h bars and forward-fill
    funding_8h = funding["funding_rate"].resample("8h").last().ffill()
    merged = agg.join(funding_8h, how="inner")
    merged = merged.dropna(subset=["ATR_14", "funding_rate"])
    return merged


def run_strategy(df: pd.DataFrame, verbose: bool = False) -> dict:
    """Simulate the strategy bar-by-bar and return trade metrics."""
    strat = FundingMeanReversionStrategy(
        name="funding_mean_reversion",
        markets=["BTCUSDT"],
        timeframe="8h",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.01,
        quantile_window=90,   # ~30 days of 8h for warmup
        high_quantile=0.90,
        low_quantile=0.10,
        mid_quantile=0.50,
        atr_sl_mult=2.5,
        max_hold_bars=6,      # 48h max
        cooldown_bars=2,
    )

    trades: list[dict] = []
    current_trade: dict | None = None

    for ts, row in df.iterrows():
        features = pd.Series({
            "close": row["close"],
            "high": row["high"],
            "low": row["low"],
            "ATR_14": row["ATR_14"],
            "funding_rate": row["funding_rate"],
        })
        sig = strat.process("BTCUSDT", "8h", features)
        if sig is None:
            continue

        if sig.action in (SignalAction.LONG, SignalAction.SHORT):
            current_trade = {
                "entry_ts": ts,
                "entry_price": float(row["close"]),
                "direction": 1 if sig.action == SignalAction.LONG else -1,
                "stop": sig.stop_loss,
                "entry_funding": float(row["funding_rate"]),
            }
        elif sig.action == SignalAction.CLOSE and current_trade is not None:
            exit_price = float(row["close"])
            direction = current_trade["direction"]
            # PnL as a percentage of entry (unit notional)
            pnl_pct = direction * (exit_price - current_trade["entry_price"]) / current_trade["entry_price"]
            trade = {
                **current_trade,
                "exit_ts": ts,
                "exit_price": exit_price,
                "exit_reason": sig.metadata.get("exit_reason", "?"),
                "pnl_pct": pnl_pct,
                "hold_bars": 0,  # filled below
            }
            # Hold duration in 8h bars
            hours = (ts - current_trade["entry_ts"]).total_seconds() / 3600
            trade["hold_bars"] = hours / 8.0
            trades.append(trade)
            current_trade = None
            if verbose:
                print(f"  TRADE {len(trades):>3}: "
                      f"{current_trade.get('direction') if current_trade else ('L' if trade['direction'] == 1 else 'S')}"
                      f" entry={trade['entry_price']:.1f} exit={trade['exit_price']:.1f} "
                      f"pnl={trade['pnl_pct']:+.3%} reason={trade['exit_reason']}")

    if not trades:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "avg_pnl_pct": 0.0,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_dd_pct": 0.0,
            "long_count": 0,
            "short_count": 0,
        }

    pnls = np.array([t["pnl_pct"] for t in trades])
    n_trades = len(trades)
    wins = int((pnls > 0).sum())
    long_count = int(sum(1 for t in trades if t["direction"] == 1))
    short_count = int(sum(1 for t in trades if t["direction"] == -1))

    total_return = pnls.sum()
    cumulative = np.cumsum(pnls)
    high_water = np.maximum.accumulate(cumulative)
    dd_pct = (cumulative - high_water).min() if len(cumulative) > 0 else 0.0

    if pnls.std() > 0 and n_trades >= 2:
        # Sharpe on per-trade returns, annualized assuming ~3 trades/month
        # (ballpark for this kind of episodic strategy)
        mean_r = pnls.mean()
        std_r = pnls.std(ddof=0)
        # Annualization factor — rough, per-trade basis
        trades_per_year = n_trades / (len(df) / (365 * 3))  # ~3 8h bars/day
        sharpe = (mean_r / std_r) * math.sqrt(max(trades_per_year, 1))
    else:
        sharpe = 0.0

    return {
        "n_trades": n_trades,
        "n_bars_scanned": len(df),
        "date_range": f"{df.index[0].date()} → {df.index[-1].date()}",
        "wins": wins,
        "win_rate": wins / n_trades,
        "long_count": long_count,
        "short_count": short_count,
        "avg_pnl_pct": float(pnls.mean()),
        "median_pnl_pct": float(np.median(pnls)),
        "pnl_std": float(pnls.std(ddof=0)),
        "total_return_pct": float(total_return),
        "sharpe": float(sharpe),
        "max_dd_pct": float(dd_pct),
        "exit_reasons": dict(pd.Series([t["exit_reason"] for t in trades]).value_counts()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("Loading data...")
    df = load_data()
    print(f"  Merged 8h bars: {len(df)}  "
          f"({df.index[0].date()} → {df.index[-1].date()})")
    print(f"  Funding rate distribution: "
          f"min={df['funding_rate'].min():.6f} "
          f"max={df['funding_rate'].max():.6f} "
          f"mean={df['funding_rate'].mean():.6f} "
          f"std={df['funding_rate'].std():.6f}")
    print()

    print("Running FundingMeanReversionStrategy...")
    results = run_strategy(df, verbose=args.verbose)
    print()

    print("═══ RESULTS ═══")
    for k, v in results.items():
        if isinstance(v, float):
            print(f"  {k:>20}: {v:+.4f}")
        else:
            print(f"  {k:>20}: {v}")
    print()

    # Stage 3 gate check
    n_trades = results.get("n_trades", 0)
    sharpe = results.get("sharpe", 0.0)
    print("═══ STAGE 3 GATE ═══")
    gate_pass = n_trades >= 5 and sharpe > 0.0
    if gate_pass:
        print(f"  ✅ PASS — {n_trades} trades, Sharpe {sharpe:+.3f} > 0")
    else:
        reasons = []
        if n_trades < 5:
            reasons.append(f"only {n_trades} trades (< 5)")
        if sharpe <= 0.0:
            reasons.append(f"Sharpe {sharpe:+.3f} ≤ 0")
        print(f"  ❌ FAIL — {', '.join(reasons)}")

    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
