#!/usr/bin/env python3
"""Run Gold Scalp Zone Detector — 24/7 Real-Time Monitor & Historical Replay Tool.

Usage:
    # 1. Live 24/7 monitoring on IC Markets cTrader XAUUSD 1m feed:
    python3 scripts/run_scalp_zone_detector.py --live

    # 2. Historical Replay / Backtest on March 24-28, 2026:
    python3 scripts/run_scalp_zone_detector.py --replay --start 2026-03-24 --end 2026-03-29

    # 3. Custom score threshold or window:
    python3 scripts/run_scalp_zone_detector.py --replay --threshold 85.0 --window 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import dotenv
import pandas as pd

# Ensure algo-trading root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.strategies.scalping.scalp_zone_detector import ScalpZoneDetector, ScalpZoneResult
from src.strategies.scalping.scalp_zone_alerter import ScalpZoneAlerter
from src.utils.logger import get_logger, setup_logging
from src.utils.types import Candle

log = get_logger("scalp_zone_runner")


def parse_args():
    parser = argparse.ArgumentParser(description="Gold Scalp Zone Detector 24/7 Monitor & Replayer")
    parser.add_argument("--live", action="store_true", help="Run live 24/7 using IC Markets cTrader feed")
    parser.add_argument("--replay", action="store_true", help="Replay historical parquet data")
    parser.add_argument("--parquet", type=str, default="data/historical/XAUUSD_1m.parquet", help="Path to 1m parquet")
    parser.add_argument("--start", type=str, default="2026-03-24", help="Replay start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-03-29", help="Replay end date (YYYY-MM-DD)")
    parser.add_argument("--threshold", type=float, default=80.0, help="Min composite score (0-100) to trigger alert")
    parser.add_argument("--window", type=int, default=10, help="Window size of 1m candles")
    parser.add_argument("--cooldown", type=int, default=300, help="Alert cooldown in seconds")
    parser.add_argument("--no-desktop", action="store_true", help="Disable macOS desktop notifications")
    parser.add_argument("--no-beep", action="store_true", help="Disable terminal audio bell")
    parser.add_argument("--tv-screenshot", action="store_true", help="Capture TradingView chart screenshot on alert")
    return parser.parse_args()


async def run_live(args):
    dotenv.load_dotenv(REPO_ROOT / ".env")

    try:
        from src.data.feeds.icmarkets_feed import ICMarketsConfig, ICMarketsFeed
    except ImportError as e:
        log.error("icmarkets_import_error", error=str(e))
        print("Error: Could not import ICMarketsFeed. Please ensure dependencies are installed.")
        return

    try:
        config = ICMarketsConfig.from_env()
    except KeyError as e:
        print(f"Missing required environment variable in .env: {e}")
        print("Please check your .env file credentials.")
        return

    detector = ScalpZoneDetector(window_size=args.window, min_score_threshold=args.threshold)
    alerter = ScalpZoneAlerter(
        cooldown_seconds=args.cooldown,
        desktop_notifications=not args.no_desktop,
        audio_beep=not args.no_beep,
        tv_screenshot_enabled=args.tv_screenshot,
    )

    feed = ICMarketsFeed(
        config=config,
        symbols=["XAUUSD"],
        timeframes=["1m"],
    )

    candle_count = 0

    async def handle_candle(candle: Candle):
        nonlocal candle_count
        if candle.symbol.upper() != "XAUUSD" or candle.timeframe != "1m" or not candle.closed:
            return

        candle_count += 1
        res: ScalpZoneResult | None = detector.update({
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "timestamp": candle.timestamp,
        })

        if res:
            alerter.process_result(res)
            # Periodic heartbeat line every 5 candles
            if candle_count % 5 == 0:
                print(
                    f"[{time.strftime('%H:%M:%S')}] XAUUSD ${candle.close:.2f} | "
                    f"Last Score: {res.composite_score:4.1f}/100 ({res.direction:5s}) | "
                    f"ER: {res.er:.2f} | R²: {res.r_squared:.2f} | ATR: ${res.atr_14:.2f}"
                )

    feed.on_candle = handle_candle

    print("=" * 80)
    print("⚡ GOLD SCALP ZONE DETECTOR — 24/7 LIVE MONITOR STARTED ⚡")
    print(f"Symbol: XAUUSD (1m) | Min Score Threshold: {args.threshold}/100 | Cooldown: {args.cooldown}s")
    print(f"Listening to IC Markets cTrader WebSocket feed (Account {config.account_id})...")
    print("=" * 80)

    try:
        await feed.start()
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\nStopping Live Scalp Zone Monitor...")
        await feed.stop()


def run_replay(args):
    print("=" * 80)
    print(f"REPLAYING HISTORICAL DATA: {args.start} -> {args.end}")
    print(f"Source: {args.parquet} | Window: {args.window} | Threshold: {args.threshold}/100")
    print("=" * 80)

    df = pd.read_parquet(args.parquet)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")
    df_slice = df[(df["dt"] >= args.start) & (df["dt"] <= args.end)].copy().reset_index(drop=True)

    if df_slice.empty:
        print(f"No bars found for date range {args.start} to {args.end}")
        return

    print(f"Loaded {len(df_slice):,} 1-minute bars.")

    detector = ScalpZoneDetector(window_size=args.window, min_score_threshold=args.threshold)
    alerter = ScalpZoneAlerter(
        cooldown_seconds=args.cooldown,
        desktop_notifications=False,  # Don't pop up 100 OS notifications in fast replay
        audio_beep=False,
        tv_screenshot_enabled=False,
    )

    alert_count = 0
    results_list = []

    for _, row in df_slice.iterrows():
        res = detector.update({
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "timestamp": row["timestamp"],
        })
        if res:
            results_list.append(res)
            if alerter.process_result(res):
                alert_count += 1

    print("\n" + "=" * 80)
    print("HISTORICAL REPLAY SUMMARY")
    print("=" * 80)
    print(f"Total 1m Bars Evaluated: {len(results_list):,}")
    print(f"Total High-Conviction Scalp Alerts (Score >= {args.threshold}): {alert_count}")
    print(f"Alert Frequency: ~{alert_count / max(1, len(df_slice)/1440):.1f} scalp setups per day")


def main():
    setup_logging()
    args = parse_args()

    if args.live:
        try:
            asyncio.run(run_live(args))
        except KeyboardInterrupt:
            print("\nShutting down.")
    else:
        # Default to replay if not specified
        run_replay(args)


if __name__ == "__main__":
    main()
