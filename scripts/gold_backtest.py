#!/usr/bin/env python3
"""Run existing strategies against XAUUSD parquet data.

Phase G.1 of the gold trading plan. The main backtest CLI
(`scripts/backtest.py`) hardcodes `BinanceDownloader` and can't fetch
XAUUSD. This script bypasses the downloader entirely — it loads the
parquet we downloaded via `scripts/download_xauusd.py` and feeds it
straight to `BacktestEngine.run()`.

Usage:
    python3 scripts/gold_backtest.py bb_rsi_mr --tf 1h
    python3 scripts/gold_backtest.py donchian_ensemble_adx --tf 1h
    python3 scripts/gold_backtest.py bb_rsi_mr --tf 1h --days 180
    python3 scripts/gold_backtest.py bb_rsi_mr --tf 1h --verbose  # trade list

Answers the G.1 question: "Do our existing strategies have ANY edge on
gold, or do they need to be redesigned from scratch for this asset?"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backtest import STRATEGY_REGISTRY  # reuse the existing registry
from src.backtest.engine import BacktestConfig, BacktestEngine


def load_parquet(symbol: str, timeframe: str) -> pd.DataFrame:
    """Load OHLCV parquet for a symbol/timeframe.

    Expects the schema produced by `scripts/download_xauusd.py`:
    timestamp (ms int64), open, high, low, close, volume.
    """
    path = REPO_ROOT / "data" / "historical" / f"{symbol}_{timeframe}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"parquet not found: {path}\n"
            f"Download it first: python3 scripts/download_xauusd.py "
            f"--timeframes {timeframe}"
        )
    df = pd.read_parquet(path)
    # Engine expects the canonical columns in this order
    return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()


def run_one(
    strategy_id: str,
    symbol: str,
    timeframe: str,
    days: int | None,
    commission_pct: float,
    verbose: bool,
) -> dict:
    if strategy_id not in STRATEGY_REGISTRY:
        print(f"Unknown strategy: {strategy_id}")
        print(f"Available: {', '.join(sorted(STRATEGY_REGISTRY))}")
        sys.exit(1)
    reg = STRATEGY_REGISTRY[strategy_id]

    data = load_parquet(symbol, timeframe)
    # Trim to the last N days if requested (timestamp is in ms since epoch)
    if days is not None:
        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
        )
        data = data[data["timestamp"] >= cutoff_ms].reset_index(drop=True)

    if len(data) < 50:
        print(f"ERROR: only {len(data)} bars available, need >= 50")
        sys.exit(1)

    start_dt = datetime.fromtimestamp(data["timestamp"].iloc[0] / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(data["timestamp"].iloc[-1] / 1000, tz=timezone.utc)
    print(f"── {strategy_id} on {symbol} {timeframe} ──")
    print(f"  Bars: {len(data)}  ({start_dt.date()} → {end_dt.date()})")
    print(f"  Price: ${data['low'].min():.2f} – ${data['high'].max():.2f}")
    print(f"  Commission: {commission_pct:.3f}%")
    print()

    strategy = reg["factory"](timeframe)
    engine = BacktestEngine(
        config=BacktestConfig(commission_pct=commission_pct / 100),
    )
    result = engine.run(
        strategy=strategy,
        data=data,
        symbol=symbol,
        timeframe=timeframe,
        indicators=reg.get("indicators", []),
    )

    m = result.metrics
    def _num(key):
        v = m.get(key, 0) or 0
        return v
    print(f"  Trades:            {len(result.trades)}")
    print(f"  Return:            {_num('total_return_pct'):+.2f}%")
    print(f"  Sharpe:            {_num('sharpe'):+.3f}")
    print(f"  Sortino:           {_num('sortino'):+.3f}")
    print(f"  Max drawdown:      {_num('max_drawdown_pct'):.2f}%")
    print(f"  Win rate:          {_num('win_rate_pct'):.1f}%")
    print(f"  Profit factor:     {_num('profit_factor'):.2f}")
    print(f"  Avg win/loss:      {_num('avg_win_loss_ratio'):.2f}")
    print(f"  Buy & hold:        {_num('buy_hold_return_pct'):+.2f}%")
    print()

    if verbose and result.trades:
        print("  Trade list (first 20):")
        for i, t in enumerate(result.trades[:20]):
            direction = getattr(t, "direction", "?")
            pnl_pct = getattr(t, "pnl_pct", 0)
            print(f"    {i+1:>3}. {direction:<5}  pnl={pnl_pct:+.3f}%")
        if len(result.trades) > 20:
            print(f"    ... and {len(result.trades) - 20} more trades")
        print()

    return {
        "strategy": strategy_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "n_bars": len(data),
        "n_trades": len(result.trades),
        "return_pct": float(m.get("total_return_pct", 0)),
        "sharpe": float(m.get("sharpe", 0)),
        "max_dd_pct": float(m.get("max_drawdown_pct", 0)),
        "win_rate_pct": float(m.get("win_rate_pct", 0)),
        "profit_factor": float(m.get("profit_factor", 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("strategy_id", help="Strategy ID from registry")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--tf", default="1h")
    parser.add_argument(
        "--days", type=int, default=None,
        help="Lookback days (default: use all available)",
    )
    parser.add_argument(
        "--commission", type=float, default=0.04,
        help="Commission %% per trade (default 0.04)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    run_one(
        strategy_id=args.strategy_id,
        symbol=args.symbol,
        timeframe=args.tf,
        days=args.days,
        commission_pct=args.commission,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
