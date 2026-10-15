"""Fetch MNQ (Micro Nasdaq futures) bars for the hybrid-scalp backtest.

Run on a machine with internet access. Writes to data/mnq/ (gitignored).

    # Best: years of 1-minute bars with exact contract rolls. Needs a Databento
    # key in the environment; the cost is printed and confirmed before anything
    # is downloaded.
    export DATABENTO_API_KEY=...
    python3 -m scripts.mnq_data databento --start 2023-01-01 --end 2026-09-26

    # Free, no signup, years of 1-minute bars: Dukascopy's Nasdaq 100 CFD,
    # the same source as our gold history. A stand-in for MNQ, not MNQ itself:
    # candles are built from bid quotes and there are no contract rolls.
    python3 -m scripts.mnq_data dukascopy --start 2023-01-01 --end 2026-09-26

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
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
    # The backtest report names its data source from this.
    path.with_suffix(".source.txt").write_text(label)
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
    try:
        cost = client.metadata.get_cost(**query)
    except Exception as exc:
        sys.exit(f"  Databento refused the request:\n  {exc}\n\n"
                 "  If it says auth: DATABENTO_API_KEY must be your own key, copied from\n"
                 "  databento.com -> Portal -> API Keys (it starts with db-).")
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


def fetch_dukascopy(start: str, end: str, instrument: str) -> None:
    try:
        import dukascopy_python as dk
    except ImportError:
        sys.exit("  pip install dukascopy-python")
    print(f"  Dukascopy {instrument}: Nasdaq 100 CFD, 1-minute bid candles. A stand-in for MNQ:")
    print("  same index, same moves, but quotes rather than trades and no contract rolls.")
    t, stop = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    chunks = []
    while t < stop:
        nxt = min(t + pd.DateOffset(months=1), stop)
        for attempt in range(3):
            try:
                df = dk.fetch(instrument=instrument, interval=dk.INTERVAL_MIN_1,
                              offer_side=dk.OFFER_SIDE_BID,
                              start=t.to_pydatetime(), end=nxt.to_pydatetime())
                break
            except Exception as exc:
                if attempt == 2:
                    sys.exit(f"  Dukascopy failed at {t:%Y-%m}: {exc}")
                time.sleep(2)
        print(f"    {t:%Y-%m}: {len(df):,} bars")
        if len(df):
            chunks.append(df)
        t = nxt
    if not chunks:
        sys.exit(f"  Dukascopy returned nothing for {instrument}")
    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index().reset_index()
    df = df.rename(columns={df.columns[0]: "ts"})
    # Minutes with no quotes come back as flat zero-volume bars; they would drag
    # ATR down and invent candles inside the daily halt.
    flat = (df["high"] == df["low"]) & (df["volume"] == 0)
    if flat.any():
        print(f"  dropped {int(flat.sum()):,} empty minutes")
    _save(df.loc[~flat, ["ts", "open", "high", "low", "close", "volume"]],
          f"Dukascopy {instrument}, Nasdaq 100 CFD bid candles (a stand-in for MNQ, no rolls)")


FIVE_MIN_MS = 300_000


def _spans(starts: set[int]) -> list[tuple[int, int]]:
    """5-minute candle starts -> [start, end) spans, neighbours joined."""
    out: list[list[int]] = []
    for s in sorted(starts):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], s + FIVE_MIN_MS)
        else:
            out.append([s, s + FIVE_MIN_MS])
    return [(a, b) for a, b in out]


def fetch_dukascopy_seconds(starts: set[int], instrument: str = "E_NQ-100",
                            workers: int = 4) -> pd.DataFrame:
    """1-second bid bars for the given 5-minute candles, used by the backtest's
    --refine to settle trades 1-minute bars can't. Cached in data/mnq/: a candle
    fetched once, even if Dukascopy had nothing for it, is never fetched again."""
    import dukascopy_python as dk

    cache = OUT_DIR / "mnq_1s_cache.parquet"
    done_path = cache.with_suffix(".done.json")
    done = set(json.loads(done_path.read_text())) if done_path.exists() else set()
    cached = pd.read_parquet(cache) if cache.exists() else None
    todo = _spans({s for s in starts if s not in done})
    if not todo:
        return cached if cached is not None else pd.DataFrame(columns=["ts", "open", "high", "low", "close"])

    def one(span):
        a, b = span
        for _ in range(3):
            try:
                df = dk.fetch(instrument=instrument, interval=dk.INTERVAL_SEC_1,
                              offer_side=dk.OFFER_SIDE_BID,
                              start=datetime.fromtimestamp(a / 1000, tz=timezone.utc),
                              end=datetime.fromtimestamp(b / 1000, tz=timezone.utc),
                              limit=int((b - a) / 1000) + 5, max_retries=2)
                df = df.reset_index()
                df = df.rename(columns={df.columns[0]: "ts"})
                ms = (pd.to_datetime(df["ts"], utc=True) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
                df["ts"] = ms.astype("int64")
                return span, df[(df["ts"] >= a) & (df["ts"] < b)][["ts", "open", "high", "low", "close"]]
            except Exception:
                time.sleep(2)
        return span, None

    # Newest first: if Dukascopy's 1-second history runs out going back, the
    # batches start coming back empty and the fetch stops instead of grinding on.
    todo = todo[::-1]
    batch_size = 40
    print(f"  fetching 1-second bars for {len(todo):,} windows from Dukascopy ({workers} at a time)")
    got, failed, empty_batches = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for b0 in range(0, len(todo), batch_size):
            results = list(ex.map(one, todo[b0:b0 + batch_size]))
            failed += sum(df is None for _, df in results)
            useful = [(span, df) for span, df in results if df is not None and len(df)]
            if useful:
                empty_batches = 0
                got += [df for _, df in useful]
                # Dukascopy is answering, so an empty window really is empty: don't ask again.
                for span, df in results:
                    if df is not None:
                        done.update(range(span[0], span[1], FIVE_MIN_MS))
            else:
                empty_batches += 1
            n_done = min(b0 + batch_size, len(todo))
            if n_done % 200 == 0 or n_done == len(todo):
                print(f"    {n_done:,}/{len(todo):,}")
            if empty_batches >= 2:
                oldest = datetime.fromtimestamp(todo[b0][0] / 1000, tz=timezone.utc)
                print(f"  Dukascopy returned no 1-second bars for {2 * batch_size} windows in a row "
                      f"(back to {oldest:%Y-%m-%d}); stopping there. The rest stay on 1-minute bars.")
                break
    if failed:
        print(f"  {failed:,} windows failed; they'll be retried next run")

    parts = [x for x in [cached, *got] if x is not None and len(x)]
    fine = (pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts")
            .reset_index(drop=True)) if parts else pd.DataFrame(columns=["ts", "open", "high", "low", "close"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fine.to_parquet(cache, index=False)
    done_path.write_text(json.dumps(sorted(done)))
    return fine


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
    k = sub.add_parser("dukascopy")
    k.add_argument("--start", required=True)
    k.add_argument("--end", required=True)
    k.add_argument("--instrument", default="E_NQ-100", help="Dukascopy's Nasdaq 100 CFD")
    sub.add_parser("yfinance")
    c = sub.add_parser("csv")
    c.add_argument("--path", required=True)
    args = ap.parse_args()

    if args.source == "databento":
        fetch_databento(args.start, args.end, args.symbol, args.yes)
    elif args.source == "dukascopy":
        fetch_dukascopy(args.start, args.end, args.instrument)
    elif args.source == "yfinance":
        fetch_yfinance()
    else:
        load_csv(args.path)


if __name__ == "__main__":
    main()
