#!/usr/bin/env python3
"""Walk-forward OOS validation of SwiftAlmaStrategy — honest baseline #3 (task #111).

Runs 7 non-overlapping 90-day OOS folds (~1.7yr total) over XAUUSD 5m→15m
data with fixed Pine params. No retuning — Pine params are rigid by design
(hand-tuned by the Pine author on TradingView; tuning here would deviate
from Pine semantics and bias the baseline). This is an OOS robustness check,
not a tuning pass.

Matrix #109 best cell: 15m × MT4 × 1y → +25.57% / 11.96% DD / Calmar 2.14
(single-window, full most-recent year). The WF test asks: does this hold up
across non-overlapping 3-month OOS slices, or was the 1y window a lucky
regime?

Fee profile: ic_markets_mt4_xauusd_normal (matrix best).
Leverage:    10x nominal (P&L is leverage-invariant for SWIFT per task #110).

Gate:
  - Mean OOS Calmar >= 0.5  → SWIFT eligible for institutional sub-book
  - Mean OOS Calmar <  0.5  → SWIFT stays research-only

Pattern mirrors scripts/walk_forward_donchian_gold.py (task #86) and
scripts/walk_forward_vol_momentum_gold.py (task #90/108) for consistency
with prior honest baselines. Runs via `PYTHONPATH=. python3 scripts/walk_forward_swift_alma.py`.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.book import SUB_BOOK_INSTITUTIONAL  # noqa: E402
from src.backtest.fee_profiles import make_fee_model  # noqa: E402
from src.backtest.leveraged_engine import LeveragedBacktestEngine  # noqa: E402
from src.backtest.path import get_or_build_m1_path_model  # noqa: E402
from src.strategies.trend_following.swift_alma import SwiftAlmaStrategy  # noqa: E402


DATA_M5 = REPO_ROOT / "data" / "historical" / "XAUUSD_5m.parquet"
DATA_M1 = REPO_ROOT / "data" / "historical" / "XAUUSD_1m.parquet"

TOTAL_CAPITAL = 10_000.0
FEE_PROFILE = "ic_markets_mt4_xauusd_normal"
TIMEFRAME = "15m"
LEVERAGE = 10.0

# 7 non-overlapping 90-day (≈3mo) OOS folds = 630 days ≈ 1.7yr.
# Folds are sliced by CALENDAR time (dt), not bar count — gold closes on
# weekends, so ~65 M15 bars/day (not 96). Each fold gets ~5,850 bars ≈
# 75-90 trades at SWIFT's frequency.
FOLD_DAYS = 90
N_FOLDS = 7

# 15m bars per calendar year (calendar-time-based Sharpe for consistency
# across weekend gaps). 120 trading hours/week × 4 bars/hour × 52 weeks
# ≈ 24,960 bars/yr. Using that as the annualization denominator.
PERIODS_PER_YEAR = 24_960.0

# SWIFT Pine defaults — verified from xlsx Properties sheet in task #109.
# Matches scripts/swift_full_matrix.py::SWIFT_PARAMS exactly.
SWIFT_PARAMS = dict(
    alma_length=2,
    alma_offset=0.85,
    alma_sigma=5.0,
    alt_tf_multiplier=8,
    sl_pct=0.005,
    tp1_pct=0.010, tp1_qty=0.50,
    tp2_pct=0.015, tp2_qty=0.30,
    tp3_pct=0.020, tp3_qty=0.20,
    max_risk_per_trade=0.005,
)

# Warmup: alt_tf_multiplier=8 × alma_length=2 → 16 alt bars = 128 chart bars.
# 200 is comfortable headroom.
WARMUP_BARS = 200

GATE_CALMAR = 0.5


@dataclass
class FoldResult:
    fold: int
    start_ts: str
    end_ts: str
    trades: int
    return_pct: float
    max_dd_pct: float
    calmar: float
    sharpe: float
    win_rate: float
    profit_factor: float


def _resample_5m_to_15m(df_m5: pd.DataFrame) -> pd.DataFrame:
    """Resample M5 OHLCV to M15. Mirrors swift_full_matrix._resample_5m_to_15m."""
    df = df_m5.copy()
    df["dt_idx"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt_idx")
    rs = df.resample("15min", origin="epoch").agg({
        "timestamp": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    rs = rs.reset_index(drop=True)
    rs["dt"] = pd.to_datetime(rs["timestamp"], unit="ms", utc=True)
    return rs


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


def _run_fold(fold_df: pd.DataFrame, m1pm, run_id: str) -> dict:
    """Run SWIFT on one OOS fold with fixed Pine params."""
    strategy = SwiftAlmaStrategy(timeframe=TIMEFRAME, **SWIFT_PARAMS)
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=TOTAL_CAPITAL,
        initial_aggressive_cash=0.0,
        fee_model=make_fee_model(FEE_PROFILE),
        path_model=m1pm,
        run_id=run_id,
    )
    result = engine.run(
        strategy, fold_df,
        symbol="XAUUSD",
        timeframe=TIMEFRAME,
        leverage=LEVERAGE,
        sub_book=SUB_BOOK_INSTITUTIONAL,
    )
    final_eq = result.metrics["final_institutional_equity"]
    return_pct = (final_eq - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100.0
    max_dd = result.metrics["max_dd_pct"]
    trades = result.trades
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "return_pct": return_pct, "max_dd_pct": max_dd,
            "win_rate": 0.0, "profit_factor": 0.0, "sharpe": 0.0,
        }
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n * 100.0
    total_win = sum(wins)
    total_loss = -sum(losses)
    pf = (total_win / total_loss) if total_loss > 0 else (float("inf") if total_win > 0 else 0.0)
    sharpe = _sharpe_from_equity(result.equity_curve_total, PERIODS_PER_YEAR)
    return {
        "trades": n,
        "return_pct": return_pct,
        "max_dd_pct": max_dd,
        "win_rate": win_rate,
        "profit_factor": pf,
        "sharpe": sharpe,
    }


def main() -> int:
    print("Loading XAUUSD M5 and resampling to M15...")
    df_m5 = pd.read_parquet(DATA_M5)
    df = _resample_5m_to_15m(df_m5)
    print(f"  Loaded {len(df_m5):,} M5 bars → {len(df):,} M15 bars")
    print(f"  Range: {df['dt'].iloc[0]} → {df['dt'].iloc[-1]}")

    print("Building M1PathModel cache...")
    m1pm = get_or_build_m1_path_model(str(DATA_M1), sub_bar_count=5)
    print(f"  ✓ M1PathModel ready")

    # Fold boundaries are dt-based (calendar time). Walk backward from the
    # END of the data in N_FOLDS × FOLD_DAYS chunks so the final fold hits
    # the most recent data (matching matrix #109's best-cell window).
    last_dt = df["dt"].iloc[-1]
    total_days = N_FOLDS * FOLD_DAYS
    wf_start_dt = last_dt - pd.Timedelta(days=total_days)
    first_dt = df["dt"].iloc[0]
    if wf_start_dt < first_dt + pd.Timedelta(days=30):  # reserve 30d warmup
        print(f"ERROR: Not enough calendar data — need {total_days}d + 30d warmup, "
              f"have {(last_dt - first_dt).days}d total")
        return 1

    print(f"\nWalk-forward config: {N_FOLDS} non-overlapping folds × {FOLD_DAYS}d each")
    print(f"Total OOS window: {total_days}d ({wf_start_dt.date()} → {last_dt.date()})")
    print(f"Timeframe: {TIMEFRAME}, fee profile: {FEE_PROFILE}")
    print(f"Leverage: {LEVERAGE}x (invariant for SWIFT per task #110)")
    print(f"Params: FIXED Pine defaults (no retuning)")
    print(f"Gate: mean OOS Calmar ≥ {GATE_CALMAR}")
    print("=" * 80)

    fold_results: list[FoldResult] = []
    for fold in range(N_FOLDS):
        fold_start_dt = wf_start_dt + pd.Timedelta(days=fold * FOLD_DAYS)
        fold_end_dt = fold_start_dt + pd.Timedelta(days=FOLD_DAYS)
        # Find index range for this fold
        fold_mask = (df["dt"] >= fold_start_dt) & (df["dt"] < fold_end_dt)
        fold_idx = df.index[fold_mask]
        if len(fold_idx) == 0:
            print(f"Fold {fold + 1}: no data in {fold_start_dt.date()}→{fold_end_dt.date()}, skip")
            continue
        fold_start = int(fold_idx[0])
        fold_end = int(fold_idx[-1]) + 1
        # Include warmup bars before the fold's first bar
        warmup_start = max(0, fold_start - WARMUP_BARS)
        fold_df = df.iloc[warmup_start:fold_end].reset_index(drop=True)

        start_ts = str(df["dt"].iloc[fold_start])
        end_ts = str(df["dt"].iloc[fold_end - 1])
        print(f"\nFold {fold + 1}: {start_ts[:10]} → {end_ts[:10]} "
              f"({fold_end - fold_start:,} bars)")

        r = _run_fold(fold_df, m1pm, run_id=f"swift_wf_f{fold + 1}")
        max_dd = max(r["max_dd_pct"], 0.01)
        # Annualize fold return over the 90d window to get a comparable Calmar
        annualized = r["return_pct"] * (365.0 / FOLD_DAYS)
        calmar = annualized / max_dd

        fold_result = FoldResult(
            fold=fold + 1,
            start_ts=start_ts,
            end_ts=end_ts,
            trades=r["trades"],
            return_pct=r["return_pct"],
            max_dd_pct=r["max_dd_pct"],
            calmar=calmar,
            sharpe=r["sharpe"],
            win_rate=r["win_rate"],
            profit_factor=r["profit_factor"],
        )
        fold_results.append(fold_result)
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "inf"
        print(f"  ret={r['return_pct']:+7.2f}% dd={r['max_dd_pct']:5.2f}% "
              f"calmar={calmar:+6.3f} sharpe={r['sharpe']:+6.3f} "
              f"trades={r['trades']:3d} wr={r['win_rate']:5.1f}% pf={pf_str}")

    # ── Summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("WALK-FORWARD SUMMARY (SWIFT Alma, 15m × MT4)")
    print("=" * 80)

    n = len(fold_results)
    returns = [f.return_pct for f in fold_results]
    dds = [f.max_dd_pct for f in fold_results]
    calmars = [f.calmar for f in fold_results]
    sharpes = [f.sharpe for f in fold_results]
    win_rates = [f.win_rate for f in fold_results]
    trades_total = sum(f.trades for f in fold_results)

    def _mean_std(xs: list[float]) -> tuple[float, float]:
        m = sum(xs) / len(xs)
        v = sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)
        return m, math.sqrt(v)

    mean_ret, std_ret = _mean_std(returns)
    mean_dd, std_dd = _mean_std(dds)
    mean_calmar, std_calmar = _mean_std(calmars)
    mean_sharpe, std_sharpe = _mean_std(sharpes)
    mean_wr, _ = _mean_std(win_rates)

    print(f"\nFolds: {n} × {FOLD_DAYS}d OOS")
    print(f"Total trades: {trades_total}")
    print(f"Mean return:   {mean_ret:+7.2f}% ± {std_ret:6.2f} (per {FOLD_DAYS}d)")
    print(f"Mean DD:       {mean_dd:7.2f}% ± {std_dd:6.2f}")
    print(f"Mean Calmar:   {mean_calmar:+7.3f} ± {std_calmar:6.3f} (annualized)")
    print(f"Mean Sharpe:   {mean_sharpe:+7.3f} ± {std_sharpe:6.3f}")
    print(f"Mean WR:       {mean_wr:7.1f}%")

    profitable = sum(1 for r in returns if r > 0)
    print(f"Profitable folds: {profitable} / {n}")

    passed = mean_calmar >= GATE_CALMAR
    verdict = "✅ PASS" if passed else "❌ FAIL"
    print(f"\nGate (mean OOS Calmar ≥ {GATE_CALMAR}): {verdict}")
    print(f"  actual: mean={mean_calmar:+.3f}, std={std_calmar:.3f}")

    if passed:
        print("\nSWIFT IS ELIGIBLE for the institutional sub-book.")
        print("Next step: integrate SwiftAlmaStrategy into the 3-strategy portfolio")
        print("alongside donchian_gold + vol_momentum_gold.")
    else:
        print("\nSWIFT FAILED the OOS gate — it should stay research-only.")
        print("Matrix #109's 1y best cell was a favorable window, not a robust baseline.")

    # Dump structured results for downstream consumption
    out = {
        "task": 111,
        "strategy": "swift_alma",
        "timeframe": TIMEFRAME,
        "fee_profile": FEE_PROFILE,
        "leverage": LEVERAGE,
        "n_folds": n,
        "fold_days": FOLD_DAYS,
        "total_trades": trades_total,
        "mean_return_pct": mean_ret,
        "std_return_pct": std_ret,
        "mean_dd_pct": mean_dd,
        "std_dd_pct": std_dd,
        "mean_calmar": mean_calmar,
        "std_calmar": std_calmar,
        "mean_sharpe": mean_sharpe,
        "std_sharpe": std_sharpe,
        "mean_win_rate": mean_wr,
        "profitable_folds": profitable,
        "gate_calmar": GATE_CALMAR,
        "gate_passed": passed,
        "folds": [asdict(f) for f in fold_results],
    }
    out_path = REPO_ROOT / "data" / "swift_walk_forward_2026-04-15.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nJSON results → {out_path}")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
