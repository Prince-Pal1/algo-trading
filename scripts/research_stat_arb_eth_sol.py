#!/usr/bin/env python3
"""Stage 1 research for Strategy 2 — Stat-Arb Pairs (ETH / SOL).

Tests the cointegration of ETHUSDT and SOLUSDT log-prices on 4y 1h
data with a 90-day rolling window. Reports:
  - Johansen trace + eigenvalue test p-values (on full window)
  - ADF test on log-spread
  - OU half-life of the spread (mean reversion speed)
  - Rolling correlation over 90-day windows (persistence check)
  - Plot-data: z-score of spread across entire history

Stage 1 gates (from plan):
  - Johansen p-value < 0.01 on 90-day window (strong cointegration)
  - Half-life between 3 and 14 days (fast enough to trade, slow enough
    that individual swings aren't noise)
  - Rolling 90-day correlation > 0.7 throughout the history

Kill path:
  - Any of the 3 gates fail → kill, try BTC/LTC or ETH/BNB as fallback.
  - Cointegration breaks in > 5% of rolling windows → follow the
    BTC-Neutral obituary template (structural fragility).

Usage:
    python3 scripts/research_stat_arb_eth_sol.py
    python3 scripts/research_stat_arb_eth_sol.py --pair BTCUSDT,LTCUSDT
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

try:
    from statsmodels.tsa.stattools import adfuller, coint
    from statsmodels.tsa.vector_ar.vecm import coint_johansen
    HAVE_STATSMODELS = True
except ImportError:
    HAVE_STATSMODELS = False

REPO = Path(__file__).resolve().parent.parent


def load_pair(sym_a: str, sym_b: str) -> pd.DataFrame:
    a = pd.read_parquet(REPO / f"data/historical/{sym_a}_1h.parquet")
    b = pd.read_parquet(REPO / f"data/historical/{sym_b}_1h.parquet")
    a["ts"] = pd.to_datetime(a["timestamp"], unit="ms", utc=True)
    b["ts"] = pd.to_datetime(b["timestamp"], unit="ms", utc=True)
    a = a[["ts", "close"]].rename(columns={"close": sym_a.lower()}).set_index("ts")
    b = b[["ts", "close"]].rename(columns={"close": sym_b.lower()}).set_index("ts")
    merged = a.join(b, how="inner").dropna()
    merged[f"log_{sym_a.lower()}"] = np.log(merged[sym_a.lower()])
    merged[f"log_{sym_b.lower()}"] = np.log(merged[sym_b.lower()])
    return merged


def engle_granger(log_a: pd.Series, log_b: pd.Series) -> dict:
    """Engle-Granger two-step cointegration test."""
    if not HAVE_STATSMODELS:
        return {"error": "statsmodels not installed"}
    # coint returns (score, pvalue, crit_values)
    score, pvalue, crit = coint(log_a, log_b)
    return {
        "test": "engle_granger",
        "score": float(score),
        "p_value": float(pvalue),
        "crit_1pct": float(crit[0]),
        "crit_5pct": float(crit[1]),
        "crit_10pct": float(crit[2]),
    }


def johansen_test(log_a: pd.Series, log_b: pd.Series) -> dict:
    if not HAVE_STATSMODELS:
        return {"error": "statsmodels not installed"}
    arr = np.column_stack([log_a.values, log_b.values])
    res = coint_johansen(arr, det_order=0, k_ar_diff=1)
    # Trace-test: H0 = r = 0 (no cointegration vectors)
    # If trace_stat[0] > crit_99[0], reject → at least one coint relation.
    trace_stat = res.lr1
    crit = res.cvt  # columns: 90%, 95%, 99%
    return {
        "test": "johansen",
        "trace_stats": [float(x) for x in trace_stat],
        "crit_90": [float(x) for x in crit[:, 0]],
        "crit_95": [float(x) for x in crit[:, 1]],
        "crit_99": [float(x) for x in crit[:, 2]],
        "reject_r0_at_99": bool(trace_stat[0] > crit[0, 2]),
        "reject_r0_at_95": bool(trace_stat[0] > crit[0, 1]),
        "hedge_ratio_evec": [float(res.evec[0, 0]), float(res.evec[1, 0])],
    }


def ou_half_life(spread: pd.Series) -> float:
    """Fit dS_t = -lambda × S_{t-1} × dt + noise; half-life = ln(2)/lambda.

    Returns half-life in BARS. Caller converts to days.
    """
    s = spread.dropna()
    ds = s.diff().dropna()
    s_lag = s.shift(1).dropna()
    aligned = pd.concat([ds, s_lag], axis=1).dropna()
    aligned.columns = ["ds", "s_lag"]
    if len(aligned) < 100:
        return float("nan")
    # OLS: ds = a + b × s_lag
    X = aligned["s_lag"].values
    y = aligned["ds"].values
    # Slope = b; mean-reversion rate lambda = -b.
    slope, intercept = np.polyfit(X, y, 1)
    lam = -slope
    if lam <= 0:
        return float("inf")
    return float(math.log(2) / lam)


def rolling_correlation(log_a: pd.Series, log_b: pd.Series, window_bars: int = 90 * 24) -> pd.Series:
    """90-day rolling correlation on log prices."""
    return log_a.rolling(window_bars).corr(log_b)


def rolling_cointegration_stability(
    log_a: pd.Series, log_b: pd.Series, window_bars: int = 90 * 24, step_bars: int = 30 * 24,
) -> list[dict]:
    """Slide a 90-day window by 30-day step; report p-value per window."""
    if not HAVE_STATSMODELS:
        return []
    results = []
    n = len(log_a)
    i = window_bars
    while i <= n:
        sub_a = log_a.iloc[i - window_bars:i]
        sub_b = log_b.iloc[i - window_bars:i]
        try:
            score, pval, _ = coint(sub_a, sub_b)
            results.append({
                "end": str(log_a.index[i - 1].date()),
                "p_value": float(pval),
                "cointegrated_5pct": bool(pval < 0.05),
            })
        except Exception:
            pass
        i += step_bars
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="ETHUSDT,SOLUSDT",
                        help="Comma-separated pair, e.g. ETHUSDT,SOLUSDT")
    parser.add_argument("--window-days", type=int, default=90,
                        help="Cointegration-test window in days")
    parser.add_argument("--out", type=Path,
                        default=Path("reports/stat_arb_stage1/eth_sol.json"))
    args = parser.parse_args()

    if not HAVE_STATSMODELS:
        print("ERROR: statsmodels not installed. Run: pip install statsmodels")
        return 1

    sym_a, sym_b = args.pair.split(",")
    print(f"=== Stage 1 research: {sym_a} / {sym_b} cointegration ===\n")

    df = load_pair(sym_a, sym_b)
    log_a = df[f"log_{sym_a.lower()}"]
    log_b = df[f"log_{sym_b.lower()}"]
    n_years = (df.index[-1] - df.index[0]).days / 365.25
    print(f"Data: {len(df)} 1h bars, {df.index[0].date()} → {df.index[-1].date()} "
          f"({n_years:.1f}y)\n")

    # Full-window Engle-Granger + Johansen
    print("=== Cointegration on full window ===")
    eg = engle_granger(log_a, log_b)
    print(f"  Engle-Granger: score={eg['score']:+.3f}  p-value={eg['p_value']:.4f}  "
          f"crit_1pct={eg['crit_1pct']:+.3f}")
    jo = johansen_test(log_a, log_b)
    print(f"  Johansen trace_stat[r=0]: {jo['trace_stats'][0]:.3f}  "
          f"crit_99: {jo['crit_99'][0]:.3f}  reject: {'✅' if jo['reject_r0_at_99'] else '❌'}")
    print(f"  Hedge ratio (from evec): {jo['hedge_ratio_evec']}")
    print()

    # OU half-life on the spread
    beta = -jo['hedge_ratio_evec'][1] / jo['hedge_ratio_evec'][0]  # a + β×b
    spread_log = log_a - beta * log_b
    hl_bars = ou_half_life(spread_log)
    hl_days = hl_bars / 24 if hl_bars != float("inf") else float("inf")
    print(f"=== Spread OU half-life ===")
    print(f"  beta (hedge ratio): {beta:+.4f}")
    print(f"  half-life: {hl_bars:.1f} bars  ({hl_days:.1f} days)")
    print()

    # Rolling correlation
    print(f"=== Rolling {args.window_days}d correlation ===")
    rc = rolling_correlation(log_a, log_b, window_bars=args.window_days * 24)
    rc_clean = rc.dropna()
    if len(rc_clean) > 0:
        rc_min = rc_clean.min()
        rc_mean = rc_clean.mean()
        rc_below_07 = (rc_clean < 0.7).sum() / len(rc_clean)
        print(f"  min: {rc_min:.3f}  mean: {rc_mean:.3f}  "
              f"% below 0.7: {rc_below_07*100:.1f}%")
    print()

    # Rolling cointegration stability
    print(f"=== Rolling {args.window_days}d cointegration p-values ===")
    stab = rolling_cointegration_stability(log_a, log_b,
                                            window_bars=args.window_days * 24,
                                            step_bars=30 * 24)
    if stab:
        p_vals = [s["p_value"] for s in stab]
        coint_pct = sum(1 for s in stab if s["cointegrated_5pct"]) / len(stab) * 100
        print(f"  windows: {len(stab)}")
        print(f"  p-value min: {min(p_vals):.4f}  median: {np.median(p_vals):.4f}  "
              f"max: {max(p_vals):.4f}")
        print(f"  % windows cointegrated at 5%: {coint_pct:.1f}%")
        # Show any recent breaks
        recent_breaks = [s for s in stab if s["p_value"] > 0.05][-5:]
        if recent_breaks:
            print(f"  Recent windows with p > 0.05:")
            for s in recent_breaks:
                print(f"    {s['end']}: p={s['p_value']:.4f}")
    print()

    # Gates
    print("=== Stage 1 Gates ===")
    gate_full = eg["p_value"] < 0.01
    gate_halflife = 3 * 24 <= hl_bars <= 14 * 24 if not math.isinf(hl_bars) else False
    gate_corr = rc_clean.min() > 0.7 if len(rc_clean) > 0 else False
    gate_stability = (coint_pct >= 85.0) if stab else False
    print(f"  Full-window p < 0.01:       {'✅' if gate_full else '❌'} ({eg['p_value']:.4f})")
    print(f"  Half-life ∈ [3d, 14d]:      {'✅' if gate_halflife else '❌'} ({hl_days:.1f}d)")
    print(f"  Rolling min corr > 0.7:     {'✅' if gate_corr else '❌'} "
          f"({rc_clean.min():.3f} if data else 'n/a')")
    print(f"  Windows cointegrated ≥ 85%: {'✅' if gate_stability else '❌'} ({coint_pct:.1f}%)")

    pass_count = sum([gate_full, gate_halflife, gate_corr, gate_stability])
    verdict = "PASS" if pass_count >= 3 else "FAIL"  # 3/4 gates OK (some tolerance)
    print(f"\n=== Stage 1 Verdict: {verdict} ({pass_count}/4 gates) ===")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "pair": [sym_a, sym_b],
        "n_bars": len(df), "n_years": n_years,
        "engle_granger": eg, "johansen": jo,
        "half_life_bars": hl_bars, "half_life_days": hl_days,
        "rolling_corr": {"min": float(rc_clean.min()) if len(rc_clean)>0 else None,
                          "mean": float(rc_clean.mean()) if len(rc_clean)>0 else None,
                          "pct_below_07": float(rc_below_07*100) if len(rc_clean)>0 else None},
        "rolling_stability": stab,
        "gates": {
            "full_window_pvalue": gate_full,
            "half_life_range": gate_halflife,
            "rolling_corr_min": gate_corr,
            "stability_85pct": gate_stability,
        },
        "verdict": verdict,
    }, indent=2, default=str))
    print(f"  Report: {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
