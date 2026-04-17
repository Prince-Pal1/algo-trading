#!/usr/bin/env python3
"""Refresh data/historical/*_1h.parquet for the live engine's symbols.

Uses the production BinanceDownloader to pull fresh klines for every
symbol currently in the warmup roster and overwrite the on-disk parquet.
After this, a fresh engine restart will warm up vol_momentum's _closes
deque from truly-recent bars, fixing the stuck momentum=-1.61 bug
diagnosed in docs/investigations/2026-04-17_vol_momentum_stale_buffer.md.

Usage:
    python3 scripts/refresh_historical_parquets.py
    python3 scripts/refresh_historical_parquets.py --symbols ADAUSDT DOTUSDT
    python3 scripts/refresh_historical_parquets.py --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.downloader import BinanceDownloader
from src.data.storage import ParquetStore


# Default symbol set matches the engine's live roster (from warmup_complete
# log). APTUSDT is on the live roster but has no parquet; including it here
# so the refresh covers the full warmup universe.
DEFAULT_SYMBOLS = [
    "ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
    "DOTUSDT", "ETHUSDT", "NEARUSDT", "XRPUSDT", "APTUSDT",
    "SOLUSDT", "MATICUSDT",
]


async def _refresh_one(
    downloader: BinanceDownloader,
    symbol: str,
    timeframe: str,
    start_date: str,
) -> tuple[str, int, str]:
    """Returns (symbol, rows_written, last_ts_iso)."""
    path = await downloader.download_and_save(symbol, timeframe, start_date)
    # Read back to confirm the tail
    import pandas as pd
    df = pd.read_parquet(path)
    last_ts_ms = int(df.iloc[-1]["timestamp"])
    last_iso = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc).isoformat(timespec="minutes")
    return symbol, len(df), last_iso


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    p.add_argument("--timeframe", default="1h")
    p.add_argument(
        "--days",
        type=int,
        default=60,
        help="How far back to download (default 60 days, covers momentum_window=168h ~7 days × 8 buffer)",
    )
    args = p.parse_args()

    start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    print(f"Refreshing {len(args.symbols)} symbol(s), timeframe={args.timeframe}, from {start} to today\n")

    downloader = BinanceDownloader()
    try:
        results = []
        for sym in args.symbols:
            try:
                r = await _refresh_one(downloader, sym, args.timeframe, start)
                results.append(r)
                print(f"  ✓ {r[0]:10s}  rows={r[1]:>6d}  last={r[2]}")
            except Exception as e:
                print(f"  ✗ {sym:10s}  ERROR: {e}")
                results.append((sym, 0, f"ERROR: {e}"))
    finally:
        await downloader.close()

    ok = [r for r in results if r[1] > 0]
    print(f"\nRefreshed {len(ok)}/{len(results)} symbols.")
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
