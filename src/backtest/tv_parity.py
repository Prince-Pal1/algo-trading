"""TradingView parity validation framework.

Reusable library for validating that a Python port of a Pine Script
strategy reproduces TradingView's backtest results bar-by-bar on the
same data. Used as Stage 0 of the strategy development process for
any externally-sourced Pine Script.

The core principle: if our Python implementation is logically
equivalent to Pine Script, and we feed it the EXACT same OHLC bars
TV used, AND we apply Pine's lookahead semantics (for strategies
that use `request.security(..., lookahead=barmerge.lookahead_on)`),
then our signal generation should match TV's strategy tester
bar-by-bar with >= 99% accuracy.

Once port equivalence is confirmed via this framework, the same
strategy class can be run through the production `LeveragedBacktestEngine`
with IC Markets fees + M3S risk management + leverage sweeps to
measure real-world viability — a separate question from correctness.

This module extracts logic that was originally one-off in
`scripts/swift_alma_tv_data_match.py` and
`scripts/swift_alma_lookahead_replication.py`. Those scripts were
proven correct at 100% on 107 days of real Vantage XAUUSD data
spanning a DST transition.

Public API:

    # CSV loaders
    load_tv_chart_csv(path) -> pd.DataFrame
    load_tv_trades_csv(path) -> list[dict]

    # Anchor detection (DST-aware)
    detect_alt_anchor_segments(tv_df, alt_tf_min) -> list[tuple[int, int]]
    anchor_for(ts_ms, segments) -> int

    # Simulation
    simulate_reversal_strategy(df, ...) -> list[dict]

    # Matching
    bar_by_bar_match(tv_df, my_df) -> dict
    trade_by_trade_match(tv_trades, my_trades, time_tolerance_min) -> dict

    # Reporting
    format_parity_report(stats, strategy_name, tv_chart_csv, tv_trades_csv) -> str
"""

from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import pandas as pd


# ── TV CSV loaders ──────────────────────────────────────────────────────


def load_tv_chart_csv(path: str | Path) -> pd.DataFrame:
    """Load TV's "Export chart data..." CSV output.

    TV's chart export uses `time` (unix seconds) + OHLC + volume, plus
    any plotted indicator columns. The `Long` and `Short` columns come
    from `plotshape()` calls in the Pine Script and are 1 at the bar
    where a long/short entry fires, NaN otherwise. Other indicator
    columns (like `condition`, `.position_size`) may also be present
    from `plot(..., display=display.data_window)` calls.

    Returns a DataFrame with columns:
        timestamp (ms), dt (UTC datetime), open, high, low, close,
        volume (if present), plus all indicator columns as-is.

    Also adds boolean helper columns:
        tv_long_entry:  True where Long == 1
        tv_short_entry: True where Short == 1

    These helpers let downstream matching code reference a consistent
    schema regardless of whether the strategy plotshape is called
    `Long`, `Buy`, `LE`, etc.
    """
    df = pd.read_csv(path)
    if "time" not in df.columns:
        raise ValueError(f"{path}: missing 'time' column (expected TV chart export format)")
    df = df.sort_values("time").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["timestamp"] = (df["time"] * 1000).astype("int64")

    # Helper boolean columns — support the common "Long" / "Short"
    # plotshape naming. Strategies that use different names can rename
    # before passing to match functions.
    df["tv_long_entry"] = df.get("Long", pd.Series(dtype=float)) == 1
    df["tv_short_entry"] = df.get("Short", pd.Series(dtype=float)) == 1
    return df


