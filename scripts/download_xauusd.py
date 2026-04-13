#!/usr/bin/env python3
"""Download XAUUSD historical OHLCV from Dukascopy into data/historical/.

Downloads 2+ years of XAUUSD data at 1h and 5m resolutions and saves them
as parquet files matching the schema our backtest engine expects.

Schema (same as existing crypto parquets in data/historical/):
    timestamp : int64 (ms since epoch)
    open      : float64
    high      : float64
    low       : float64
    close     : float64
    volume    : float64

Usage:
    python3 scripts/download_xauusd.py
    python3 scripts/download_xauusd.py --timeframes 1h 5m 15m
    python3 scripts/download_xauusd.py --years 3

Note: Dukascopy XAU/USD is spot gold quoted to the 3rd decimal (e.g. 2400.125).
Our parquet stores prices as float64 which preserves that precision.

This script is part of Phase G.1 of the gold trading plan
(~/.claude/plans/parallel-noodling-goblet.md). Runs in the worktree at
/Users/prince/algo-trading-gold and writes to /Users/prince/algo-trading/
data/historical/ via the symlink so the primary dir can also read the files.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import dukascopy_python as dk

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_DIR = REPO_ROOT / "data" / "historical"

_TIMEFRAME_MAP = {
    "1m":  dk.INTERVAL_MIN_1,
    "5m":  dk.INTERVAL_MIN_5,
    "15m": dk.INTERVAL_MIN_15,
    "30m": dk.INTERVAL_MIN_30,
    "1h":  dk.INTERVAL_HOUR_1,
    "4h":  dk.INTERVAL_HOUR_4,
    "1d":  dk.INTERVAL_DAY_1,
}


def download_timeframe(
    instrument: str,
    tf: str,
    years: float,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Download `years` worth of `tf` bars for `instrument` from Dukascopy.

    Paginates in monthly chunks to stay under Dukascopy's per-request limit.
    Returns a DataFrame with the full range concatenated and deduplicated.
    """
    if tf not in _TIMEFRAME_MAP:
        raise ValueError(f"unsupported timeframe: {tf}. Valid: {list(_TIMEFRAME_MAP)}")

    interval = _TIMEFRAME_MAP[tf]
    end = end or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=int(years * 365))

    print(f"  [{instrument} {tf}] fetching from {start.date()} to {end.date()}")

    # Chunk by month to avoid hitting the 30000-bar-per-request default
    chunks: list[pd.DataFrame] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=30), end)
        attempt = 0
        while attempt < 3:
            try:
                df = dk.fetch(
                    instrument=instrument,
                    interval=interval,
                    offer_side=dk.OFFER_SIDE_BID,
                    start=chunk_start,
                    end=chunk_end,
                )
                if df is None or len(df) == 0:
                    print(f"    {chunk_start.date()} → {chunk_end.date()}: 0 rows "
                          f"(weekend/holiday window)")
                else:
                    chunks.append(df)
                    print(f"    {chunk_start.date()} → {chunk_end.date()}: "
                          f"{len(df)} bars")
                break
            except Exception as e:
                attempt += 1
                print(f"    {chunk_start.date()}: retry {attempt}/3 ({e})")
                time.sleep(2)
        chunk_start = chunk_end

    if not chunks:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    combined = pd.concat(chunks, axis=0)
    # Dedupe in case of overlapping chunk boundaries
    combined = combined[~combined.index.duplicated(keep="first")]
    combined = combined.sort_index()
    return combined


def save_parquet(df: pd.DataFrame, out_path: Path) -> None:
    """Save DataFrame as parquet in the same schema as existing crypto files.

    Existing schema (from BTCUSDT_1h.parquet): timestamp (ms int64),
    open/high/low/close (float64), volume (float64).
    """
    if len(df) == 0:
        print(f"  skipping empty dataframe for {out_path.name}")
        return

    out_df = df.copy()
    # Convert timestamp index to int64 ms column (matches crypto parquets)
    out_df["timestamp"] = (out_df.index.astype("int64") // 1_000_000).astype("int64")
    out_df = out_df.reset_index(drop=True)
    out_df = out_df[["timestamp", "open", "high", "low", "close", "volume"]]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(out_path, engine="pyarrow")
    print(f"  ✓ wrote {out_path.name}: {len(out_df)} bars, "
          f"{out_path.stat().st_size / 1024:.0f} KB")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeframes", nargs="+", default=["1h", "5m"],
        help="Timeframes to download (default: 1h 5m)",
    )
    parser.add_argument(
        "--years", type=float, default=2.0,
        help="Years of history (default: 2.0)",
    )
    parser.add_argument(
        "--instrument", default="XAU/USD",
        help="Dukascopy instrument ID (default: XAU/USD)",
    )
    parser.add_argument(
        "--symbol-label", default="XAUUSD",
        help="Symbol label for output filenames (default: XAUUSD)",
    )
    args = parser.parse_args()

    print(f"Dukascopy → {HISTORICAL_DIR}")
    print(f"  instrument: {args.instrument}")
    print(f"  symbol_label: {args.symbol_label}")
    print(f"  timeframes: {args.timeframes}")
    print(f"  years: {args.years}")
    print()

    for tf in args.timeframes:
        print(f"── {args.symbol_label} {tf} ──")
        df = download_timeframe(args.instrument, tf, years=args.years)
        if len(df) > 0:
            print(f"  total: {len(df)} bars  "
                  f"range: {df.index[0].date()} → {df.index[-1].date()}")
            out_path = HISTORICAL_DIR / f"{args.symbol_label}_{tf}.parquet"
            save_parquet(df, out_path)
        else:
            print(f"  ⚠ no data returned for {tf}")
        print()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
