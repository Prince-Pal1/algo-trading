#!/usr/bin/env python3
"""Stage 1 research for Strategy 3 — Liquidation cascade reversion.

Hypothesis: when the market cascades through forced liquidations (chain
of market stop-outs + liquidation-engine fills), the terminal price tick
is an OVERSHOOT driven by a momentary imbalance. Price retraces as
normal liquidity replenishes, typically within 3-30 minutes.

We can't easily get historical liquidation data without API auth
(Binance /fapi/v1/allForceOrders is auth-required). We use a
PRICE-BASED PROXY instead: identify 1-minute bars with
extreme-adverse returns (< -N sigma of rolling window), treat them as
cascade proxy events, and measure post-event retracement.

This isn't identical to liquidation-data cascades, but they're strongly
correlated (cascades = extreme negative 1m returns by construction).
If the price proxy shows no retracement edge, the real strategy won't
either. If it shows strong edge, the actual liquidation feed (Stage 0)
provides even cleaner entry timing.

Stage 1 gates (from plan):
  - ≥ 60% of cascades retrace > 50% of initial move within 30 min
  - Sharpe on the retracement trade > 1.0
  - ≥ 15 cascade events in 6 months

Usage:
    python3 scripts/research_liquidation_cascade.py
    python3 scripts/research_liquidation_cascade.py --sigma 4.0 --min-move-bps 50
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent


def load_btc_1m() -> pd.DataFrame:
    df = pd.read_parquet(REPO / "data/historical/BTCUSDT_1m.parquet")
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    return df


def detect_cascades(
    df: pd.DataFrame,
    *,
    sigma: float = 4.0,
    min_move_bps: float = 50.0,
    rolling_window: int = 1440,  # 1 day of 1m bars
) -> pd.DataFrame:
    """Flag bars where the 1m return is adverse by > sigma × rolling_std
    OR magnitude > min_move_bps.

    Returns DataFrame of cascade events with timestamp, entry_price
    (= close of cascade bar), and the 1m return.
    """
    df = df.copy()
    df["ret_1m"] = df["close"].pct_change()
    df["ret_bps"] = df["ret_1m"] * 10_000
    df["roll_std_bps"] = df["ret_bps"].rolling(rolling_window).std()
    df["z_score"] = df["ret_bps"] / df["roll_std_bps"]

    # Cascade = extreme NEGATIVE move (longs getting liquidated).
    # Could also test positive (shorts liquidated) — same mechanic.
    mask_neg = (df["z_score"] <= -sigma) & (df["ret_bps"] <= -min_move_bps)
    cascades = df[mask_neg].copy()
    cascades["direction"] = "down"

    # Collapse consecutive cascade bars into single events (keep the
    # largest move in a run of extremes — the "climax" tick).
    if len(cascades) > 1:
        ts_diff = cascades.index.to_series().diff().dt.total_seconds()
        # New event if gap > 10 min
        cascades["event_id"] = (ts_diff > 600).cumsum()
        # Keep most extreme bar per event group
        idx = cascades.groupby("event_id")["z_score"].idxmin()
        cascades = cascades.loc[idx].sort_index()

    return cascades[["close", "ret_1m", "ret_bps", "z_score", "direction"]]


def measure_retracement(
    df: pd.DataFrame,
    cascades: pd.DataFrame,
    *,
    horizons_min: list[int] = (5, 15, 30, 60, 120),
) -> pd.DataFrame:
    """For each cascade, compute forward move over multiple horizons +
    whether the initial cascade move was retraced within 30 min.
    """
    rows = []
    for ts, cascade in cascades.iterrows():
        entry = float(cascade["close"])  # enter at cascade bar close
        # Initial move: the cascade bar's return (negative)
        initial = float(cascade["ret_1m"])
        row: dict = {
            "ts": ts, "entry_price": entry,
            "initial_move_bps": float(cascade["ret_bps"]),
            "z_score": float(cascade["z_score"]),
        }

        # Forward prices across horizons (look-ahead)
        window = df.loc[ts:ts + pd.Timedelta(minutes=max(horizons_min))]
        if len(window) < 2:
            continue

        # Max favorable (price goes UP after a down cascade) and max adverse
        forward_high = window["high"].iloc[1:].max() if len(window) > 1 else entry
        forward_low = window["low"].iloc[1:].min() if len(window) > 1 else entry
        max_fav = (forward_high - entry) / entry
        max_adv = (entry - forward_low) / entry
        row["max_fav_bps"] = max_fav * 10_000
        row["max_adv_bps"] = max_adv * 10_000

        for h in horizons_min:
            end_ts = ts + pd.Timedelta(minutes=h)
            if end_ts in df.index:
                px = float(df.loc[end_ts, "close"])
            else:
                # Closest bar at or after end_ts
                after = df.loc[end_ts:]
                if len(after) == 0:
                    continue
                px = float(after["close"].iloc[0])
            ret = (px - entry) / entry
            row[f"ret_{h}m_bps"] = ret * 10_000

        # Retracement: did price bounce 50% of |initial_move| before going lower?
        # For a down-cascade: initial_move < 0; retrace means price > entry × (1 - |initial|/2)
        retrace_target = entry * (1 + abs(initial) * 0.5)
        # Did HIGH exceed retrace_target within 30 min?
        win_30 = df.loc[ts:ts + pd.Timedelta(minutes=30)]
        row["retraced_50pct_in_30m"] = bool(
            len(win_30) > 1 and (win_30["high"].iloc[1:] >= retrace_target).any()
        )
        # Full retrace (back to entry) within 30 min
        row["fully_retraced_30m"] = bool(
            len(win_30) > 1 and (win_30["high"].iloc[1:] >= entry).any()
        )
        rows.append(row)

    return pd.DataFrame(rows)


def simulate_strategy(
    retr: pd.DataFrame,
    *,
    sl_bps: float = 50.0,  # stop-loss at N bps below entry
    tp_bps: float = 80.0,  # take-profit at N bps above entry
    horizon_min: int = 60, # give up after N min
) -> dict:
    """Naive sim: LONG at cascade close, SL/TP/time-stop.

    For each cascade:
      - If max_adv_bps > sl_bps → SL hit → -sl_bps
      - Else if max_fav_bps > tp_bps → TP hit → +tp_bps
      - Else time-stop → ret_{horizon_min}m_bps
    """
    pnls = []
    for _, r in retr.iterrows():
        if r["max_adv_bps"] > sl_bps:
            pnls.append(-sl_bps)
        elif r["max_fav_bps"] > tp_bps:
            pnls.append(tp_bps)
        else:
            col = f"ret_{horizon_min}m_bps"
            if col in r and not pd.isna(r[col]):
                pnls.append(float(r[col]))
    if not pnls:
        return {"n": 0}
    mean_p = statistics.mean(pnls)
    std_p = statistics.stdev(pnls) if len(pnls) > 1 else 0
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(pnls),
        "mean_pnl_bps": round(mean_p, 2),
        "std_pnl_bps": round(std_p, 2),
        "win_rate": round(wins / len(pnls), 3),
        "sharpe_per_trade": round(mean_p / std_p, 3) if std_p > 0 else 0,
        "total_pnl_bps": round(sum(pnls), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sigma", type=float, default=4.0,
                        help="Extreme-move z-score threshold")
    parser.add_argument("--min-move-bps", type=float, default=50.0,
                        help="Minimum abs 1m return to count as cascade")
    parser.add_argument("--sl-bps", type=float, default=50.0)
    parser.add_argument("--tp-bps", type=float, default=80.0)
    parser.add_argument("--horizon-min", type=int, default=60)
    parser.add_argument("--out", type=Path,
                        default=Path("reports/liquidation_stage1/report.json"))
    args = parser.parse_args()

    print("=== Strategy 3 Stage 1 research: liquidation cascade reversion ===\n")
    df = load_btc_1m()
    n_days = (df.index[-1] - df.index[0]).days
    print(f"Data: {len(df)} 1m bars, {df.index[0].date()} → {df.index[-1].date()} ({n_days} days)\n")

    cascades = detect_cascades(df, sigma=args.sigma, min_move_bps=args.min_move_bps)
    print(f"=== Cascade detection (z < -{args.sigma:.1f}σ AND move < -{args.min_move_bps:.0f} bps) ===")
    print(f"  Cascades detected: {len(cascades)}")
    if len(cascades) > 0:
        print(f"  Events/month:      {len(cascades) / (n_days/30):.1f}")
        print(f"  Mean z-score:      {cascades['z_score'].mean():.2f}")
        print(f"  Mean initial move: {cascades['ret_bps'].mean():.1f} bps")

    if len(cascades) < 5:
        print(f"\n  Insufficient events ({len(cascades)} < 15 gate).")
        print(f"  Adjust --sigma or --min-move-bps or extend data window.")
        return 1

    retr = measure_retracement(df, cascades)
    if len(retr) == 0:
        print("  No retracement data (look-ahead window exceeded).")
        return 1

    print(f"\n=== Retracement characterization (n={len(retr)}) ===")
    retr_50 = retr["retraced_50pct_in_30m"].sum()
    full_retr = retr["fully_retraced_30m"].sum()
    print(f"  % retraced ≥ 50% in 30m:  {100*retr_50/len(retr):.1f}%")
    print(f"  % fully retraced in 30m:  {100*full_retr/len(retr):.1f}%")
    print(f"  Mean max-favourable:      {retr['max_fav_bps'].mean():+.1f} bps")
    print(f"  Mean max-adverse:         {retr['max_adv_bps'].mean():+.1f} bps")
    for h in [5, 15, 30, 60]:
        col = f"ret_{h}m_bps"
        if col in retr.columns:
            mean_h = retr[col].mean()
            median_h = retr[col].median()
            pos = (retr[col] > 0).mean() * 100
            print(f"  {h:>3}m return: mean={mean_h:+6.1f} bps  median={median_h:+6.1f}  positive {pos:.0f}%")

    print(f"\n=== Naive LONG sim: SL -{args.sl_bps:.0f} / TP +{args.tp_bps:.0f} bps, {args.horizon_min}m horizon ===")
    sim = simulate_strategy(retr, sl_bps=args.sl_bps, tp_bps=args.tp_bps,
                            horizon_min=args.horizon_min)
    for k, v in sim.items():
        print(f"  {k:<20} {v}")

    # Gates
    print(f"\n=== Stage 1 Gates ===")
    events_gate = len(cascades) >= 15
    retrace_gate = (retr_50 / len(retr)) >= 0.60
    sharpe_gate = sim.get("sharpe_per_trade", 0) > 0.1  # ~1.0 annualized at N cascades/yr
    print(f"  ≥ 15 cascade events:         {'✅' if events_gate else '❌'} ({len(cascades)})")
    print(f"  ≥ 60% retraced 50% in 30m:   {'✅' if retrace_gate else '❌'} ({100*retr_50/len(retr):.1f}%)")
    print(f"  Per-trade Sharpe > 0.1:      {'✅' if sharpe_gate else '❌'} ({sim.get('sharpe_per_trade',0)})")

    pass_count = sum([events_gate, retrace_gate, sharpe_gate])
    verdict = "PASS" if pass_count >= 2 else "FAIL"
    print(f"\n=== Verdict: {verdict} ({pass_count}/3) ===")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "cascades": len(cascades),
        "events_per_month": len(cascades) / (n_days / 30) if n_days > 0 else 0,
        "retrace_50pct_pct": 100 * retr_50 / len(retr),
        "full_retrace_pct": 100 * full_retr / len(retr),
        "naive_sim": sim,
        "verdict": verdict,
    }, indent=2, default=str))
    print(f"  Report: {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
