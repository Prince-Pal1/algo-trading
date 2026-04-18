#!/usr/bin/env python3
"""Fee-aware Stage 3 + Stage 4 validator for FundingMeanReversionStrategy.

Extends scripts/research_funding_mr.py with:
  - Full 4.3y backtest (was 3mo)
  - Binance fee profile selection via --broker / --scenario
  - Monte Carlo bootstrap (Stage 4 lite)
  - Rolling-window stability analysis (Stage 4 standard)

Stage 3 gates: Sharpe > 0.3, ≥ 30 trades, net return > 0
Stage 4 gates: MC P(profit) ≥ 60%, 3/4+ windows profitable

Usage:
    python3 scripts/validate_funding_mr.py --broker binance_perpetual_futures --scenario normal
    python3 scripts/validate_funding_mr.py --broker binance_spot --scenario normal --compare
    python3 scripts/validate_funding_mr.py --broker binance_perpetual_futures --mc-runs 1000
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

from src.backtest.costs import commission_usd
from src.fees import get_broker
from src.strategies.carry.funding_mean_reversion import FundingMeanReversionStrategy
from src.utils.types import RiskProfile, SignalAction


def load_data() -> pd.DataFrame:
    ohlcv = pd.read_parquet(REPO_ROOT / "data/historical/BTCUSDT_1h.parquet")
    funding = pd.read_parquet(REPO_ROOT / "data/historical/funding/BTCUSDT_8h.parquet")
    ohlcv["ts"] = pd.to_datetime(ohlcv["timestamp"], unit="ms", utc=True)
    ohlcv = ohlcv.set_index("ts")
    funding["ts"] = pd.to_datetime(funding["timestamp"], unit="ms", utc=True)
    funding = funding.set_index("ts")
    agg = ohlcv.resample("8h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    high, low, prev_close = agg["high"], agg["low"], agg["close"].shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    agg["ATR_14"] = tr.rolling(14).mean()
    funding_8h = funding["funding_rate"].resample("8h").last().ffill()
    return agg.join(funding_8h, how="inner").dropna(subset=["ATR_14", "funding_rate"])


def simulate(
    df: pd.DataFrame,
    *,
    fee_schedule=None,
    strategy_params: dict | None = None,
) -> list[dict]:
    """Replay the strategy bar-by-bar. Return per-trade results with fee-adjusted PnL."""
    params = strategy_params or dict(
        quantile_window=90, high_quantile=0.90, low_quantile=0.10, mid_quantile=0.50,
        atr_sl_mult=2.5, max_hold_bars=6, cooldown_bars=2,
    )
    strat = FundingMeanReversionStrategy(
        name="funding_mean_reversion", markets=["BTCUSDT"], timeframe="8h",
        risk_profile=RiskProfile.SAFE, max_risk_per_trade=0.01, **params,
    )
    trades: list[dict] = []
    current: dict | None = None
    qty = 0.5  # fixed notional size for cost modelling

    for ts, row in df.iterrows():
        features = pd.Series({
            "close": row["close"], "high": row["high"], "low": row["low"],
            "ATR_14": row["ATR_14"], "funding_rate": row["funding_rate"],
        })
        sig = strat.process("BTCUSDT", "8h", features)
        if sig is None:
            continue
        if sig.action in (SignalAction.LONG, SignalAction.SHORT):
            current = {
                "entry_ts": ts,
                "entry": float(row["close"]),
                "dir": 1 if sig.action == SignalAction.LONG else -1,
                "entry_funding": float(row["funding_rate"]),
            }
        elif sig.action == SignalAction.CLOSE and current is not None:
            exit_p = float(row["close"])
            pnl_gross = current["dir"] * (exit_p - current["entry"]) / current["entry"]
            if fee_schedule is not None:
                comm = commission_usd(quantity_units=qty, schedule=fee_schedule,
                                      reference_price=current["entry"])
                fee_pct = comm / (current["entry"] * qty)
            else:
                fee_pct = 0.0
            trades.append({
                "entry_ts": current["entry_ts"],
                "exit_ts": ts,
                "dir": current["dir"],
                "pnl_gross": pnl_gross,
                "fee_pct": fee_pct,
                "pnl_net": pnl_gross - fee_pct,
                "exit_reason": sig.metadata.get("exit_reason", "?"),
            })
            current = None
    return trades


def compute_metrics(trades: list[dict], n_years: float) -> dict:
    if not trades:
        return {"n": 0}
    net = [t["pnl_net"] for t in trades]
    gross = [t["pnl_gross"] for t in trades]
    n = len(net)
    mean_net = statistics.mean(net)
    std_net = statistics.stdev(net) if n > 1 else 0.0
    trades_per_year = n / n_years
    sharpe_ann = (mean_net / std_net) * math.sqrt(trades_per_year) if std_net > 0 else 0.0
    # Equity curve and max drawdown
    eq = [1.0]
    for r in net:
        eq.append(eq[-1] * (1 + r))
    peak = eq[0]
    max_dd = 0.0
    for e in eq:
        peak = max(peak, e)
        dd = (e / peak) - 1
        max_dd = min(max_dd, dd)
    wins = sum(1 for r in net if r > 0)
    return {
        "n": n,
        "trades_per_year": round(trades_per_year, 1),
        "win_rate": wins / n,
        "sharpe_net_ann": round(sharpe_ann, 3),
        "total_gross_pct": round(sum(gross) * 100, 2),
        "total_net_pct": round(sum(net) * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "avg_fee_pct": round(statistics.mean([t["fee_pct"] for t in trades]) * 100, 4),
    }


def monte_carlo_bootstrap(trades: list[dict], n_runs: int, n_years: float) -> dict:
    """Resample trades with replacement; compute P(profit) + Sharpe distribution."""
    if not trades:
        return {"runs": 0}
    rng = np.random.default_rng(seed=42)
    net = np.array([t["pnl_net"] for t in trades])
    n = len(net)
    profits = 0
    sharpes = []
    for _ in range(n_runs):
        resampled = rng.choice(net, size=n, replace=True)
        total = (1 + resampled).prod() - 1  # compounded
        if total > 0:
            profits += 1
        mu, sd = resampled.mean(), resampled.std(ddof=1)
        if sd > 0:
            sharpes.append((mu / sd) * math.sqrt(n / n_years))
    return {
        "runs": n_runs,
        "p_profit": round(profits / n_runs, 3),
        "sharpe_5pct": round(float(np.percentile(sharpes, 5)), 3),
        "sharpe_50pct": round(float(np.percentile(sharpes, 50)), 3),
        "sharpe_95pct": round(float(np.percentile(sharpes, 95)), 3),
    }


def rolling_window_stability(trades: list[dict], n_windows: int = 4) -> list[dict]:
    """Split trades into N equal chunks; compute metrics per chunk."""
    if len(trades) < n_windows * 5:
        return []
    chunks = np.array_split(trades, n_windows)
    out = []
    for i, chunk in enumerate(chunks):
        chunk_list = list(chunk)
        if not chunk_list:
            continue
        net = [t["pnl_net"] for t in chunk_list]
        total_pct = sum(net) * 100
        mean_n = statistics.mean(net)
        std_n = statistics.stdev(net) if len(net) > 1 else 0
        sharpe_chunk = (mean_n / std_n) if std_n > 0 else 0
        out.append({
            "window": i + 1,
            "n": len(chunk_list),
            "total_pct": round(total_pct, 2),
            "profitable": total_pct > 0,
            "sharpe_raw": round(sharpe_chunk, 3),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fee-aware Funding MR Stage 3+4 validator")
    parser.add_argument("--broker", default="binance_perpetual_futures",
                        help="Broker id in config/brokers/")
    parser.add_argument("--scenario", default="normal",
                        help="Scenario within broker (normal, bnb_discount, stress, ...)")
    parser.add_argument("--mc-runs", type=int, default=500,
                        help="Number of Monte Carlo bootstrap iterations")
    parser.add_argument("--compare", action="store_true",
                        help="Run Stage 3 across all 3 fee scenarios for side-by-side")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write JSON report to this path")
    args = parser.parse_args()

    print("=== Funding MR Stage 3 + 4 validation ===\n")
    print("Loading 4.3y BTCUSDT 8h OHLCV + funding data...")
    df = load_data()
    n_years = (df.index[-1] - df.index[0]).days / 365.25
    print(f"  {len(df)} 8h bars, {df.index[0].date()} → {df.index[-1].date()} ({n_years:.1f}y)\n")

    if args.compare:
        scenarios = [
            ("binance_spot", "normal", "Binance spot 0.10%/side"),
            ("binance_spot", "bnb_discount", "Binance spot + BNB 0.075%/side"),
            ("binance_perpetual_futures", "normal", "Binance perp 0.04%/side"),
        ]
        print(f"{'Profile':<40} {'n':>4} {'Sharpe':>8} {'Gross%':>9} {'Net%':>9} {'MaxDD':>8} {'Fee%':>7}")
        print("-" * 90)
        for broker_id, scen, label in scenarios:
            sched = get_broker(broker_id).resolve_profile(
                instrument_class="crypto", scenario=scen,
            ).commission_schedule
            trades = simulate(df, fee_schedule=sched)
            m = compute_metrics(trades, n_years)
            print(f"{label:<40} {m['n']:>4} {m['sharpe_net_ann']:>+8.3f} "
                  f"{m['total_gross_pct']:>+8.2f}% {m['total_net_pct']:>+8.2f}% "
                  f"{m['max_dd_pct']:>+7.2f}% {m['avg_fee_pct']:>6.3f}%")
        return 0

    schedule = get_broker(args.broker).resolve_profile(
        instrument_class="crypto", scenario=args.scenario,
    ).commission_schedule
    print(f"Fee profile: {args.broker} / {args.scenario}")
    print(f"  taker: {schedule.taker_fraction_of_notional*100:.4f}%/side  "
          f"maker: {schedule.maker_fraction_of_notional*100:.4f}%/side\n")

    trades = simulate(df, fee_schedule=schedule)
    metrics = compute_metrics(trades, n_years)
    print("=== Stage 3 Results ===")
    for k, v in metrics.items():
        print(f"  {k:<20} {v}")
    stage3_pass = metrics["n"] >= 30 and metrics["sharpe_net_ann"] > 0.3 and metrics["total_net_pct"] > 0
    print(f"\n  Stage 3 GATE: {'✅ PASS' if stage3_pass else '❌ FAIL'}\n")

    print(f"=== Stage 4 Monte Carlo bootstrap ({args.mc_runs} runs) ===")
    mc = monte_carlo_bootstrap(trades, args.mc_runs, n_years)
    for k, v in mc.items():
        print(f"  {k:<20} {v}")
    stage4_mc_pass = mc.get("p_profit", 0) >= 0.60
    print(f"\n  Stage 4 MC GATE: {'✅ PASS' if stage4_mc_pass else '❌ FAIL'} (need P(profit) ≥ 0.60)\n")

    print("=== Stage 4 Rolling-window stability (4 equal chunks) ===")
    windows = rolling_window_stability(trades, n_windows=4)
    profitable = sum(1 for w in windows if w["profitable"])
    print(f"  {'Window':<8} {'n':>4} {'Return%':>9} {'Sharpe_raw':>11}")
    for w in windows:
        mark = "✅" if w["profitable"] else "❌"
        print(f"  {w['window']:<8} {w['n']:>4} {w['total_pct']:>+8.2f}% {w['sharpe_raw']:>+10.3f} {mark}")
    stage4_stab_pass = profitable >= 3
    print(f"\n  Stage 4 Stability GATE: {'✅ PASS' if stage4_stab_pass else '❌ FAIL'} "
          f"(need 3/4+ windows profitable, got {profitable}/4)\n")

    verdict = "PASS" if (stage3_pass and stage4_mc_pass and stage4_stab_pass) else "FAIL"
    print(f"=== OVERALL: {verdict} ===")

    if args.out:
        out = {
            "broker": args.broker, "scenario": args.scenario,
            "stage3": metrics, "stage3_pass": stage3_pass,
            "stage4_mc": mc, "stage4_mc_pass": stage4_mc_pass,
            "stage4_stability": windows, "stage4_stability_pass": stage4_stab_pass,
            "verdict": verdict,
        }
        args.out.write_text(json.dumps(out, indent=2, default=str))
        print(f"\n  Report written to {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
