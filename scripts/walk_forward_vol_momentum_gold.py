#!/usr/bin/env python3
"""Walk-forward retune of vol_momentum_gold on 2yr XAUUSD 1h.

Mirrors scripts/walk_forward_donchian_gold.py but tunes vol_momentum's
parameter grid (momentum_window × vol_lookback × vol_target ×
sl_atr_mult × session_filter) per fold. Reports mean OOS metrics
across 6 folds for an honest G.3 Day 1 baseline.

Gate: mean OOS Calmar >= 0.3 OR vol_momentum_gold should NOT ship
to the combined institutional book.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import pandas as pd

from src.backtest.book import SUB_BOOK_INSTITUTIONAL
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.momentum.vol_momentum_gold import VolMomentumGoldStrategy


DATA_PATH = "data/historical/XAUUSD_1h.parquet"
INDICATORS = ["atr_14", "atr_20", "adx_14"]
TOTAL_CAPITAL = 10_000.0
PERIODS_PER_YEAR = 6240.0

BARS_PER_DAY = 24
TRAIN_DAYS = 90
TEST_DAYS = 30


@dataclass
class FoldResult:
    fold: int
    best_params: dict
    out_sample_return: float
    out_sample_dd: float
    out_sample_calmar: float
    out_sample_sharpe: float
    out_sample_trades: int


def _sharpe_from_equity(curve: list[float], periods_per_year: float) -> float:
    if len(curve) < 2:
        return 0.0
    rets = []
    for i in range(1, len(curve)):
        prev = curve[i - 1]
        if prev <= 0:
            continue
        rets.append((curve[i] - prev) / prev)
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def _run_backtest(
    df: pd.DataFrame,
    *,
    momentum_window: int,
    vol_lookback: int,
    vol_target: float,
    sl_atr_mult: float,
    long_only: bool,
    session_filter: bool,
    run_id: str,
) -> dict:
    strategy = VolMomentumGoldStrategy(
        momentum_window=momentum_window,
        vol_lookback=vol_lookback,
        vol_target=vol_target,
        sl_atr_mult=sl_atr_mult,
        long_only=long_only,
        session_filter=session_filter,
    )
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=TOTAL_CAPITAL,
        initial_aggressive_cash=0.0,
        run_id=run_id,
    )
    result = engine.run(
        strategy=strategy,
        data=df,
        symbol="XAUUSD",
        timeframe="1h",
        leverage=10.0,
        sub_book=SUB_BOOK_INSTITUTIONAL,
        indicators=INDICATORS,
    )
    m = result.metrics
    return {
        "return_pct": m["total_return_pct"],
        "max_dd_pct": m["max_dd_pct"],
        "trades": m["total_trades"],
        "equity_curve": result.equity_curve_total,
    }


def _tune_on_fold(train_df: pd.DataFrame) -> dict:
    """Small grid search on the training window. Returns the best config."""
    best = None
    best_calmar = -999.0
    # Small grid to keep walk-forward quick — covers the tuned neighborhood
    for mw, vl, vt, sl in itertools.product(
        [168, 240],
        [168, 240],
        [0.15, 0.20],
        [2.5, 3.0],
    ):
        r = _run_backtest(
            train_df,
            momentum_window=mw, vol_lookback=vl, vol_target=vt,
            sl_atr_mult=sl, long_only=True, session_filter=True,
            run_id=f"wf_vmg_train_{mw}_{vl}_{vt}_{sl}",
        )
        max_dd = max(r["max_dd_pct"], 0.01)
        calmar = r["return_pct"] / max_dd
        if calmar > best_calmar and r["trades"] >= 5:
            best_calmar = calmar
            best = {
                "momentum_window": mw,
                "vol_lookback": vl,
                "vol_target": vt,
                "sl_atr_mult": sl,
                "train_calmar": calmar,
            }
    return best or {
        "momentum_window": 240,
        "vol_lookback": 240,
        "vol_target": 0.20,
        "sl_atr_mult": 3.0,
        "train_calmar": 0.0,
    }


def main() -> None:
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} bars of {DATA_PATH}")

    bars_train = TRAIN_DAYS * BARS_PER_DAY
    bars_test = TEST_DAYS * BARS_PER_DAY
    warmup = 300  # vol_momentum needs ~240 bars of history for mw=240
    n_folds = 6
    fold_results: list[FoldResult] = []

    start = warmup
    for fold in range(n_folds):
        train_start = start
        train_end = train_start + bars_train
        test_start = train_end
        test_end = test_start + bars_test
        if test_end > len(df):
            print(f"Stopping at fold {fold + 1}: not enough data ({test_end} > {len(df)})")
            break

        print(f"\nFold {fold + 1}: train {train_start}..{train_end} ({TRAIN_DAYS}d), test {test_start}..{test_end} ({TEST_DAYS}d)")
        train_df = df.iloc[train_start:train_end].reset_index(drop=True)
        best = _tune_on_fold(train_df)
        print(f"  best params: mw={best['momentum_window']} vl={best['vol_lookback']} vt={best['vol_target']} sl={best['sl_atr_mult']} train_calmar={best['train_calmar']:.3f}")

        test_df = df.iloc[test_start - warmup:test_end].reset_index(drop=True)
        test_r = _run_backtest(
            test_df,
            momentum_window=best["momentum_window"],
            vol_lookback=best["vol_lookback"],
            vol_target=best["vol_target"],
            sl_atr_mult=best["sl_atr_mult"],
            long_only=True,
            session_filter=True,
            run_id=f"wf_vmg_test_{fold}",
        )
        max_dd = max(test_r["max_dd_pct"], 0.01)
        annualized = test_r["return_pct"] * (PERIODS_PER_YEAR / len(test_df))
        calmar = annualized / max_dd
        sharpe = _sharpe_from_equity(test_r["equity_curve"], PERIODS_PER_YEAR)

        fold_results.append(FoldResult(
            fold=fold + 1,
            best_params=best,
            out_sample_return=test_r["return_pct"],
            out_sample_dd=test_r["max_dd_pct"],
            out_sample_calmar=calmar,
            out_sample_sharpe=sharpe,
            out_sample_trades=test_r["trades"],
        ))
        print(f"  out-of-sample: ret={test_r['return_pct']:.2f}% dd={test_r['max_dd_pct']:.2f}% calmar={calmar:.3f} sharpe={sharpe:.3f} trades={test_r['trades']}")

        start += bars_test

    if not fold_results:
        print("\nNo folds completed.")
        return

    n = len(fold_results)
    mean_ret = sum(r.out_sample_return for r in fold_results) / n
    mean_dd = sum(r.out_sample_dd for r in fold_results) / n
    mean_calmar = sum(r.out_sample_calmar for r in fold_results) / n
    mean_sharpe = sum(r.out_sample_sharpe for r in fold_results) / n
    std_calmar = math.sqrt(
        sum((r.out_sample_calmar - mean_calmar) ** 2 for r in fold_results) / max(1, n - 1)
    )
    std_sharpe = math.sqrt(
        sum((r.out_sample_sharpe - mean_sharpe) ** 2 for r in fold_results) / max(1, n - 1)
    )
    total_trades = sum(r.out_sample_trades for r in fold_results)

    print()
    print("=" * 60)
    print(f"vol_momentum_gold walk-forward summary ({n} folds, 30d each):")
    print(f"  mean OOS return: {mean_ret:+.2f}% (sum {sum(r.out_sample_return for r in fold_results):+.2f}%)")
    print(f"  mean OOS max_dd: {mean_dd:.2f}%")
    print(f"  mean OOS Calmar: {mean_calmar:+.3f} ± {std_calmar:.3f}")
    print(f"  mean OOS Sharpe: {mean_sharpe:+.3f} ± {std_sharpe:.3f}")
    print(f"  total OOS trades: {total_trades}")
    print()
    gate = "✅ PASS" if mean_calmar >= 0.3 else "❌ FAIL"
    print(f"G.3 readiness gate (mean OOS Calmar >= 0.3): {gate}")
    if mean_calmar < 0.3:
        print("  → vol_momentum_gold is NOT safe for the combined institutional book baseline")
        print("  → either drop it or re-tune with a broader grid")


if __name__ == "__main__":
    main()
