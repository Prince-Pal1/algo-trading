#!/usr/bin/env python3
"""Benchmark: Hard 6-Condition AND-Gate vs Weighted Composite Score on XAUUSD 1m.

Compares detection efficiency, false negative rescues, and trigger distributions
across historical XAUUSD data (specifically March 24-28, 2026 and broader dataset).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def load_data(filepath: str, start_dt: str = "2026-03-24", end_dt: str = "2026-03-29") -> pd.DataFrame:
    df = pd.read_parquet(filepath)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")
    if start_dt and end_dt:
        df = df[(df["dt"] >= start_dt) & (df["dt"] <= end_dt)].copy().reset_index(drop=True)
    
    # Compute 1m ATR_14
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean().bfill()
    return df


def evaluate_window(sub: pd.DataFrame, window_size: int = 10) -> dict:
    o = sub["open"].values
    h = sub["high"].values
    l = sub["low"].values
    c = sub["close"].values
    atr = sub["atr_14"].values[-1]

    ranges = h - l
    ranges_safe = np.where(ranges > 0, ranges, 1e-6)
    bodies = np.abs(c - o)
    body_pcts = bodies / ranges_safe
    med_body_pct = float(np.median(body_pcts))

    net_move = float(c[-1] - c[0])
    abs_net_move = abs(net_move)
    is_down = net_move < 0

    # 1. Directional consistency ratio
    if is_down:
        dir_count = np.sum(c[1:] < c[:-1])
    else:
        dir_count = np.sum(c[1:] > c[:-1])
    dir_ratio = float(dir_count / (window_size - 1))

    # 2. CV of ranges
    mean_range = float(np.mean(ranges))
    std_range = float(np.std(ranges))
    cv = std_range / mean_range if mean_range > 0 else 1.0

    # 3. Spike check: max single bar vs 3x median
    med_range = float(np.median(ranges))
    max_range = float(np.max(ranges))
    spike_ok = bool(max_range <= (3.0 * med_range))

    # 4. Linearity / R2
    x = np.arange(window_size)
    y = c
    if np.std(y) > 1e-6:
        corr = np.corrcoef(x, y)[0, 1]
        r2 = float(corr**2) if not np.isnan(corr) else 0.0
    else:
        r2 = 0.0

    # 5. Kaufman Efficiency Ratio (ER)
    path_len = float(np.sum(np.abs(c[1:] - c[:-1])))
    er = float(abs_net_move / path_len) if path_len > 0 else 0.0

    # --- Method A: Hard 6-Gate (AND) ---
    c1 = med_body_pct >= 0.65
    c2 = dir_ratio >= 0.70
    c3 = cv <= 0.50
    c4 = abs_net_move >= 5.0
    c5 = spike_ok
    c6 = r2 >= 0.85
    hard_pass = bool(c1 and c2 and c3 and c4 and c5 and c6)
    hard_failures = [
        name for name, passed in [
            ("1.BodyPct<0.65", c1),
            ("2.DirRatio<0.70", c2),
            ("3.CV>0.50", c3),
            ("4.Leg<$5", c4),
            ("5.Spike>3x", c5),
            ("6.R2<0.85", c6),
        ] if not passed
    ]

    # --- Method B: Weighted Composite Score (0 - 100) ---
    # ER Score (0 - 25 pts): ER=0.80 gets full marks
    er_score = min(25.0, (er / 0.80) * 25.0)

    # Linearity / R2 Score (0 - 25 pts): R2=0.90 gets full marks
    r2_score = min(25.0, (r2 / 0.90) * 25.0)

    # Directional Persistence (0 - 20 pts): dir_ratio=0.80 gets full marks
    dir_score = min(20.0, (dir_ratio / 0.80) * 20.0)

    # ATR-Relative Displacement (0 - 15 pts): net move >= 2.5x 1m ATR gets full marks
    atr_norm_move = abs_net_move / atr if (atr and atr > 0) else abs_net_move / 2.0
    move_score = min(15.0, (atr_norm_move / 2.5) * 15.0)

    # Smoothness & Body Quality (0 - 15 pts)
    # CV component (7.5 pts, penalized smoothly as CV rises)
    cv_score = max(0.0, min(7.5, (1.0 - cv) * 7.5))
    # Body pct component (7.5 pts)
    body_score = min(7.5, (med_body_pct / 0.75) * 7.5)
    smooth_score = cv_score + body_score

    composite_score = er_score + r2_score + dir_score + move_score + smooth_score
    weighted_pass = bool(composite_score >= 75.0)

    return {
        "hard_pass": hard_pass,
        "weighted_pass": weighted_pass,
        "composite_score": composite_score,
        "er": er,
        "r2": r2,
        "dir_ratio": dir_ratio,
        "abs_net_move": abs_net_move,
        "atr_norm_move": atr_norm_move,
        "cv": cv,
        "med_body_pct": med_body_pct,
        "hard_failures": hard_failures,
        "direction": "SHORT" if is_down else "LONG",
    }


def run_benchmark(filepath: str = "data/historical/XAUUSD_1m.parquet", window_size: int = 10):
    print("=" * 80)
    print("GOLD SCALP ZONE DETECTOR: HARD 6-GATE vs WEIGHTED COMPOSITE BENCHMARK")
    print("=" * 80)

    # 1. Test on Reference Week: March 24 - 28, 2026
    df_ref = load_data(filepath, "2026-03-24", "2026-03-29")
    print(f"\n[Test 1] Reference Period: March 24 - 28, 2026 ({len(df_ref):,} 1m bars)")

    records = []
    for i in range(window_size + 14, len(df_ref)):
        sub = df_ref.iloc[i - window_size : i]
        res = evaluate_window(sub, window_size)
        res["time"] = df_ref["dt"].iloc[i - 1]
        res["close"] = df_ref["close"].iloc[i - 1]
        records.append(res)

    res_df = pd.DataFrame(records)

    hard_triggers = res_df["hard_pass"].sum()
    weighted_triggers = res_df["weighted_pass"].sum()
    both_triggers = (res_df["hard_pass"] & res_df["weighted_pass"]).sum()
    rescued = res_df[res_df["weighted_pass"] & (~res_df["hard_pass"])]
    false_positives_avoided = res_df[res_df["hard_pass"] & (~res_df["weighted_pass"])]

    print(f"Total 1m Windows Scanned:         {len(res_df):,}")
    print(f"Hard 6-Gate Triggers:             {hard_triggers} ({hard_triggers/len(res_df)*100:.2f}%)")
    print(f"Weighted Composite (>=75) Triggers: {weighted_triggers} ({weighted_triggers/len(res_df)*100:.2f}%)")
    print(f"Overlap (Both Triggered):         {both_triggers}")
    print(f"Rescued Clean Zones (Weighted OK, Hard Failed): {len(rescued)}")

    print("\n--- Failure Breakdown of the Rescued Clean Zones ---")
    failure_counts = {}
    for fails in rescued["hard_failures"]:
        for f in fails:
            failure_counts[f] = failure_counts.get(f, 0) + 1
    for k, v in sorted(failure_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {k:20s}: Failed {v} times in otherwise high-scoring zones ({v/len(rescued)*100:.1f}%)")

    print("\n--- Top 6 Rescued Example Windows (Why Hard Gate Dropped Them) ---")
    for idx, row in rescued.sort_values(by="composite_score", ascending=False).head(6).iterrows():
        print(f"Time: {row['time']} | Dir: {row['direction']:5s} | Score: {row['composite_score']:.1f}/100")
        print(f"  ER: {row['er']:.2f} | R²: {row['r2']:.2f} | Dir%: {row['dir_ratio']*100:.0f}% | Move: ${row['abs_net_move']:.2f} ({row['atr_norm_move']:.1f}x ATR) | CV: {row['cv']:.2f}")
        print(f"  Hard-Gate Rejection Reasons: {', '.join(row['hard_failures'])}\n")

    # 2. Test on 1 Full Month (March 2026) for Stability & Frequency
    df_month = load_data(filepath, "2026-03-01", "2026-04-01")
    print(f"\n[Test 2] Full Month Stability Test: March 2026 ({len(df_month):,} 1m bars)")

    records_m = []
    for i in range(window_size + 14, len(df_month)):
        sub = df_month.iloc[i - window_size : i]
        res = evaluate_window(sub, window_size)
        res["time"] = df_month["dt"].iloc[i - 1]
        records_m.append(res)
    res_m = pd.DataFrame(records_m)

    h_m = res_m["hard_pass"].sum()
    w_m = res_m["weighted_pass"].sum()
    resc_m = len(res_m[res_m["weighted_pass"] & (~res_m["hard_pass"])])

    print(f"Total 1m Windows in Month:        {len(res_m):,}")
    print(f"Hard 6-Gate Triggers:             {h_m} ({h_m/len(res_m)*100:.2f}%)")
    print(f"Weighted Composite (>=75):        {w_m} ({w_m/len(res_m)*100:.2f}%)")
    print(f"Total Rescued Clean Scalp Setups: {resc_m}")


if __name__ == "__main__":
    run_benchmark()
