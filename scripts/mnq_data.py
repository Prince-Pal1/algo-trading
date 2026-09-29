"""Fetch MNQ (Micro Nasdaq futures) bars for the hybrid-scalp backtest.

Run on a machine with internet access. Writes to data/mnq/ (gitignored).

    # Best: years of 1-minute bars with exact contract rolls. Needs a Databento
    # key in the environment; the cost is printed and confirmed before anything
    # is downloaded.
    export DATABENTO_API_KEY=...
    python3 -m scripts.mnq_data databento --start 2023-01-01 --end 2026-09-26

    # Free, no signup, but only ~60 days of 5-minute bars and no roll info.
    python3 -m scripts.mnq_data yfinance

    # Anything else: a CSV with a time column plus open/high/low/close.
    # A TradingView chart export works (its time column is Unix seconds).
    python3 -m scripts.mnq_data csv --path ~/Downloads/CME_MINI_MNQ1!, 5.csv

1-minute data is worth the trouble. With a 1.5 ATR stop and a 2 ATR target a
single 5-minute candle can hit both, and only finer bars say which came first.
The backtest reports how many trades had to be guessed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path("data/mnq")


def _save(df: pd.DataFrame, label: str) -> Path:
    from src.strategies.futures.mnq_hybrid import normalise
    df = normalise(df)
    spacing = int(np.median(np.diff(df["ts"].to_numpy()))) if len(df) > 1 else 0
    kind = "1m" if spacing <= 60_000 else "5m"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"mnq_{kind}.parquet"
    df.to_parquet(path, index=False)
    first = pd.to_datetime(df["ts"].iloc[0], unit="ms", utc=True)
    last = pd.to_datetime(df["ts"].iloc[-1], unit="ms", utc=True)
    rolls = int((df["instrument_id"].diff().fillna(0) != 0).sum()) if "instrument_id" in df else None
    print(f"  saved {len(df):,} {kind} bars from {label}")
    print(f"  {first:%Y-%m-%d} -> {last:%Y-%m-%d}   ->  {path}")
    if rolls is not None:
        print(f"  contract rolls in range: {rolls}")
    return path


def fetch_databento(start: str, end: str, symbol: str, yes: bool) -> None:
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("  DATABENTO_API_KEY is not set. Put the key in the environment; never in a file in the repo.")
    try:
        import databento as db
    except ImportError:
        sys.exit("  pip install databento")

    client = db.Historical(key)
    query = dict(dataset="GLBX.MDP3", symbols=[symbol], stype_in="continuous",
                 schema="ohlcv-1m", start=start, end=end)
    cost = client.metadata.get_cost(**query)
    print(f"  Databento {symbol} 1-minute bars {start} -> {end}: estimated cost ${cost:.2f}")
    if not yes and input("  download? [y/N] ").strip().lower() != "y":
        sys.exit("  cancelled")

    df = client.timeseries.get_range(**query).to_df()
    df = df.reset_index().rename(columns={"ts_event": "ts"})
    # Older clients return fixed-point prices (1e-9 units); newer return floats.
    for col in ("open", "high", "low", "close"):
        if df[col].abs().max() > 1e8:
            df[col] = df[col] / 1e9
    keep = [c for c in ("ts", "open", "high", "low", "close", "volume", "instrument_id") if c in df]
    _save(df[keep], f"Databento {symbol}")


def fetch_yfinance() -> None:
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("  pip install yfinance")
    print("  yfinance keeps ~60 days of 5-minute bars and no contract roll information.")
    print("  Fine for a first look; too short to judge the strategy.")
    df = yf.download("MNQ=F", interval="5m", period="60d", progress=False, auto_adjust=False)
    if df.empty:
        sys.exit("  yfinance returned nothing")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    df = df.rename(columns={df.columns[0]: "ts"})
    _save(df[["ts", "Open", "High", "Low", "Close", "Volume"]].rename(columns=str.lower), "yfinance MNQ=F")


def load_csv(path: str) -> None:
    df = pd.read_csv(Path(path).expanduser())
    _save(df, path)


def main() -> None:
    ap = argparse.ArgumentParser(prog="scripts.mnq_data", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="source", required=True)
    d = sub.add_parser("databento")
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--symbol", default="MNQ.v.0",
                   help="continuous contract; .v.0 rolls to the most-traded month, as traders do")
    d.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    sub.add_parser("yfinance")
    c = sub.add_parser("csv")
    c.add_argument("--path", required=True)
    args = ap.parse_args()

    if args.source == "databento":
        fetch_databento(args.start, args.end, args.symbol, args.yes)
    elif args.source == "yfinance":
        fetch_yfinance()
    else:
        load_csv(args.path)


if __name__ == "__main__":
    main()
