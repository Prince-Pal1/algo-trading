#!/usr/bin/env python3
"""G.5 — validate the Brownian bridge intrabar path model against M1 ground truth.

The BrownianBridgeModel in src/backtest/path.py estimates intrabar hit
ordering for SL/TP levels given only M5 OHLC. G.5 asks: is the model's
hit-count error < 10% on real data?

We cheat the "tick data" requirement by using M1 bars as ground truth
inside each M5 bar. Each M5 bar contains 5 consecutive M1 bars, so for
any level inside [m5_low, m5_high], we can determine which of the 5 M1
bars was the first to touch it — that's the true fraction_into_bar
with a 0.2 bin resolution.

We compare against the bridge model's prediction in two modes:
1. LEVEL TIMING: for a synthetic level inside each bar, measure
   |predicted_fraction - true_fraction|. Report mean, median, p90, p99.
2. BOTH-HIT ORDERING: when two levels are both inside a bar's range,
   measure whether the model correctly identifies which was hit first.

Report card + decision gate (> 80% both-hit agreement OR < 0.25 mean
fraction error → bridge model is sufficient, proceed with Tier 5 backtests).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.backtest.path import Bar, BrownianBridgeModel


M5_PATH = "data/historical/XAUUSD_5m.parquet"
M1_PATH = "data/historical/XAUUSD_1m.parquet"
SAMPLE_SIZE = 2000
RUN_ID = "g5_validate"


@dataclass
class LevelTimingResult:
    n_tested: int
    mean_error: float
    median_error: float
    p90_error: float
    p99_error: float


@dataclass
class BothHitResult:
    n_tested: int
    n_agree: int
    agreement_pct: float


def _group_m1_by_m5(m1: pd.DataFrame) -> dict[int, list[int]]:
    """Return a dict mapping M5 timestamp → list of M1 row indices inside it.

    An M5 bar starting at timestamp T (ms) contains M1 bars at T, T+60000,
    T+120000, T+180000, T+240000.
    """
    m1_by_ts: dict[int, int] = {int(row.timestamp): i for i, row in m1.iterrows()}
    grouped: dict[int, list[int]] = {}
    for m5_ts in range(int(m1["timestamp"].min()), int(m1["timestamp"].max()) + 300_000, 300_000):
        indices = []
        for offset in range(5):
            m1_ts = m5_ts + offset * 60_000
            if m1_ts in m1_by_ts:
                indices.append(m1_by_ts[m1_ts])
        if len(indices) == 5:
            grouped[m5_ts] = indices
    return grouped


def _m1_fraction_for_level(m1_bars: pd.DataFrame, level: float) -> float | None:
    """Find the first M1 bar (of 5) that touched `level`.
    Returns fraction = (m1_idx + 0.5) / 5, or None if untouched.
    """
    for i in range(len(m1_bars)):
        bar = m1_bars.iloc[i]
        if bar["low"] <= level <= bar["high"]:
            return (i + 0.5) / 5.0
    return None


def _level_timing_validation(
    m5: pd.DataFrame,
    m1: pd.DataFrame,
    m1_groups: dict[int, list[int]],
    rng: np.random.Generator,
) -> LevelTimingResult:
    """For SAMPLE_SIZE random M5 bars, compute bridge predicted fraction
    vs M1 ground-truth fraction for a level placed inside the bar.
    """
    model = BrownianBridgeModel(run_id=RUN_ID)
    errors: list[float] = []

    # Filter m5 rows that have 5 M1 bars inside
    m5_valid = m5[m5["timestamp"].isin(m1_groups.keys())].reset_index(drop=True)
    if len(m5_valid) == 0:
        return LevelTimingResult(0, 0.0, 0.0, 0.0, 0.0)

    sample_idx = rng.choice(len(m5_valid), size=min(SAMPLE_SIZE, len(m5_valid)), replace=False)

    for bar_idx in sample_idx:
        row = m5_valid.iloc[int(bar_idx)]
        bar_low = float(row["low"])
        bar_high = float(row["high"])
        if bar_high - bar_low < 0.1:
            continue

        # Place a level at a random position 20-80% between low and high
        frac_in_range = 0.2 + rng.random() * 0.6
        level = bar_low + frac_in_range * (bar_high - bar_low)

        bar = Bar(
            open=float(row["open"]), high=bar_high, low=bar_low,
            close=float(row["close"]),
            ts_ms=int(row["timestamp"]),
        )
        predicted = model.fraction_into_bar(bar, level, bar_idx=int(bar_idx))
        if predicted is None:
            continue

        m1_indices = m1_groups[int(row["timestamp"])]
        m1_bars = m1.iloc[m1_indices].reset_index(drop=True)
        ground_truth = _m1_fraction_for_level(m1_bars, level)
        if ground_truth is None:
            continue

        errors.append(abs(predicted - ground_truth))

    if not errors:
        return LevelTimingResult(0, 0.0, 0.0, 0.0, 0.0)
    errors_arr = np.array(errors)
    return LevelTimingResult(
        n_tested=len(errors),
        mean_error=float(errors_arr.mean()),
        median_error=float(np.median(errors_arr)),
        p90_error=float(np.quantile(errors_arr, 0.90)),
        p99_error=float(np.quantile(errors_arr, 0.99)),
    )


def _both_hit_validation(
    m5: pd.DataFrame,
    m1: pd.DataFrame,
    m1_groups: dict[int, list[int]],
    rng: np.random.Generator,
) -> BothHitResult:
    """For bars where two levels are both inside [low, high], check whether
    the bridge model correctly identifies which was hit first vs the M1
    ground truth."""
    model = BrownianBridgeModel(run_id=RUN_ID)
    n_tested = 0
    n_agree = 0

    m5_valid = m5[m5["timestamp"].isin(m1_groups.keys())].reset_index(drop=True)
    if len(m5_valid) == 0:
        return BothHitResult(0, 0, 0.0)

    sample_idx = rng.choice(len(m5_valid), size=min(SAMPLE_SIZE, len(m5_valid)), replace=False)

    for bar_idx in sample_idx:
        row = m5_valid.iloc[int(bar_idx)]
        bar_low = float(row["low"])
        bar_high = float(row["high"])
        if bar_high - bar_low < 0.2:
            continue

        # Put a level at 25% from low (level_a) and 75% from low (level_b)
        level_a = bar_low + 0.25 * (bar_high - bar_low)
        level_b = bar_low + 0.75 * (bar_high - bar_low)

        bar = Bar(
            open=float(row["open"]), high=bar_high, low=bar_low,
            close=float(row["close"]),
            ts_ms=int(row["timestamp"]),
        )

        pred_a = model.fraction_into_bar(bar, level_a, bar_idx=int(bar_idx))
        pred_b = model.fraction_into_bar(bar, level_b, bar_idx=int(bar_idx))
        if pred_a is None or pred_b is None:
            continue

        m1_indices = m1_groups[int(row["timestamp"])]
        m1_bars = m1.iloc[m1_indices].reset_index(drop=True)
        true_a = _m1_fraction_for_level(m1_bars, level_a)
        true_b = _m1_fraction_for_level(m1_bars, level_b)
        if true_a is None or true_b is None:
            continue

        n_tested += 1
        pred_a_first = pred_a < pred_b
        true_a_first = true_a < true_b
        if pred_a_first == true_a_first:
            n_agree += 1

    agreement = (n_agree / n_tested * 100.0) if n_tested > 0 else 0.0
    return BothHitResult(n_tested=n_tested, n_agree=n_agree, agreement_pct=agreement)


def main() -> None:
    print("G.5 — Brownian bridge intrabar path model validation")
    print("=" * 65)
    print()
    print("Loading M5 + M1 data...")
    m5 = pd.read_parquet(M5_PATH).sort_values("timestamp").reset_index(drop=True)
    m1 = pd.read_parquet(M1_PATH).sort_values("timestamp").reset_index(drop=True)
    print(f"  M5: {len(m5):,} bars")
    print(f"  M1: {len(m1):,} bars")

    print("Grouping M1 bars into M5 buckets...")
    m1_groups = _group_m1_by_m5(m1)
    print(f"  {len(m1_groups):,} M5 buckets with 5 M1 sub-bars each")

    rng = np.random.default_rng(42)

    print()
    print(f"Validation 1/2: level timing error (n={SAMPLE_SIZE} random bars)")
    timing = _level_timing_validation(m5, m1, m1_groups, rng)
    print(f"  n_tested = {timing.n_tested}")
    print(f"  mean error       = {timing.mean_error:.4f}")
    print(f"  median error     = {timing.median_error:.4f}")
    print(f"  p90 error        = {timing.p90_error:.4f}")
    print(f"  p99 error        = {timing.p99_error:.4f}")

    print()
    print(f"Validation 2/2: both-hit ordering agreement (n={SAMPLE_SIZE} random bars)")
    bh = _both_hit_validation(m5, m1, m1_groups, rng)
    print(f"  n_tested    = {bh.n_tested}")
    print(f"  n_agree     = {bh.n_agree}")
    print(f"  agreement % = {bh.agreement_pct:.2f}%")

    print()
    print("=" * 65)

    # Decision gate
    timing_pass = timing.mean_error < 0.25
    ordering_pass = bh.agreement_pct > 80.0

    print("Decision gates:")
    print(f"  Timing gate (mean error < 0.25):       {'✅ PASS' if timing_pass else '❌ FAIL'} ({timing.mean_error:.4f})")
    print(f"  Ordering gate (agreement > 80%):       {'✅ PASS' if ordering_pass else '❌ FAIL'} ({bh.agreement_pct:.2f}%)")
    print()
    if timing_pass and ordering_pass:
        print("✅ G.5 PASS — Brownian bridge model is sufficient for Tier 5 backtests")
        print("   No tick-data upgrade needed. Continue with existing BrownianBridgeModel.")
    else:
        print("❌ G.5 FAIL — Bridge model has bias. Upgrade to tick-based path model")
        print("   before trusting Tier 5 backtest results.")


if __name__ == "__main__":
    main()
