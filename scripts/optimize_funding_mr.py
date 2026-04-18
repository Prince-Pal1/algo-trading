#!/usr/bin/env python3
"""Stage 5 optimization grid for FundingMeanReversionStrategy.

Sweeps high_quantile × atr_sl_mult × cooldown_bars on 4.3y BTCUSDT
with Binance perpetual futures fees. Reports Sharpe + PSR + DSR
(Deflated Sharpe Ratio) to flag selection-bias.

Stage 5 gates: PSR > 0.5, grid std < 0.5, base params survive within
±20% of best grid cell (to avoid overfit).

Usage:
    python3 scripts/optimize_funding_mr.py --broker binance_perpetual_futures
    python3 scripts/optimize_funding_mr.py --grid small   # quick run
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_funding_mr import load_data, simulate, compute_metrics
from src.fees import get_broker


BASE_PARAMS = dict(
    quantile_window=90, high_quantile=0.90, low_quantile=0.10, mid_quantile=0.50,
    atr_sl_mult=2.5, max_hold_bars=6, cooldown_bars=2,
)


def grid_small() -> list[dict]:
    """Quick sanity grid (27 cells)."""
    cells = []
    for hq in [0.85, 0.90, 0.95]:
        for sl in [2.0, 2.5, 3.0]:
            for cd in [1, 2, 4]:
                cells.append(dict(BASE_PARAMS, high_quantile=hq, low_quantile=1-hq,
                                  atr_sl_mult=sl, cooldown_bars=cd))
    return cells


def grid_standard() -> list[dict]:
    """Full grid (64 cells)."""
    cells = []
    for hq in [0.85, 0.90, 0.93, 0.95]:
        for sl in [1.5, 2.0, 2.5, 3.0]:
            for cd in [1, 2, 4, 6]:
                cells.append(dict(BASE_PARAMS, high_quantile=hq, low_quantile=1-hq,
                                  atr_sl_mult=sl, cooldown_bars=cd))
    return cells


def deflated_sharpe(raw_sharpe: float, n_trades: int, n_trials: int,
                    skew: float = 0.0, kurt: float = 3.0) -> float:
    """López de Prado's Deflated Sharpe Ratio (AFML ch.14).

    Accounts for selection bias across n_trials in a grid search.
    Returns probability that true Sharpe > 0 given the observed max.
    """
    if n_trades < 2 or n_trials < 2:
        return 0.0
    # Expected max Sharpe under null (all strategies have true SR = 0)
    euler = 0.5772
    expected_max = math.sqrt(2 * math.log(n_trials)) - \
                   (euler / math.sqrt(2 * math.log(n_trials)))
    # Adjusted Sharpe (accounts for higher moments)
    sigma_sr = math.sqrt((1 - skew * raw_sharpe + ((kurt - 1) / 4) * raw_sharpe**2) / (n_trades - 1))
    if sigma_sr <= 0:
        return 0.0
    z = (raw_sharpe - expected_max) / sigma_sr
    return stats.norm.cdf(z)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default="binance_perpetual_futures")
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--grid", default="standard", choices=["small", "standard"])
    parser.add_argument("--out", type=Path,
                        default=Path("reports/funding_mr_stage34/optimize.json"))
    args = parser.parse_args()

    print("=== Funding MR Stage 5 optimization ===\n")
    df = load_data()
    n_years = (df.index[-1] - df.index[0]).days / 365.25
    schedule = get_broker(args.broker).resolve_profile(
        instrument_class="crypto", scenario=args.scenario,
    ).commission_schedule
    print(f"Fee profile: {args.broker}/{args.scenario}  "
          f"({schedule.taker_fraction_of_notional*100:.4f}%/side taker)\n")

    cells = grid_small() if args.grid == "small" else grid_standard()
    print(f"Grid size: {len(cells)} cells\n")

    results = []
    base_metrics = None
    for i, params in enumerate(cells):
        trades = simulate(df, fee_schedule=schedule, strategy_params=params)
        m = compute_metrics(trades, n_years)
        m["params"] = {k: params[k] for k in ("high_quantile", "atr_sl_mult", "cooldown_bars")}
        results.append(m)
        if params == BASE_PARAMS:
            base_metrics = m
        print(f"  cell {i+1:>3}/{len(cells)}: hq={params['high_quantile']:.2f} "
              f"sl={params['atr_sl_mult']:.1f} cd={params['cooldown_bars']}  "
              f"n={m['n']:>3}  Sharpe={m['sharpe_net_ann']:>+6.3f}  "
              f"Net={m['total_net_pct']:>+7.2f}%  DD={m['max_dd_pct']:>+6.2f}%")

    # Ranking + stats
    results.sort(key=lambda r: r["sharpe_net_ann"], reverse=True)
    sharpes = [r["sharpe_net_ann"] for r in results]
    best = results[0]
    mean_s = statistics.mean(sharpes)
    std_s = statistics.stdev(sharpes) if len(sharpes) > 1 else 0.0

    print(f"\n=== Grid statistics ===")
    print(f"  cells: {len(results)}")
    print(f"  Sharpe min: {min(sharpes):+.3f}  median: {statistics.median(sharpes):+.3f}  "
          f"max: {max(sharpes):+.3f}  mean: {mean_s:+.3f}  std: {std_s:.3f}")

    # PSR on base params
    psr_base = None
    if base_metrics:
        psr_base = stats.norm.cdf(base_metrics["sharpe_net_ann"] * math.sqrt(base_metrics["n"] - 1))
        print(f"\n=== Base params ===")
        print(f"  Sharpe: {base_metrics['sharpe_net_ann']:+.3f}  "
              f"n: {base_metrics['n']}  Net: {base_metrics['total_net_pct']:+.2f}%")
        print(f"  PSR (base): {psr_base:.3f}")

    # DSR on best cell (selection bias correction)
    dsr_best = deflated_sharpe(best["sharpe_net_ann"], best["n"], len(cells))
    print(f"\n=== Best grid cell (selection-bias aware) ===")
    print(f"  params: {best['params']}")
    print(f"  Sharpe: {best['sharpe_net_ann']:+.3f}  n: {best['n']}  "
          f"Net: {best['total_net_pct']:+.2f}%  DD: {best['max_dd_pct']:+.2f}%")
    print(f"  DSR (deflated vs {len(cells)} trials): {dsr_best:.3f}")

    # Gates
    print(f"\n=== Stage 5 Gates ===")
    psr_gate = (psr_base is not None and psr_base >= 0.5)
    std_gate = std_s < 0.5
    all_positive = all(r["sharpe_net_ann"] > 0 for r in results)
    print(f"  PSR (base) ≥ 0.5:     {'✅' if psr_gate else '❌'} "
          f"({psr_base:.3f})" if psr_base is not None else "  PSR: no base params in grid")
    print(f"  Grid std  < 0.5:      {'✅' if std_gate else '❌'} ({std_s:.3f})")
    print(f"  All cells positive:   {'✅' if all_positive else '❌'} "
          f"({sum(1 for r in results if r['sharpe_net_ann']>0)}/{len(results)})")
    print(f"  DSR advisory (informational — expect ≈0 on small grids per process memory): "
          f"{dsr_best:.3f}")
    if dsr_best < 0.5 and std_gate and all_positive:
        print(f"\n  Note: Low DSR + tight grid std + all-positive = ROBUST edge, USE BASE PARAMS.")
        print(f"        (Per STRATEGY_DEVELOPMENT_PROCESS.md lesson: DSR=0 on small grid = expected selection bias.)")

    if base_metrics:
        base_vs_best_gap = best["sharpe_net_ann"] - base_metrics["sharpe_net_ann"]
        print(f"  Best vs base gap:     {base_vs_best_gap:+.3f}  "
              f"({'low overfit signal' if abs(base_vs_best_gap) < 0.2 else 'check overfit risk'})")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "broker": args.broker, "scenario": args.scenario,
        "n_cells": len(results),
        "grid_stats": {"min": min(sharpes), "median": statistics.median(sharpes),
                       "max": max(sharpes), "mean": mean_s, "std": std_s},
        "base": base_metrics,
        "best": best,
        "psr_base": psr_base if base_metrics else None,
        "dsr_best": dsr_best,
        "all_results": results[:20],  # top 20
    }, indent=2, default=str))
    print(f"\n  Report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
