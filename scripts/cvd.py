"""CVD CLI — download, inspect, and screen order flow.

Free Binance aggregated-trade dumps in, delta bars + divergence/absorption
screens out. Crypto only: a CFD feed has no tape, so there is nothing to
compute (see src/data/cvd.py).

Usage:
    python -m scripts.cvd fetch BTCUSDT --tf 5m --start 2026-09-01 --end 2026-09-07
    python -m scripts.cvd show BTCUSDT --tf 5m --bars 40
    python -m scripts.cvd screen BTCUSDT --tf 5m --lookback 20
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import pandas as pd

from src.data.agg_trades import AggTradesDownloader, flow_series_key
from src.data.cvd import delta_ratio, detect_absorption, detect_divergences
from src.data.storage import ParquetStore

SPARK = "▁▂▃▄▅▆▇█"


def _load(symbol: str, timeframe: str) -> pd.DataFrame:
    store = ParquetStore()
    key = flow_series_key(timeframe)
    if not store.exists(symbol, key):
        print(
            f"No cached flow bars for {symbol} {timeframe}.\n"
            f"Run: python -m scripts.cvd fetch {symbol} --tf {timeframe} --start YYYY-MM-DD",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return store.load(symbol, key).sort_values("timestamp").reset_index(drop=True)


def _spark(values: pd.Series) -> str:
    """Unicode sparkline — enough to eyeball price vs CVD shape side by side."""
    vals = values.dropna().to_numpy(dtype=float)
    if len(vals) == 0:
        return ""
    lo, hi = vals.min(), vals.max()
    if hi - lo < 1e-12:
        return SPARK[0] * len(vals)
    scaled = (vals - lo) / (hi - lo) * (len(SPARK) - 1)
    return "".join(SPARK[int(round(v))] for v in scaled)


def _ts(ms: int) -> str:
    return pd.to_datetime(int(ms), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M")


def cmd_fetch(args: argparse.Namespace) -> None:
    async def run() -> None:
        dl = AggTradesDownloader()
        try:
            path = await dl.download_and_save(
                args.symbol, args.tf, args.start, args.end, market=args.market
            )
            print(f"Saved flow bars → {path}")
        finally:
            await dl.close()

    asyncio.run(run())


def cmd_show(args: argparse.Namespace) -> None:
    bars = _load(args.symbol, args.tf).tail(args.bars)
    if bars.empty:
        print("No bars.")
        return

    ratios = delta_ratio(bars)
    print(f"\n{args.symbol} {args.tf} — last {len(bars)} bars\n")
    print(f"{'time':<17}{'close':>11}{'delta':>12}{'Δ/vol':>8}{'cvd':>14}")
    print("-" * 62)
    for (_, row), ratio in zip(bars.iterrows(), ratios):
        print(
            f"{_ts(row['timestamp']):<17}"
            f"{row['close']:>11.2f}"
            f"{row['delta']:>12.3f}"
            f"{ratio:>8.2f}"
            f"{row['cvd']:>14.3f}"
        )

    print(f"\nprice {_spark(bars['close'])}")
    print(f"cvd   {_spark(bars['cvd'])}")
    print(
        "\nSame shape = flow confirms price. Diverging shapes = the move is not "
        "backed by aggression."
    )


def cmd_screen(args: argparse.Namespace) -> None:
    bars = _load(args.symbol, args.tf)
    if bars.empty:
        print("No bars.")
        return

    divs = detect_divergences(bars, lookback=args.lookback, z_threshold=args.z)
    absorp = detect_absorption(bars, range_window=args.lookback)

    print(f"\n{args.symbol} {args.tf} — {len(bars)} bars "
          f"({_ts(bars['timestamp'].iloc[0])} → {_ts(bars['timestamp'].iloc[-1])})")

    print(f"\nDivergences (lookback={args.lookback}, z>={args.z}): {len(divs)}")
    if not divs.empty:
        print(f"{'time':<17}{'kind':<10}{'price_z':>9}{'cvd_z':>9}{'strength':>10}{'close':>11}")
        print("-" * 66)
        for _, row in divs.tail(args.limit).iterrows():
            print(
                f"{_ts(row['timestamp']):<17}{row['kind']:<10}"
                f"{row['price_z']:>9.2f}{row['cvd_z']:>9.2f}"
                f"{row['strength']:>10.2f}{row['close']:>11.2f}"
            )

    print(f"\nAbsorption bars: {len(absorp)}")
    if not absorp.empty:
        print(f"{'time':<17}{'kind':<15}{'Δ/vol':>8}{'range':>8}{'close':>11}")
        print("-" * 59)
        for _, row in absorp.tail(args.limit).iterrows():
            print(
                f"{_ts(row['timestamp']):<17}{row['kind']:<15}"
                f"{row['delta_ratio']:>8.2f}{row['range_ratio']:>8.2f}{row['close']:>11.2f}"
            )

    print("\nThese are screens, not signals — check context before acting on any row.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scripts.cvd", description="Cumulative Volume Delta tooling"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="download aggTrades and save delta bars")
    p_fetch.add_argument("symbol")
    p_fetch.add_argument("--tf", default="5m", help="bar timeframe (default 5m)")
    p_fetch.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_fetch.add_argument("--end", default=None, help="YYYY-MM-DD (default: start)")
    p_fetch.add_argument("--market", default="spot", choices=["spot", "um", "cm"])
    p_fetch.set_defaults(func=cmd_fetch)

    p_show = sub.add_parser("show", help="print recent delta bars + sparklines")
    p_show.add_argument("symbol")
    p_show.add_argument("--tf", default="5m")
    p_show.add_argument("--bars", type=int, default=30)
    p_show.set_defaults(func=cmd_show)

    p_screen = sub.add_parser("screen", help="list divergences and absorption bars")
    p_screen.add_argument("symbol")
    p_screen.add_argument("--tf", default="5m")
    p_screen.add_argument("--lookback", type=int, default=20)
    p_screen.add_argument("--z", type=float, default=1.0, help="min |z| on both legs")
    p_screen.add_argument("--limit", type=int, default=20, help="max rows printed")
    p_screen.set_defaults(func=cmd_screen)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
