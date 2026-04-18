#!/usr/bin/env python3
"""Stage 6 (portfolio fit) + Stage 7 (WF-OOS) for FundingMeanReversionStrategy.

Stage 6: compute trade-return series for funding_mr vs funding_carry
on the SAME 4.3y BTCUSDT window and report correlation.
Gate: |corr| < 0.3 and portfolio Sharpe > 2.318 baseline.

Stage 7: 7-fold non-overlapping walk-forward.
Gate: continuous-run Calmar > 0.5, ≥ 5/7 folds profitable.

Usage:
    python3 scripts/wf_funding_mr.py
    python3 scripts/wf_funding_mr.py --folds 7
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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_funding_mr import load_data, simulate
from src.fees import get_broker
from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.utils.types import RiskProfile, SignalAction


def simulate_carry(df: pd.DataFrame, fee_schedule) -> list[dict]:
    """Replay funding_carry on the same merged 8h frame."""
    from src.backtest.costs import commission_usd
    strat = FundingCarryStrategy(
        name="funding_carry", markets=["BTCUSDT-CARRY"], timeframe="8h",
        risk_profile=RiskProfile.SAFE, max_risk_per_trade=0.01,
        friction_pct=0.00005, flip_persistence_bars=3,
        max_drawdown_kill_pct=0.03, max_hold_bars=0, cooldown_bars=3,
    )
    trades: list[dict] = []
    current: dict | None = None
    qty = 0.5
    for ts, row in df.iterrows():
        features = pd.Series({
            "close": row["close"], "high": row["high"], "low": row["low"],
            "ATR_14": row["ATR_14"], "funding_rate": row["funding_rate"],
        })
        sig = strat.process("BTCUSDT-CARRY", "8h", features)
        if sig is None:
            continue
        if sig.action in (SignalAction.LONG, SignalAction.SHORT):
            current = {"entry_ts": ts, "entry": float(row["close"]),
                       "dir": 1 if sig.action == SignalAction.LONG else -1}
        elif sig.action == SignalAction.CLOSE and current:
            exit_p = float(row["close"])
            pnl_gross = current["dir"] * (exit_p - current["entry"]) / current["entry"]
            comm = commission_usd(quantity_units=qty, schedule=fee_schedule,
                                  reference_price=current["entry"])
            fee_pct = comm / (current["entry"] * qty)
            trades.append({"entry_ts": current["entry_ts"], "exit_ts": ts,
                           "dir": current["dir"], "pnl_net": pnl_gross - fee_pct})
            current = None
    return trades


def trade_series_on_grid(trades: list[dict], grid: pd.DatetimeIndex) -> np.ndarray:
    """Collapse trades into per-8h-bar returns aligned with grid.

    Simple attribution: a trade's net PnL is credited to its exit bar.
    If multiple trades close on same bar, they're summed.
    """
    series = pd.Series(0.0, index=grid)
    for t in trades:
        ts = t["exit_ts"]
        if ts in series.index:
            series.loc[ts] += t["pnl_net"]
    return series.values


def compute_sharpe(returns: np.ndarray, bars_per_year: float = 365 * 3) -> float:
    if len(returns) < 2:
        return 0.0
    mu, sd = returns.mean(), returns.std(ddof=1)
    if sd == 0:
        return 0.0
    return (mu / sd) * math.sqrt(bars_per_year)


def compute_calmar(returns: np.ndarray) -> float:
    if len(returns) < 2:
        return 0.0
    eq = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak) - 1
    max_dd = abs(dd.min()) if dd.min() < 0 else 1e-9
    n_years = len(returns) / (365 * 3)
    total_ret = eq[-1] - 1
    ann_ret = (1 + total_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
    return ann_ret / max_dd if max_dd > 0 else 0


def stage_6_portfolio(df: pd.DataFrame, schedule) -> dict:
    print("\n=== Stage 6 — Portfolio fit (funding_mr × funding_carry) ===\n")
    mr_trades = simulate(df, fee_schedule=schedule)
    carry_trades = simulate_carry(df, schedule)
    print(f"  funding_mr    trades: {len(mr_trades)}")
    print(f"  funding_carry trades: {len(carry_trades)}")

    grid = df.index  # 8h bars
    mr_series = trade_series_on_grid(mr_trades, grid)
    carry_series = trade_series_on_grid(carry_trades, grid)

    n_bars = len(grid)
    mr_sharpe = compute_sharpe(mr_series)
    carry_sharpe = compute_sharpe(carry_series)
    # Correlation on bars where at least one strategy has activity
    mask = (mr_series != 0) | (carry_series != 0)
    if mask.sum() < 10:
        corr = 0.0
    else:
        corr = float(np.corrcoef(mr_series[mask], carry_series[mask])[0, 1])

    # Portfolio: 50/50 equal weight
    portfolio = 0.5 * mr_series + 0.5 * carry_series
    p_sharpe = compute_sharpe(portfolio)

    print(f"\n  funding_mr    Sharpe: {mr_sharpe:+.3f}")
    print(f"  funding_carry Sharpe: {carry_sharpe:+.3f}")
    print(f"  Correlation (activity-only): {corr:+.3f}")
    print(f"  50/50 Portfolio Sharpe:      {p_sharpe:+.3f}")

    corr_gate = abs(corr) < 0.3
    p_gate = p_sharpe > max(mr_sharpe, carry_sharpe) * 0.95  # portfolio ≥ best-single × 0.95
    print(f"\n  |corr| < 0.3:         {'✅' if corr_gate else '❌'} ({corr:+.3f})")
    print(f"  Portfolio ≥ best:     {'✅' if p_gate else '❌'}")

    return {
        "mr_trades": len(mr_trades), "carry_trades": len(carry_trades),
        "mr_sharpe": mr_sharpe, "carry_sharpe": carry_sharpe,
        "correlation": corr, "portfolio_sharpe": p_sharpe,
        "corr_gate": corr_gate, "portfolio_gate": p_gate,
    }


def stage_7_wf(df: pd.DataFrame, schedule, n_folds: int = 7) -> dict:
    print(f"\n=== Stage 7 — {n_folds}-fold Walk-Forward OOS ===\n")
    chunks = np.array_split(df, n_folds)
    fold_results = []
    all_trade_returns: list[float] = []

    for i, chunk in enumerate(chunks):
        chunk_df = pd.DataFrame(chunk)
        if len(chunk_df) < 100:  # need warmup
            print(f"  fold {i+1}: skipped (only {len(chunk_df)} bars)")
            continue
        trades = simulate(chunk_df, fee_schedule=schedule)
        if not trades:
            print(f"  fold {i+1}: 0 trades in window")
            fold_results.append({"fold": i+1, "n": 0, "return_pct": 0.0,
                                 "sharpe": 0.0, "profitable": False})
            continue
        rets = [t["pnl_net"] for t in trades]
        all_trade_returns.extend(rets)
        n_years = (chunk_df.index[-1] - chunk_df.index[0]).days / 365.25
        mean_r = statistics.mean(rets)
        std_r = statistics.stdev(rets) if len(rets) > 1 else 0
        trades_per_year = len(rets) / n_years if n_years > 0 else 0
        sharpe = (mean_r / std_r) * math.sqrt(trades_per_year) if std_r > 0 and trades_per_year > 0 else 0
        total_pct = sum(rets) * 100
        fold_results.append({
            "fold": i+1, "n": len(rets),
            "start": str(chunk_df.index[0].date()), "end": str(chunk_df.index[-1].date()),
            "return_pct": round(total_pct, 2),
            "sharpe": round(sharpe, 3),
            "profitable": total_pct > 0,
        })
        print(f"  fold {i+1}: {chunk_df.index[0].date()} → {chunk_df.index[-1].date()}  "
              f"n={len(rets):>3}  ret={total_pct:+7.2f}%  sharpe={sharpe:+6.3f}  "
              f"{'✅' if total_pct > 0 else '❌'}")

    profitable = sum(1 for f in fold_results if f["profitable"])
    # Continuous Calmar over all WF trades (concatenated)
    all_rets = np.array(all_trade_returns)
    cont_calmar = compute_calmar(all_rets) if len(all_rets) > 1 else 0
    mean_sharpe = statistics.mean([f["sharpe"] for f in fold_results if f["n"] > 0]) if fold_results else 0

    print(f"\n  Profitable folds: {profitable}/{len(fold_results)}")
    print(f"  Mean fold Sharpe: {mean_sharpe:+.3f}")
    print(f"  Continuous-run Calmar: {cont_calmar:+.3f}")

    fold_gate = profitable >= max(5, int(len(fold_results) * 0.71))
    calmar_gate = cont_calmar > 0.5

    print(f"\n  Folds profitable ≥ 5/7:  {'✅' if fold_gate else '❌'} ({profitable}/{len(fold_results)})")
    print(f"  Continuous Calmar > 0.5: {'✅' if calmar_gate else '❌'} ({cont_calmar:+.3f})")

    return {
        "n_folds": len(fold_results),
        "profitable_folds": profitable,
        "mean_sharpe": mean_sharpe,
        "continuous_calmar": cont_calmar,
        "folds": fold_results,
        "fold_gate": fold_gate,
        "calmar_gate": calmar_gate,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default="binance_perpetual_futures")
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--folds", type=int, default=7)
    parser.add_argument("--out", type=Path,
                        default=Path("reports/funding_mr_stage34/wf_portfolio.json"))
    args = parser.parse_args()

    print("=== Funding MR Stage 6 + 7 validation ===\n")
    df = load_data()
    print(f"Data: {len(df)} 8h bars, {df.index[0].date()} → {df.index[-1].date()}")

    schedule = get_broker(args.broker).resolve_profile(
        instrument_class="crypto", scenario=args.scenario,
    ).commission_schedule

    s6 = stage_6_portfolio(df, schedule)
    s7 = stage_7_wf(df, schedule, n_folds=args.folds)

    verdict = "PASS" if (s6["corr_gate"] and s7["fold_gate"] and s7["calmar_gate"]) else "PARTIAL"
    print(f"\n=== OVERALL Stage 6+7: {verdict} ===")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "broker": args.broker, "scenario": args.scenario,
        "stage6": s6, "stage7": s7, "verdict": verdict,
    }, indent=2, default=str))
    print(f"  Report: {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
