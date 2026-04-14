#!/usr/bin/env python3
"""Bar-by-bar SWIFT replication test against TV's exported chart data.

Uses the EXACT OHLC bars that TV's strategy tester used, so there is
zero data-source divergence. Compares my lookahead replication's
`le_trigger` / `se_trigger` flags at every M5 bar against TV's
`Long` / `Short` entry markers in the exported CSV.

Expected: if my replication logic is correct, every bar where TV
has Long=1 should also have my le_trigger=True, and vice versa for
Short. Discrepancies indicate real logic bugs (not data drift).

Input:
    ~/Downloads/VANTAGE_XAUUSD, 5 (1).csv
    Contains 315 M5 bars with OHLC + SWIFTALGO indicator columns:
    - Long / Short: 1 at the bar where the entry fires
    - condition: state machine value (-1, 1.0, 1.1, 1.2, 1.3, etc)
    - .position_size: current position (negative = short, positive = long)

Usage:
    python3 scripts/swift_alma_tv_data_match.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.feature_engine import _alma  # noqa: E402


TV_CSV = Path.home() / "Downloads" / "VANTAGE_XAUUSD, 5 (1).csv"

# Pine Script SWIFTALGO parameters
ALT_TF_MINUTES = 40
ALMA_LENGTH = 2
ALMA_OFFSET = 0.85
ALMA_SIGMA = 5


def load_tv_csv(path: Path) -> pd.DataFrame:
    """Load TV's exported chart data + indicator columns."""
    df = pd.read_csv(path)
    df = df.sort_values("time").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    # The 'time' column is unix SECONDS — convert to ms for compatibility
    df["timestamp"] = (df["time"] * 1000).astype("int64")
    # Build boolean entry flags from the Long/Short markers (1 at entry bar)
    df["tv_long_entry"] = df["Long"] == 1
    df["tv_short_entry"] = df["Short"] == 1
    return df


