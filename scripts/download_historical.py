#!/usr/bin/env python3
"""Download historical OHLCV data from Binance.

Usage:
    python scripts/download_historical.py --symbol BTCUSDT --timeframe 1m --start 2024-04-01
    python scripts/download_historical.py --symbol ETHUSDT --timeframe 5m --start 2024-01-01 --end 2026-01-01
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.downloader import BinanceDownloader


async def main() -> None:
    parser = argparse.ArgumentParser(description="Download historical OHLCV from Binance")
    parser.add_argument("--symbol", required=True, help="Trading pair (e.g. BTCUSDT)")
    parser.add_argument("--timeframe", default="1m", help="Candle timeframe (default: 1m)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: now)")
    args = parser.parse_args()

    print(f"Downloading {args.symbol} {args.timeframe} from {args.start} to {args.end or 'now'}...")

    downloader = BinanceDownloader()
    try:
        path = await downloader.download_and_save(
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_date=args.start,
            end_date=args.end,
        )
        print(f"Saved to: {path}")
    finally:
        await downloader.close()


if __name__ == "__main__":
    asyncio.run(main())
