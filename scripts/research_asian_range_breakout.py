#!/usr/bin/env python3
"""Stage 1 research for Strategy 2' — Asian session range breakout (XAUUSD).

The hypothesis: during the London session (07:00-11:00 UTC), XAUUSD
frequently breaks out of the range established during the Asian session
(00:00-07:00 UTC). The breakout direction often extends by several times
the range size before reversing.

This script tests whether the pattern has real edge on 2y of XAUUSD 5m
data (resampled to 15m). Computes:
  - Days where a breakout occurred (above Asian high or below Asian low
    during London 07:00-11:00 UTC).
  - Post-breakout return at 4h, 8h horizons.
  - Hit rate (% of breakouts that continue ≥ 1× range).
  - Reward/risk if using Asian-mid as stop.

Stage 1 gates:
  - Breakout frequency ≥ 50% of trading days (otherwise sparse edge).
  - Directional hit rate ≥ 55% (directional edge exists).
  - Average post-breakout return > 0.5× Asian range (reward/risk > 1.0).

Usage:
    python3 scripts/research_asian_range_breakout.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent


def load_and_resample(tf: str = "15min") -> pd.DataFrame:
    df = pd.read_parquet(REPO / "data/historical/XAUUSD_5m.parquet")
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("ts")
    agg = df.resample(tf).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return agg


def analyze_days(df: pd.DataFrame, *,
                 asian_start: str = "00:00",
                 asian_end: str = "07:00",
                 london_end: str = "11:00",
                 extend_horizon_hours: int = 8) -> list[dict]:
    """Per trading day, extract the Asian range + London breakout outcome."""
    results = []

    # Group by date (UTC)
    df["date"] = df.index.date
    for date, day_df in df.groupby("date"):
        # Asian session bars
        asia = day_df.between_time(asian_start, asian_end, inclusive="left")
        if len(asia) < 10:  # need enough bars for a meaningful range
            continue
        asian_high = asia["high"].max()
        asian_low = asia["low"].min()
        asian_range = asian_high - asian_low
        asian_mid = (asian_high + asian_low) / 2
        if asian_range <= 0:
            continue

        # London breakout window
        london = day_df.between_time(asian_end, london_end, inclusive="left")
        if len(london) == 0:
            continue

        # First breakout detection
        breakout_dir = None
        breakout_bar = None
        breakout_price = None
        for ts, row in london.iterrows():
            if row["high"] > asian_high and breakout_dir is None:
                breakout_dir = "UP"
                breakout_bar = ts
                breakout_price = asian_high  # assume fill at break
                break
            if row["low"] < asian_low and breakout_dir is None:
                breakout_dir = "DOWN"
                breakout_bar = ts
                breakout_price = asian_low
                break

        if breakout_dir is None:
            results.append({
                "date": str(date), "breakout": False,
                "asian_range": float(asian_range),
            })
            continue

        # Post-breakout outcome: track max favourable move + max adverse move
        # over extend_horizon_hours AFTER the breakout.
        end_ts = breakout_bar + pd.Timedelta(hours=extend_horizon_hours)
        forward = day_df.loc[breakout_bar:end_ts]
        if len(forward) < 2:
            continue

        if breakout_dir == "UP":
            max_fav = forward["high"].max() - breakout_price
            max_adv = breakout_price - forward["low"].min()
            final_price = forward["close"].iloc[-1]
            final_move = final_price - breakout_price
        else:  # DOWN
            max_fav = breakout_price - forward["low"].min()
            max_adv = forward["high"].max() - breakout_price
            final_price = forward["close"].iloc[-1]
            final_move = breakout_price - final_price

        # Normalize by range
        results.append({
            "date": str(date),
            "breakout": True,
            "direction": breakout_dir,
            "asian_range": float(asian_range),
            "breakout_price": float(breakout_price),
            "max_fav_raw": float(max_fav),
            "max_adv_raw": float(max_adv),
            "final_move_raw": float(final_move),
            "max_fav_over_range": float(max_fav / asian_range),
            "max_adv_over_range": float(max_adv / asian_range),
            "final_move_over_range": float(final_move / asian_range),
            "hit_1x_range": bool(max_fav >= asian_range),
            "hit_2x_range": bool(max_fav >= 2 * asian_range),
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", default="15min")
    parser.add_argument("--asian-start", default="00:00")
    parser.add_argument("--asian-end", default="07:00")
    parser.add_argument("--london-end", default="11:00")
    parser.add_argument("--horizon-hours", type=int, default=8)
    parser.add_argument("--out", type=Path,
                        default=Path("reports/asian_range_stage1/report.json"))
    args = parser.parse_args()

    print("=== Asian Range Breakout Stage 1 research (XAUUSD) ===\n")
    df = load_and_resample(args.tf)
    n_years = (df.index[-1] - df.index[0]).days / 365.25
    print(f"Data: {len(df)} {args.tf} bars, "
          f"{df.index[0].date()} → {df.index[-1].date()} ({n_years:.1f}y)\n")

    results = analyze_days(
        df,
        asian_start=args.asian_start, asian_end=args.asian_end,
        london_end=args.london_end, extend_horizon_hours=args.horizon_hours,
    )

    total_days = len(results)
    breakout_days = [r for r in results if r["breakout"]]
    n_breakouts = len(breakout_days)
    print(f"=== Summary ===")
    print(f"  Total days analysed:          {total_days}")
    print(f"  Breakout days:                {n_breakouts} ({100*n_breakouts/total_days:.1f}%)")

    if not breakout_days:
        print("  No breakouts found — kill.")
        return 1

    up = [r for r in breakout_days if r["direction"] == "UP"]
    dn = [r for r in breakout_days if r["direction"] == "DOWN"]
    print(f"    UP breakouts:               {len(up)}")
    print(f"    DOWN breakouts:             {len(dn)}")

    print()
    print(f"=== Post-breakout outcome (within {args.horizon_hours}h) ===")

    max_fav = [r["max_fav_over_range"] for r in breakout_days]
    max_adv = [r["max_adv_over_range"] for r in breakout_days]
    final = [r["final_move_over_range"] for r in breakout_days]
    hit_1x = sum(1 for r in breakout_days if r["hit_1x_range"])
    hit_2x = sum(1 for r in breakout_days if r["hit_2x_range"])
    final_positive = sum(1 for r in breakout_days if r["final_move_over_range"] > 0)

    print(f"  Max favourable (× range): mean={statistics.mean(max_fav):.2f}  "
          f"median={statistics.median(max_fav):.2f}")
    print(f"  Max adverse (× range):    mean={statistics.mean(max_adv):.2f}  "
          f"median={statistics.median(max_adv):.2f}")
    print(f"  Final move (× range):     mean={statistics.mean(final):+.3f}  "
          f"median={statistics.median(final):+.3f}")
    print(f"  % hit 1× range fav:       {100*hit_1x/n_breakouts:.1f}%")
    print(f"  % hit 2× range fav:       {100*hit_2x/n_breakouts:.1f}%")
    print(f"  % final positive:         {100*final_positive/n_breakouts:.1f}%")

    # Naive strategy PnL: enter on breakout, stop at Asian mid (= half range away),
    # take profit at 1× range, exit at horizon-close otherwise.
    print()
    print(f"=== Naive sim: enter at breakout, SL at Asian mid, TP at 1× range ===")
    pnls = []  # in units of Asian-range
    for r in breakout_days:
        # SL = break_price - 0.5*range (UP) or break_price + 0.5*range (DOWN)
        # Represented in range units:
        # If max_adv > 0.5 (price retraced past Asian mid), SL hit: loss = -0.5
        # Else if max_fav >= 1.0: win = +1.0
        # Else: final move
        if r["max_adv_over_range"] > 0.5:
            pnls.append(-0.5)
        elif r["max_fav_over_range"] >= 1.0:
            pnls.append(+1.0)
        else:
            pnls.append(r["final_move_over_range"])
    mean_pnl = statistics.mean(pnls)
    std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 0
    win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
    print(f"  trades: {len(pnls)}")
    print(f"  mean PnL (× range): {mean_pnl:+.3f}")
    print(f"  std PnL  (× range): {std_pnl:.3f}")
    print(f"  win rate: {win_rate*100:.1f}%")
    if std_pnl > 0:
        # Per-trade Sharpe × sqrt(trades/year) for annualized
        trades_per_year = len(pnls) / n_years
        sharpe_ann = (mean_pnl / std_pnl) * (trades_per_year ** 0.5)
        print(f"  annualized Sharpe (unitless): {sharpe_ann:+.3f}")

    # Gates
    print()
    print("=== Stage 1 gates ===")
    freq_gate = n_breakouts / total_days >= 0.50
    hit_gate = hit_1x / n_breakouts >= 0.55
    edge_gate = mean_pnl > 0 and mean_pnl / (std_pnl or 1) > 0.1  # rough per-trade Sharpe > 0.1
    print(f"  Breakout frequency ≥ 50%:      {'✅' if freq_gate else '❌'} "
          f"({100*n_breakouts/total_days:.1f}%)")
    print(f"  Hit 1× range ≥ 55%:            {'✅' if hit_gate else '❌'} "
          f"({100*hit_1x/n_breakouts:.1f}%)")
    print(f"  Naive-sim edge positive:       {'✅' if edge_gate else '❌'} "
          f"(mean={mean_pnl:+.3f}, Sharpe≈{mean_pnl/(std_pnl or 1):+.3f})")

    pass_count = sum([freq_gate, hit_gate, edge_gate])
    verdict = "PASS" if pass_count >= 2 else "FAIL"
    print(f"\n=== Verdict: {verdict} ({pass_count}/3 gates) ===")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "total_days": total_days,
        "breakout_days": n_breakouts,
        "up_down": {"up": len(up), "down": len(dn)},
        "hit_1x_pct": 100 * hit_1x / n_breakouts,
        "hit_2x_pct": 100 * hit_2x / n_breakouts,
        "naive_mean_pnl_range": mean_pnl,
        "naive_std_pnl_range": std_pnl,
        "naive_win_rate": win_rate,
        "verdict": verdict,
    }, indent=2, default=str))
    print(f"  Report: {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
