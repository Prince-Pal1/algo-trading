#!/usr/bin/env python3
"""Walk-forward retune of donchian_gold on 2yr XAUUSD 1h.

6 folds: 3-month train window (grid search) → 1-month test (hold-out).
Reports mean and stdev of out-of-sample Calmar/Sharpe/DD across folds.

Gate: out-of-sample Calmar >= 0.3 or donchian_gold should NOT ship to
the G.3 paper clock (single-window Calmar 1.675 was in-sample tuned).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import pandas as pd

from src.backtest.book import SUB_BOOK_INSTITUTIONAL
from src.backtest.fee_profiles import make_fee_model
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy


DATA_PATH = "data/historical/XAUUSD_1h.parquet"
INDICATORS = [
    "donchian_20", "donchian_55", "donchian_120",
    "atr_14", "atr_20", "adx_14",
]
TOTAL_CAPITAL = 10_000.0
PERIODS_PER_YEAR = 6240.0

# Walk-forward schedule: 3 months train, 1 month test, 6 folds total.
BARS_PER_DAY = 24  # 1h bars, 24 per day
TRAIN_DAYS = 90
TEST_DAYS = 30


@dataclass
class FoldResult:
    fold: int
    train_start_idx: int
    train_end_idx: int
    test_start_idx: int
    test_end_idx: int
    best_params: dict
    in_sample_return: float
    in_sample_dd: float
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
    sl_atr_mult: float,
    adx_threshold: float | None,
    min_channels: int,
    risk_pct: float,
    run_id: str,
) -> dict:
    strategy = DonchianGoldStrategy(
        session_filter=True,
        adx_trend_threshold=adx_threshold,
        sl_atr_mult=sl_atr_mult,
        min_channels=min_channels,
        max_risk_per_trade=risk_pct,
    )
    # Mandatory: explicit fee profile per CLAUDE.md rule (task #105/107).
    # Never use the default ICMarketsMetalFeeModel() constructor — that
    # was the source of the 90× slippage bug. The cTrader profile here
    # uses research-calibrated numbers (commit 6ae48c8 + task #106).
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=TOTAL_CAPITAL,
        initial_aggressive_cash=0.0,
        fee_model=make_fee_model("ic_markets_ctrader_xauusd_normal"),
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
    for sl, adx, mc, risk in itertools.product(
        [2.5, 3.0, 3.5],
        [20.0, 25.0, 30.0],
        [2],
        [0.01, 0.02],
    ):
        r = _run_backtest(
            train_df,
            sl_atr_mult=sl, adx_threshold=adx,
            min_channels=mc, risk_pct=risk,
            run_id=f"wf_train_{sl}_{adx}_{mc}_{risk}",
        )
        max_dd = max(r["max_dd_pct"], 0.01)
        calmar = r["return_pct"] / max_dd
        if calmar > best_calmar and r["trades"] >= 10:
            best_calmar = calmar
            best = {
                "sl_atr_mult": sl,
                "adx_threshold": adx,
                "min_channels": mc,
                "risk_pct": risk,
                "train_calmar": calmar,
            }
    return best or {
        "sl_atr_mult": 3.0,
        "adx_threshold": 25.0,
        "min_channels": 2,
        "risk_pct": 0.02,
        "train_calmar": 0.0,
    }


def main() -> None:
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} bars of {DATA_PATH}")

    bars_train = TRAIN_DAYS * BARS_PER_DAY
    bars_test = TEST_DAYS * BARS_PER_DAY
    fold_size = bars_train + bars_test

    # 6 rolling folds starting from the earliest index where we have
    # a full fold + at least 120 bars of donchian warmup.
    warmup = 250
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
        print(f"  best params: sl={best['sl_atr_mult']} adx={best['adx_threshold']} risk={best['risk_pct']} train_calmar={best['train_calmar']:.3f}")

        # Apply to the out-of-sample test window
        # Include warmup bars from earlier so indicators are valid
        test_df = df.iloc[test_start - warmup:test_end].reset_index(drop=True)
        test_r = _run_backtest(
            test_df,
            sl_atr_mult=best["sl_atr_mult"],
            adx_threshold=best["adx_threshold"],
            min_channels=best["min_channels"],
            risk_pct=best["risk_pct"],
            run_id=f"wf_test_{fold}",
        )
        max_dd = max(test_r["max_dd_pct"], 0.01)
        annualized = test_r["return_pct"] * (PERIODS_PER_YEAR / len(test_df))
        calmar = annualized / max_dd
        sharpe = _sharpe_from_equity(test_r["equity_curve"], PERIODS_PER_YEAR)

        fold_results.append(FoldResult(
            fold=fold + 1,
            train_start_idx=train_start,
            train_end_idx=train_end,
            test_start_idx=test_start,
            test_end_idx=test_end,
            best_params=best,
            in_sample_return=0.0,
            in_sample_dd=0.0,
            out_sample_return=test_r["return_pct"],
            out_sample_dd=test_r["max_dd_pct"],
            out_sample_calmar=calmar,
            out_sample_sharpe=sharpe,
            out_sample_trades=test_r["trades"],
        ))
        print(f"  out-of-sample: ret={test_r['return_pct']:.2f}% dd={test_r['max_dd_pct']:.2f}% calmar={calmar:.3f} sharpe={sharpe:.3f} trades={test_r['trades']}")

        # Next fold walks forward by 1 test window (rolling)
        start += bars_test

    # Summary
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
    print(f"Walk-forward summary ({n} folds, 30d each):")
    print(f"  mean OOS return: {mean_ret:+.2f}% (sum {sum(r.out_sample_return for r in fold_results):+.2f}%)")
    print(f"  mean OOS max_dd: {mean_dd:.2f}%")
    print(f"  mean OOS Calmar: {mean_calmar:+.3f} ± {std_calmar:.3f}")
    print(f"  mean OOS Sharpe: {mean_sharpe:+.3f} ± {std_sharpe:.3f}")
    print(f"  total OOS trades: {total_trades}")
    print()
    gate = "✅ PASS" if mean_calmar >= 0.3 else "❌ FAIL"
    print(f"G.3 readiness gate (mean OOS Calmar >= 0.3): {gate}")
    if mean_calmar < 0.3:
        print("  → donchian_gold is NOT safe for the G.3 paper clock baseline")
        print("  → needs deeper tuning OR should be paired with a second strategy")


if __name__ == "__main__":
    main()