def detect_alt_anchor(tv_df: pd.DataFrame) -> int:
    """Infer the alt-TF anchor offset from TV's entry timestamps.

    At a 40-min alt TF, all entries fire on the FIRST M5 of a new alt bar.
    The minute-of-hour of each entry tells us the alignment.
    Returns offset in minutes such that: (ts_ms - offset) mod alt_tf_ms == 0
    """
    entries = tv_df[tv_df["tv_long_entry"] | tv_df["tv_short_entry"]]
    if len(entries) == 0:
        return 0
    # Compute (timestamp_ms mod alt_tf_ms) in minutes for each entry
    alt_ms = ALT_TF_MINUTES * 60 * 1000
    mods = entries["timestamp"] % alt_ms  # ms into current alt bar
    mods_min = (mods // 60000).astype(int).tolist()
    if not mods_min:
        return 0
    # The most common mod value is the "entry minute within alt bar" = 0
    # So the anchor offset = the most common mod value
    from collections import Counter
    c = Counter(mods_min)
    most_common, freq = c.most_common(1)[0]
    print(f"  Anchor detection: mod distribution = {dict(c)}")
    print(f"  Most common mod: {most_common} min  →  anchor offset = {most_common}")
    return int(most_common)


def compute_replication_signals(
    tv_df: pd.DataFrame,
    alt_tf_min: int,
    anchor_offset_min: int,
    alma_length: int,
    alma_offset: float,
    alma_sigma: float,
) -> pd.DataFrame:
    """Run the lookahead replication logic on TV's bar data.

    Returns a DataFrame with our le_trigger / se_trigger flags added.
    """
    bar_ms = alt_tf_min * 60 * 1000
    offset_ms = anchor_offset_min * 60 * 1000

    # Assign each M5 bar to its containing alt bar (by timestamp)
    df = tv_df.copy()
    df["alt_ts"] = ((df["timestamp"] - offset_ms) // bar_ms) * bar_ms + offset_ms

    # Build alt-TF bars by groupby
    alt = df.groupby("alt_ts").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()

    # Compute ALMA on the alt series
    alt["alma_close"] = _alma(alt["close"], alma_length, alma_offset, alma_sigma)
    alt["alma_open"] = _alma(alt["open"], alma_length, alma_offset, alma_sigma)
    # Mask warmup rows to NaN (avoid the 0.0 fillna artifact)
    alt.loc[alt.index[:alma_length - 1], "alma_close"] = np.nan
    alt.loc[alt.index[:alma_length - 1], "alma_open"] = np.nan

    # Merge alt ALMA values back to each M5 bar
    alt_lookup = alt[["alt_ts", "alma_close", "alma_open"]]
    df = df.merge(alt_lookup, on="alt_ts", how="left")

    # Detect crossovers at M5 level
    df["alma_close_prev"] = df["alma_close"].shift(1)
    df["alma_open_prev"] = df["alma_open"].shift(1)

    valid = (
        df["alma_close"].notna()
        & df["alma_open"].notna()
        & df["alma_close_prev"].notna()
        & df["alma_open_prev"].notna()
    )
    df["my_le_trigger"] = valid & (
        (df["alma_close_prev"] <= df["alma_open_prev"])
        & (df["alma_close"] > df["alma_open"])
    )
    df["my_se_trigger"] = valid & (
        (df["alma_close_prev"] >= df["alma_open_prev"])
        & (df["alma_close"] < df["alma_open"])
    )
    return df, alt


def compare(df: pd.DataFrame) -> dict:
    """Bar-by-bar comparison of TV entry markers vs my triggers."""
    tv_longs = df[df["tv_long_entry"]]
    tv_shorts = df[df["tv_short_entry"]]
    my_longs = df[df["my_le_trigger"]]
    my_shorts = df[df["my_se_trigger"]]

    # Align by timestamp (exact)
    tv_long_ts = set(tv_longs["timestamp"])
    tv_short_ts = set(tv_shorts["timestamp"])
    my_long_ts = set(my_longs["timestamp"])
    my_short_ts = set(my_shorts["timestamp"])

    # Match statistics
    long_match = tv_long_ts & my_long_ts
    long_tv_only = tv_long_ts - my_long_ts
    long_mine_only = my_long_ts - tv_long_ts
    short_match = tv_short_ts & my_short_ts
    short_tv_only = tv_short_ts - my_short_ts
    short_mine_only = my_short_ts - tv_short_ts

    return {
        "tv_longs": len(tv_longs),
        "tv_shorts": len(tv_shorts),
        "my_longs": len(my_longs),
        "my_shorts": len(my_shorts),
        "long_match": len(long_match),
        "long_tv_only": sorted(long_tv_only),
        "long_mine_only": sorted(long_mine_only),
        "short_match": len(short_match),
        "short_tv_only": sorted(short_tv_only),
        "short_mine_only": sorted(short_mine_only),
    }


def main() -> int:
    print("=" * 76)
    print("SWIFT bar-by-bar match: TV export vs our replication on SAME data")
    print("=" * 76)

    print(f"\nLoading TV CSV: {TV_CSV}")
    tv_df = load_tv_csv(TV_CSV)
    print(f"  {len(tv_df)} bars, {tv_df['dt'].iloc[0]} → {tv_df['dt'].iloc[-1]}")

    print("\nDetecting alt-TF anchor...")
    anchor_offset = detect_alt_anchor(tv_df)

    print(f"\nComputing replication signals (anchor=+{anchor_offset}min)...")
    df, alt = compute_replication_signals(
        tv_df, ALT_TF_MINUTES, anchor_offset,
        ALMA_LENGTH, ALMA_OFFSET, ALMA_SIGMA,
    )
    print(f"  Alt bars built: {len(alt)}")

    print("\n── Comparison ──")
    stats = compare(df)
    print(f"TV  Long entries:  {stats['tv_longs']}")
    print(f"TV  Short entries: {stats['tv_shorts']}")
    print(f"My  le_triggers:   {stats['my_longs']}")
    print(f"My  se_triggers:   {stats['my_shorts']}")
    print()
    print(f"LONG match:     {stats['long_match']} / {stats['tv_longs']} TV  |  {stats['long_match']} / {stats['my_longs']} mine")
    print(f"SHORT match:    {stats['short_match']} / {stats['tv_shorts']} TV  |  {stats['short_match']} / {stats['my_shorts']} mine")
    print()

    if stats["long_tv_only"]:
        print(f"TV Long entries NOT in mine ({len(stats['long_tv_only'])}):")
        for ts in stats["long_tv_only"][:10]:
            dt = pd.Timestamp(ts, unit="ms", tz="UTC")
            bar = df[df["timestamp"] == ts].iloc[0]
            print(f"  {dt}  cond={bar['condition']}  my_alma_close={bar.get('alma_close'):.4f}  my_alma_open={bar.get('alma_open'):.4f}")

    if stats["long_mine_only"]:
        print(f"\nMy le_triggers NOT in TV ({len(stats['long_mine_only'])}):")
        for ts in stats["long_mine_only"][:10]:
            dt = pd.Timestamp(ts, unit="ms", tz="UTC")
            bar = df[df["timestamp"] == ts].iloc[0]
            print(f"  {dt}  cond={bar['condition']}  my_alma_close={bar.get('alma_close'):.4f}  my_alma_open={bar.get('alma_open'):.4f}")

    if stats["short_tv_only"]:
        print(f"\nTV Short entries NOT in mine ({len(stats['short_tv_only'])}):")
        for ts in stats["short_tv_only"][:10]:
            dt = pd.Timestamp(ts, unit="ms", tz="UTC")
            bar = df[df["timestamp"] == ts].iloc[0]
            print(f"  {dt}  cond={bar['condition']}  my_alma_close={bar.get('alma_close'):.4f}  my_alma_open={bar.get('alma_open'):.4f}")

    if stats["short_mine_only"]:
        print(f"\nMy se_triggers NOT in TV ({len(stats['short_mine_only'])}):")
        for ts in stats["short_mine_only"][:10]:
            dt = pd.Timestamp(ts, unit="ms", tz="UTC")
            bar = df[df["timestamp"] == ts].iloc[0]
            print(f"  {dt}  cond={bar['condition']}  my_alma_close={bar.get('alma_close'):.4f}  my_alma_open={bar.get('alma_open'):.4f}")

    # Verdict
    print()
    print("=" * 76)
    total_tv = stats["tv_longs"] + stats["tv_shorts"]
    total_my = stats["my_longs"] + stats["my_shorts"]
    total_match = stats["long_match"] + stats["short_match"]
    print(f"Total TV entries: {total_tv}")
    print(f"Total mine triggers: {total_my}")
    print(f"Matching bars: {total_match} / {total_tv} TV = {total_match/max(total_tv,1)*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
