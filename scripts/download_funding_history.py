#!/usr/bin/env python3
"""One-shot downloader for historical BTCUSDT funding rates.

Pulls from Binance Futures /fapi/v1/fundingRate into
    data/historical/funding/BTCUSDT_8h.parquet

Usage:
    python3 scripts/download_funding_history.py
    python3 scripts/download_funding_history.py --symbol ETHUSDT --start 2023-01-01
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from src.data.downloader import BinanceFundingDownloader
from src.data.funding_synthetic import load_funding_parquet, load_synthetic_series
from src.utils.logger import get_logger

log = get_logger("download_funding_history")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD; default is now")
    parser.add_argument("--verify", action="store_true", default=True,
                        help="Load the Parquet after download + print sample rows")
    args = parser.parse_args()

    dl = BinanceFundingDownloader()
    try:
        path = await dl.download_and_save(args.symbol, args.start, args.end)
    finally:
        await dl.close()

    print(f"\n✅ Downloaded funding to: {path}")

    if args.verify:
        funding_df = load_funding_parquet(args.symbol)
        n = len(funding_df)
        print(f"\nLoaded {n} funding rate rows")
        if n > 0:
            first = funding_df.iloc[0]
            last = funding_df.iloc[-1]
            print(f"   First: ts={int(first['timestamp'])} rate={float(first['funding_rate']):.6f}")
            print(f"   Last:  ts={int(last['timestamp'])} rate={float(last['funding_rate']):.6f}")
            print(f"   Mean rate: {float(funding_df['funding_rate'].mean()):.6f}")
            print(f"   Std dev:   {float(funding_df['funding_rate'].std()):.6f}")
            pos_pct = (funding_df["funding_rate"] > 0).mean() * 100
            print(f"   Positive:  {pos_pct:.1f}%")

            # Build synthetic series and print summary
            synth = load_synthetic_series(args.symbol, friction_pct=0.00005)
            print(f"\nSynthetic series length: {len(synth)}")
            if len(synth) > 0:
                first_close = float(synth["close"].iloc[0])
                last_close = float(synth["close"].iloc[-1])
                ratio = last_close / first_close
                print(f"   Close[0]:  {first_close:.4f}")
                print(f"   Close[-1]: {last_close:.4f}")
                print(f"   Ratio:     {ratio:.4f} ({(ratio - 1) * 100:+.2f}% over window)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
