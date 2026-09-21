"""Order book depth CLI — record resting liquidity, inspect it, draw a heatmap.

Records Binance diff-depth streams into long-form depth rows (see
`src/data/depth_recorder.py`) and renders them as a liquidity heatmap — the
same view Bookmap draws, from the same free public feed, with no desktop app
in the path.

Crypto only. A CFD feed has no central book, so there is nothing to record on
the gold side (see `src/data/cvd.py`).

Usage:
    python -m scripts.depth record BTCUSDT --duration 300
    python -m scripts.depth show BTCUSDT
    python -m scripts.depth heatmap BTCUSDT --out heatmap.png
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import pandas as pd

from src.data.depth_recorder import DepthRecorder, DepthStore, to_heatmap_grid
from src.data.feeds.binance_ws import BinanceWebSocketFeed


def _ts(ms: int) -> str:
    return pd.to_datetime(int(ms), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M:%S")


def _load(symbol: str) -> pd.DataFrame:
    store = DepthStore()
    if not store.exists(symbol):
        print(
            f"No depth recorded for {symbol}.\n"
            f"Run: python -m scripts.depth record {symbol} --duration 300",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return store.load(symbol)


def cmd_record(args: argparse.Namespace) -> None:
    symbol = args.symbol.upper()
    recorder = DepthRecorder(symbol, sample_ms=args.sample_ms, depth=args.depth)
    store = DepthStore()

    feed = BinanceWebSocketFeed(
        symbols=[symbol],
        timeframes=["1m"],
        testnet=False,
        depth_symbols=[symbol],
        depth_speed=args.speed,
        use_futures_stream=args.futures,
    )

    async def on_depth(snapshot) -> None:
        if recorder.record(snapshot) and recorder.samples % 30 == 0:
            book = feed.books[symbol.lower()]
            bid, ask = book.best_bid, book.best_ask
            print(
                f"  {recorder.samples:>5} samples | "
                f"bid {bid:.4f} / ask {ask:.4f} | "
                f"levels {book.depth_counts[0]}/{book.depth_counts[1]} | "
                f"rows {recorder.pending_rows:,}"
            )
        if recorder.should_flush:
            recorder.flush(store)

    feed.on_depth = on_depth

    async def run() -> None:
        print(
            f"Recording {symbol} depth for {args.duration}s "
            f"(sample {args.sample_ms}ms, {args.depth} levels/side)…"
        )
        task = asyncio.create_task(feed.start())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=args.duration)
        except asyncio.TimeoutError:
            pass
        finally:
            await feed.stop()
            task.cancel()

        written = recorder.flush(store)
        book = feed.books[symbol.lower()]
        print(
            f"\nDone. {recorder.samples} samples, {written:,} rows flushed "
            f"→ {store._path(symbol)}"
        )
        if book.desync_count:
            print(f"Book desynced {book.desync_count}x and re-bootstrapped each time.")
        if recorder.samples == 0:
            print(
                "No samples captured — the book never reached SYNCED. "
                "Check network access to Binance.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    asyncio.run(run())


def cmd_show(args: argparse.Namespace) -> None:
    depth = _load(args.symbol)
    if depth.empty:
        print("No rows.")
        return

    samples = depth["timestamp"].nunique()
    bids = depth[depth["side"] == "bid"]
    asks = depth[depth["side"] == "ask"]
    print(f"\n{args.symbol.upper()} depth")
    print(f"  rows        {len(depth):,}")
    print(f"  samples     {samples:,}")
    print(f"  window      {_ts(depth['timestamp'].min())} → {_ts(depth['timestamp'].max())}")
    print(f"  price range {depth['price'].min():.6g} – {depth['price'].max():.6g}")
    print(f"  bid rows    {len(bids):,}   ask rows {len(asks):,}")

    print("\nLargest resting levels observed:")
    top = (
        depth.groupby(["side", "price"], as_index=False)["size"]
        .max()
        .sort_values("size", ascending=False)
        .head(args.limit)
    )
    print(f"{'side':<6}{'price':>14}{'max size':>16}")
    print("-" * 36)
    for _, row in top.iterrows():
        print(f"{row['side']:<6}{row['price']:>14.6g}{row['size']:>16,.4f}")

    grid, note = to_heatmap_grid(depth, price_bins=args.price_bins)
    print(f"\nHeatmap grid: {grid.shape[0]} price rows × {grid.shape[1]} time columns")
    print(f"Bucketing: {note}")


def cmd_heatmap(args: argparse.Namespace) -> None:
    from src.dashboard.flow_charts import depth_heatmap

    depth = _load(args.symbol)
    grid, note = to_heatmap_grid(
        depth, price_bins=args.price_bins, time_bins=args.time_bins
    )
    fig = depth_heatmap(
        grid,
        title=f"{args.symbol.upper()} — order book depth",
        clip_percentile=args.clip,
        note=note,
    )
    if args.out.endswith(".html"):
        fig.write_html(args.out)
    else:
        fig.write_image(args.out, width=args.width, height=args.height, scale=2)
    print(f"Wrote {args.out}  ({grid.shape[0]} price rows × {grid.shape[1]} columns)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scripts.depth", description="Order book depth recording and heatmaps"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("record", help="record live depth from Binance")
    p_rec.add_argument("symbol")
    p_rec.add_argument("--duration", type=int, default=300, help="seconds (default 300)")
    p_rec.add_argument("--sample-ms", type=int, default=1000, help="sampling cadence")
    p_rec.add_argument("--depth", type=int, default=40, help="levels per side")
    p_rec.add_argument("--speed", default="100ms", choices=["100ms", "1000ms"])
    p_rec.add_argument("--futures", action="store_true", help="USD-M perps instead of spot")
    p_rec.set_defaults(func=cmd_record)

    p_show = sub.add_parser("show", help="summarize recorded depth")
    p_show.add_argument("symbol")
    p_show.add_argument("--limit", type=int, default=15)
    p_show.add_argument("--price-bins", type=int, default=120)
    p_show.set_defaults(func=cmd_show)

    p_hm = sub.add_parser("heatmap", help="render the liquidity heatmap")
    p_hm.add_argument("symbol")
    p_hm.add_argument("--out", default="heatmap.png", help=".png or .html")
    p_hm.add_argument("--price-bins", type=int, default=160)
    p_hm.add_argument("--time-bins", type=int, default=None)
    p_hm.add_argument("--clip", type=float, default=99.0, help="colour clip percentile")
    p_hm.add_argument("--width", type=int, default=1400)
    p_hm.add_argument("--height", type=int, default=640)
    p_hm.set_defaults(func=cmd_heatmap)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