def load_tv_trades_csv(path: str | Path) -> list[dict]:
    """Parse TV's "Strategy Report → List of trades → download" CSV.

    TV's trades export has 2 rows per trade: Entry and Exit. Both rows
    carry the same realized P&L. We collapse each pair into a single
    trade dict. Trade numbers in the export are 1-indexed.

    Normalized schema:
        trade_num: int
        side: "long" | "short"
        entry_ts_ms, entry_dt, entry_price
        exit_ts_ms, exit_dt, exit_price
        pnl_usd, pnl_pct
    """
    pairs: dict[int, dict] = {}
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                tn = int(row["Trade #"])
            except (KeyError, ValueError):
                continue
            if tn not in pairs:
                pairs[tn] = {}
            row_type = row.get("Type", "")
            is_entry = row_type.startswith("Entry")
            side = "long" if "long" in row_type else "short"
            try:
                dt = datetime.strptime(row["Date and time"], "%Y-%m-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
            except (KeyError, ValueError):
                continue
            ts_ms = int(dt.timestamp() * 1000)
            try:
                price = float(row["Price USD"])
            except (KeyError, ValueError):
                continue

            if is_entry:
                pairs[tn]["entry_ts_ms"] = ts_ms
                pairs[tn]["entry_dt"] = row["Date and time"]
                pairs[tn]["entry_price"] = price
                pairs[tn]["side"] = side
            else:
                pairs[tn]["exit_ts_ms"] = ts_ms
                pairs[tn]["exit_dt"] = row["Date and time"]
                pairs[tn]["exit_price"] = price
                try:
                    pairs[tn]["pnl_usd"] = float(row.get("Net P&L USD", 0))
                    pairs[tn]["pnl_pct"] = float(row.get("Net P&L %", 0))
                except ValueError:
                    pairs[tn]["pnl_usd"] = 0.0
                    pairs[tn]["pnl_pct"] = 0.0

    trades: list[dict] = []
    for tn in sorted(pairs.keys()):
        p = pairs[tn]
        if "entry_ts_ms" in p and "exit_ts_ms" in p:
            p["trade_num"] = tn
            trades.append(p)
    return trades


# ── Anchor detection (DST-aware) ────────────────────────────────────────


def detect_alt_anchor_segments(
    tv_df: pd.DataFrame,
    alt_tf_min: int,
    *,
    long_col: str = "tv_long_entry",
    short_col: str = "tv_short_entry",
) -> list[tuple[int, int]]:
    """Infer piecewise-constant alt-TF anchor offsets from TV's entry bars.

    Pine Script's `request.security(sym, higher_tf, ...)` anchors the
    higher-timeframe bars to the EXCHANGE's session timezone, not UTC.
    When US DST starts/ends, the UTC alignment of these alt bars shifts
    by 60 minutes, which is `60 mod alt_tf_min` in the alt-TF grid.
    For a 40-min alt TF, that's a 20-minute shift.

    This function detects the shift by:
      1. Extracting all bars where TV fired an entry (long or short)
      2. Computing `timestamp mod (alt_tf_min * 60 * 1000)` for each
         (the "minute offset into the current alt bar")
      3. Grouping by date and finding the modal offset per day
      4. Collapsing consecutive days with the same anchor into segments

    Returns a sorted list of (from_ts_ms, anchor_min) tuples. For any
    bar, the applicable anchor is the one whose from_ts_ms is the
    largest ≤ the bar's timestamp. Use `anchor_for()` for lookups.

    If no entries are present, returns [(0, 0)] as a safe default.
    """
    entries = tv_df[tv_df.get(long_col, False) | tv_df.get(short_col, False)].copy()
    if len(entries) == 0:
        return [(0, 0)]

    alt_ms = alt_tf_min * 60 * 1000
    entries["mod_min"] = ((entries["timestamp"] % alt_ms) // 60000).astype(int)
    entries["date"] = entries["dt"].dt.date

    # Per-day modal anchor (most common mod value)
    def _mode(s: pd.Series) -> int:
        m = s.mode()
        return int(m.iloc[0]) if not m.empty else 0

    day_anchors = entries.groupby("date")["mod_min"].agg(_mode)

    # Collapse consecutive days with the same anchor into segments
    segments: list[tuple[int, int]] = []
    prev_anchor: int | None = None
    for day, anchor in day_anchors.items():
        if anchor != prev_anchor:
            ts_ms = int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)
            segments.append((ts_ms, int(anchor)))
            prev_anchor = int(anchor)

    # Ensure the first segment covers the first bar in the dataframe
    if segments:
        first_bar_ts = int(tv_df["timestamp"].iloc[0])
        if segments[0][0] > first_bar_ts:
            segments.insert(0, (first_bar_ts, segments[0][1]))

    return segments


def anchor_for(ts_ms: int, segments: list[tuple[int, int]]) -> int:
    """Look up the anchor offset applicable at the given timestamp.

    Step-function lookup: returns the anchor from the largest segment
    whose from_ts_ms is ≤ ts_ms. If ts_ms precedes all segments,
    returns the first segment's anchor.
    """
    if not segments:
        return 0
    best = segments[0][1]
    for seg_ts, seg_anchor in segments:
        if seg_ts <= ts_ms:
            best = seg_anchor
        else:
            break
    return best


# ── Alt-TF resampling + ALMA-style signal helpers ──────────────────────
#
# These are utilities for the COMMON case of a Pine-ported strategy that
# uses `request.security(sym, higher_tf, ..., lookahead=barmerge.lookahead_on)`
# to compute indicators on a higher timeframe and detect crossovers.
#
# A strategy's `detect_signals_lookahead` classmethod can compose these
# helpers to avoid reimplementing the lookahead + resample + crossover
# logic each time.


def resample_with_dynamic_anchor(
    df: pd.DataFrame,
    alt_tf_min: int,
    anchor_segments: list[tuple[int, int]],
    *,
    ts_col: str = "timestamp",
) -> pd.DataFrame:
    """Resample a base-TF DataFrame to a higher TF, respecting per-bar anchors.

    For each base bar, compute its containing alt-TF bar's `alt_ts`
    using the anchor applicable at that bar's timestamp. Then groupby
    `alt_ts` to aggregate OHLC.

    This handles DST transitions: within a single backtest window that
    spans a DST change, different bars use different anchors and
    produce a consistent alt-TF grid across the boundary.

    Returns:
        DataFrame with columns [alt_ts, open, high, low, close] — one
        row per alt-TF bar, sorted by alt_ts.
    """
    bar_ms = alt_tf_min * 60 * 1000
    df = df.copy()
    df["_anchor_min"] = df[ts_col].apply(lambda t: anchor_for(int(t), anchor_segments))
    df["alt_ts"] = (
        ((df[ts_col] - df["_anchor_min"] * 60_000) // bar_ms) * bar_ms
        + df["_anchor_min"] * 60_000
    )
    alt = df.groupby("alt_ts").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    return alt


def apply_lookahead_alma_cross(
    base_df: pd.DataFrame,
    alt_df: pd.DataFrame,
    *,
    alma_length: int,
    alma_offset: float,
    alma_sigma: float,
    ts_col: str = "timestamp",
) -> pd.DataFrame:
    """Compute ALMA(close) / ALMA(open) on alt-TF bars, apply lookahead.

    Pine Script semantics for `request.security(..., lookahead=barmerge.lookahead_on)`:
    at every base-TF bar inside a still-forming alt-TF bar, the function
    returns the FINAL value of that alt bar — which is future data in
    the historical backtest. This is a repainting bias.

    We replicate it by:
      1. Computing ALMA on the alt-TF bar series
      2. Merging each alt bar's ALMA values into ALL base bars that
         belong to it (via the `alt_ts` key)
      3. Masking the first (length - 1) rows to NaN so the `.fillna(0.0)`
         inside `_alma()` doesn't cause spurious crossovers at the
         warmup boundary

    Then crossover detection at the BASE bar level: le_trigger fires
    when prev_alma_close <= prev_alma_open AND current_alma_close >
    current_alma_open (where "prev" is the previous base bar via
    shift(1)).

    Returns the input base_df with added columns:
        alma_close, alma_open, le_trigger (bool), se_trigger (bool)
    """
    import numpy as np

    from src.data.feature_engine import _alma

    alt = alt_df.copy()
    alt["alma_close"] = _alma(alt["close"], alma_length, alma_offset, alma_sigma)
    alt["alma_open"] = _alma(alt["open"], alma_length, alma_offset, alma_sigma)
    # _alma fills NaN with 0, mask the first (length-1) warmup rows
    alt.loc[alt.index[:alma_length - 1], "alma_close"] = np.nan
    alt.loc[alt.index[:alma_length - 1], "alma_open"] = np.nan

    # Merge alt-TF values back into base bars via alt_ts
    # Caller must have already added alt_ts via resample_with_dynamic_anchor
    df = base_df.copy()
    if "alt_ts" not in df.columns:
        raise ValueError(
            "apply_lookahead_alma_cross requires base_df to have an 'alt_ts' column; "
            "call resample_with_dynamic_anchor first"
        )
    alt_lookup = alt[["alt_ts", "alma_close", "alma_open"]]
    df = df.merge(alt_lookup, on="alt_ts", how="left")

    # Detect crossovers at base-TF level
    df["alma_close_prev"] = df["alma_close"].shift(1)
    df["alma_open_prev"] = df["alma_open"].shift(1)
    valid = (
        df["alma_close"].notna()
        & df["alma_open"].notna()
        & df["alma_close_prev"].notna()
        & df["alma_open_prev"].notna()
    )
    df["le_trigger"] = valid & (
        (df["alma_close_prev"] <= df["alma_open_prev"])
        & (df["alma_close"] > df["alma_open"])
    )
    df["se_trigger"] = valid & (
        (df["alma_close_prev"] >= df["alma_open_prev"])
        & (df["alma_close"] < df["alma_open"])
    )
    return df


# ── Reversal simulator (generic, no strategy coupling) ──────────────────


@dataclass
class SimulatedTrade:
    trade_num: int
    side: str          # "long" | "short"
    entry_ts_ms: int
    entry_dt: str
    entry_price: float
    exit_ts_ms: int
    exit_dt: str
    exit_price: float
    pnl_pct: float
    pnl_usd: float
    equity_after: float


def simulate_reversal_strategy(
    df: pd.DataFrame,
    *,
    le_col: str = "le_trigger",
    se_col: str = "se_trigger",
    initial_equity: float = 1_000_000.0,
    position_pct: float = 0.10,
    ts_col: str = "timestamp",
    close_col: str = "close",
) -> list[dict]:
    """Simulate a reversal-only strategy under Pine defaults.

    Assumptions:
      - Each signal opens/reverses ONE position (no pyramiding)
      - On le_trigger while not long: close any short, open long at
        the current bar's close
      - On se_trigger while not short: close any long, open short at
        the current bar's close
      - Position notional = equity × position_pct (fixed % of equity)
      - Zero commission, zero slippage (matches Pine's defaults)
      - Same-bar flip: close + open happen at the same bar close

    The simulator is strategy-agnostic — it just needs two boolean
    columns (`le_trigger` / `se_trigger`) in the DataFrame. The
    strategy's job (or a helper function like
    `SwiftAlmaStrategy.detect_signals_lookahead`) is to produce those
    columns correctly.

    Returns a list of trade dicts with the same shape as
    `SimulatedTrade`. Use `[dataclasses.asdict(t) for t in trades]` if
    you need dict output from a list of SimulatedTrade instances —
    this function returns raw dicts directly for convenience.
    """
    trades: list[dict] = []
    side = 0  # 0 = flat, +1 = long, -1 = short
    entry_price = 0.0
    entry_ts = 0
    equity = float(initial_equity)

    for _, row in df.iterrows():
        close = float(row[close_col])
        ts = int(row[ts_col])

        le = bool(row.get(le_col, False))
        se = bool(row.get(se_col, False))

        if le and side != 1:
            # Close any short, then open long at same bar close
            if side == -1:
                pnl_frac = (entry_price - close) / entry_price
                notional = equity * position_pct
                pnl_usd = notional * pnl_frac
                equity += pnl_usd
                trades.append({
                    "trade_num": len(trades) + 1,
                    "side": "short",
                    "entry_ts_ms": int(entry_ts),
                    "entry_dt": pd.Timestamp(entry_ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                    "entry_price": float(entry_price),
                    "exit_ts_ms": int(ts),
                    "exit_dt": pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                    "exit_price": float(close),
                    "pnl_pct": float(pnl_frac * 100.0),
                    "pnl_usd": float(pnl_usd),
                    "equity_after": float(equity),
                })
            side = 1
            entry_price = close
            entry_ts = ts

        elif se and side != -1:
            if side == 1:
                pnl_frac = (close - entry_price) / entry_price
                notional = equity * position_pct
                pnl_usd = notional * pnl_frac
                equity += pnl_usd
                trades.append({
                    "trade_num": len(trades) + 1,
                    "side": "long",
                    "entry_ts_ms": int(entry_ts),
                    "entry_dt": pd.Timestamp(entry_ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                    "entry_price": float(entry_price),
                    "exit_ts_ms": int(ts),
                    "exit_dt": pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                    "exit_price": float(close),
                    "pnl_pct": float(pnl_frac * 100.0),
                    "pnl_usd": float(pnl_usd),
                    "equity_after": float(equity),
                })
            side = -1
            entry_price = close
            entry_ts = ts

    return trades


# ── Matchers ────────────────────────────────────────────────────────────


def bar_by_bar_match(
    tv_chart_df: pd.DataFrame,
    my_triggers_df: pd.DataFrame,
    *,
    tv_long_col: str = "tv_long_entry",
    tv_short_col: str = "tv_short_entry",
    my_long_col: str = "le_trigger",
    my_short_col: str = "se_trigger",
    ts_col: str = "timestamp",
) -> dict:
    """Exact-timestamp match between TV's entry markers and our triggers.

    Both inputs must have a `timestamp` column (unix ms). We extract
    the set of timestamps where TV fired entries and the set where our
    triggers fired, then compute set intersection and differences.

    Returns:
        {
            tv_longs: int, tv_shorts: int,
            my_longs: int, my_shorts: int,
            long_match: int, long_tv_only: list[int], long_mine_only: list[int],
            short_match: int, short_tv_only: list[int], short_mine_only: list[int],
            total_tv: int, total_my: int, total_match: int,
            total_match_pct: float,
        }
    """
    tv_long_ts = set(tv_chart_df[tv_chart_df[tv_long_col]][ts_col])
    tv_short_ts = set(tv_chart_df[tv_chart_df[tv_short_col]][ts_col])
    my_long_ts = set(my_triggers_df[my_triggers_df[my_long_col]][ts_col])
    my_short_ts = set(my_triggers_df[my_triggers_df[my_short_col]][ts_col])

    long_match = tv_long_ts & my_long_ts
    short_match = tv_short_ts & my_short_ts

    total_tv = len(tv_long_ts) + len(tv_short_ts)
    total_my = len(my_long_ts) + len(my_short_ts)
    total_match = len(long_match) + len(short_match)

    return {
        "tv_longs": len(tv_long_ts),
        "tv_shorts": len(tv_short_ts),
        "my_longs": len(my_long_ts),
        "my_shorts": len(my_short_ts),
        "long_match": len(long_match),
        "long_tv_only": sorted(tv_long_ts - my_long_ts),
        "long_mine_only": sorted(my_long_ts - tv_long_ts),
        "short_match": len(short_match),
        "short_tv_only": sorted(tv_short_ts - my_short_ts),
        "short_mine_only": sorted(my_short_ts - tv_short_ts),
        "total_tv": total_tv,
        "total_my": total_my,
        "total_match": total_match,
        "total_match_pct": (total_match / total_tv * 100.0) if total_tv else 0.0,
    }


def trade_by_trade_match(
    tv_trades: list[dict],
    my_trades: list[dict],
    *,
    time_tolerance_min: int = 80,
) -> dict:
    """Match trades by entry timestamp within tolerance + same side.

    For each TV trade, find the closest (by entry_ts_ms) MINE trade
    whose side agrees and is within ±time_tolerance_min of the TV
    entry time. Each MINE trade can only match one TV trade (greedy).

    Use this when only the TV trades CSV is available (no chart data).
    When chart data IS available, `bar_by_bar_match` is stricter and
    preferred.

    Returns dict with match counts, time/price distributions, and
    unmatched lists.
    """
    mine_sorted = sorted(my_trades, key=lambda t: t["entry_ts_ms"])
    mine_keys = [t["entry_ts_ms"] for t in mine_sorted]
    used_mine: set[int] = set()
    tol_ms = time_tolerance_min * 60 * 1000

    matched: list[dict] = []
    tv_only: list[dict] = []

    for tv in tv_trades:
        target = tv["entry_ts_ms"]
        lo = bisect_left(mine_keys, target - tol_ms)
        hi = bisect_right(mine_keys, target + tol_ms)
        candidates = [
            (i, mine_sorted[i]) for i in range(lo, hi)
            if i not in used_mine and mine_sorted[i]["side"] == tv["side"]
        ]
        if not candidates:
            tv_only.append(tv)
            continue
        best_i, best_mt = min(candidates, key=lambda c: abs(c[1]["entry_ts_ms"] - target))
        used_mine.add(best_i)
        time_diff_min = (best_mt["entry_ts_ms"] - target) / 60_000.0
        entry_price_diff = (best_mt["entry_price"] - tv["entry_price"]) / tv["entry_price"] * 100.0
        matched.append({
            "tv_trade_num": tv["trade_num"],
            "mine_trade_num": best_mt["trade_num"],
            "side": tv["side"],
            "time_diff_min": time_diff_min,
            "entry_price_diff_pct": entry_price_diff,
            "pnl_sign_agree": (best_mt["pnl_usd"] * tv["pnl_usd"]) >= 0,
        })

    mine_only = [t for i, t in enumerate(mine_sorted) if i not in used_mine]

    time_diffs_abs = sorted(abs(m["time_diff_min"]) for m in matched)
    entry_diffs_abs = sorted(abs(m["entry_price_diff_pct"]) for m in matched)

    def _median(xs: list[float]) -> float:
        return xs[len(xs) // 2] if xs else 0.0

    return {
        "tv_total": len(tv_trades),
        "mine_total": len(my_trades),
        "matched": len(matched),
        "tv_only": len(tv_only),
        "mine_only": len(mine_only),
        "tv_match_pct": len(matched) / len(tv_trades) * 100.0 if tv_trades else 0.0,
        "mine_match_pct": len(matched) / len(my_trades) * 100.0 if my_trades else 0.0,
        "median_time_diff_min": _median(time_diffs_abs),
        "median_entry_price_diff_pct": _median(entry_diffs_abs),
        "pnl_sign_agreement_pct": (
            sum(1 for m in matched if m["pnl_sign_agree"]) / len(matched) * 100.0
            if matched else 0.0
        ),
        "matched_sample": matched[:20],
        "tv_only_sample": tv_only[:20],
        "mine_only_sample": mine_only[:20],
    }


# ── Report writer ───────────────────────────────────────────────────────


def format_parity_report(
    *,
    strategy_name: str,
    tv_chart_csv: str,
    tv_trades_csv: str | None,
    bar_stats: dict,
    trade_stats: dict | None,
    anchor_segments: list[tuple[int, int]],
) -> str:
    """Return a human-readable Markdown report summarising the validation."""
    lines: list[str] = []
    lines.append(f"# TradingView Parity Report — {strategy_name}")
    lines.append("")
    lines.append(f"**Generated:** 2026-04-14")
    lines.append(f"**TV chart data:** `{tv_chart_csv}`")
    if tv_trades_csv:
        lines.append(f"**TV trades:** `{tv_trades_csv}`")
    lines.append("")

    lines.append("## Detected anchor segments (DST-aware)")
    lines.append("")
    if anchor_segments:
        lines.append("| From (UTC) | Anchor offset |")
        lines.append("|---|---|")
        for ts_ms, anchor in anchor_segments:
            dt = pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
            lines.append(f"| {dt} | +{anchor} min |")
    else:
        lines.append("*(none detected)*")
    lines.append("")

    lines.append("## Bar-by-bar match (entry timestamps)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| TV Long entries | {bar_stats['tv_longs']} |")
    lines.append(f"| TV Short entries | {bar_stats['tv_shorts']} |")
    lines.append(f"| My le_triggers | {bar_stats['my_longs']} |")
    lines.append(f"| My se_triggers | {bar_stats['my_shorts']} |")
    lines.append(f"| LONG match | {bar_stats['long_match']} / {bar_stats['tv_longs']} |")
    lines.append(f"| SHORT match | {bar_stats['short_match']} / {bar_stats['tv_shorts']} |")
    lines.append(f"| **Total** | **{bar_stats['total_match']} / {bar_stats['total_tv']} = {bar_stats['total_match_pct']:.2f}%** |")
    lines.append("")

    if bar_stats.get("long_tv_only"):
        lines.append(f"### TV Long entries NOT in mine ({len(bar_stats['long_tv_only'])})")
        lines.append("")
        for ts in bar_stats["long_tv_only"][:10]:
            dt = pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {dt}")
        if len(bar_stats["long_tv_only"]) > 10:
            lines.append(f"- *(+{len(bar_stats['long_tv_only']) - 10} more)*")
        lines.append("")

    if bar_stats.get("short_tv_only"):
        lines.append(f"### TV Short entries NOT in mine ({len(bar_stats['short_tv_only'])})")
        lines.append("")
        for ts in bar_stats["short_tv_only"][:10]:
            dt = pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {dt}")
        if len(bar_stats["short_tv_only"]) > 10:
            lines.append(f"- *(+{len(bar_stats['short_tv_only']) - 10} more)*")
        lines.append("")

    if trade_stats:
        lines.append("## Trade-by-trade match (TV trades CSV)")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| TV trades | {trade_stats['tv_total']} |")
        lines.append(f"| My trades | {trade_stats['mine_total']} |")
        lines.append(f"| Matched | {trade_stats['matched']} ({trade_stats['tv_match_pct']:.2f}%) |")
        lines.append(f"| TV-only | {trade_stats['tv_only']} |")
        lines.append(f"| Mine-only | {trade_stats['mine_only']} |")
        lines.append(f"| Median entry time offset | {trade_stats['median_time_diff_min']:.1f} min |")
        lines.append(f"| Median entry price offset | {trade_stats['median_entry_price_diff_pct']:.4f}% |")
        lines.append(f"| P&L sign agreement | {trade_stats['pnl_sign_agreement_pct']:.2f}% |")
        lines.append("")

    lines.append("## Verdict")
    lines.append("")
    pct = bar_stats["total_match_pct"]
    if pct >= 99.0:
        lines.append(f"**✅ PASS — {pct:.2f}% bar-by-bar match**")
        lines.append("")
        lines.append("Port is logically equivalent to Pine Script on the same data.")
        lines.append("Safe to proceed to Stage 1+ of strategy development.")
    elif pct >= 90.0:
        lines.append(f"**⚠️ NEAR MATCH — {pct:.2f}% bar-by-bar**")
        lines.append("")
        lines.append("Most signals align but there are gaps. Common causes:")
        lines.append("- Missing DST anchor transition in a segment")
        lines.append("- Different ALMA/indicator parameter values")
        lines.append("- Warmup truncation on the first few alt bars")
        lines.append("")
        lines.append("Investigate the unmatched entries listed above before proceeding.")
    else:
        lines.append(f"**❌ FAIL — only {pct:.2f}% bar-by-bar match**")
        lines.append("")
        lines.append("Port is NOT equivalent to Pine Script. Likely causes:")
        lines.append("- Wrong core indicator logic (e.g., ALMA vs EMA, wrong length)")
        lines.append("- Missing `lookahead_on` semantics")
        lines.append("- Wrong alt-TF multiplier")
        lines.append("- Missing signal entry condition")
        lines.append("")
        lines.append("Do NOT proceed to Stage 1 until this is resolved.")
    lines.append("")

    return "\n".join(lines)
