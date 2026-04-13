#!/usr/bin/env python3
"""Parameter tuning grid for vol_momentum_gold on XAUUSD 1h."""

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


@dataclass
class TuneRow:
    momentum_window: int
    vol_lookback: int
    vol_target: float
    sl_atr_mult: float
    rebalance_interval: int
    long_only: bool
    session_filter: bool
    total_return_pct: float
    max_dd_pct: float
    calmar: float
    sharpe: float
    total_trades: int


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


def _run_config(
    df: pd.DataFrame,
    *,
    momentum_window: int,
    vol_lookback: int,
    vol_target: float,
    sl_atr_mult: float,
    rebalance_interval: int,
    long_only: bool,
    session_filter: bool,
) -> TuneRow:
    strategy = VolMomentumGoldStrategy(
        momentum_window=momentum_window,
        vol_lookback=vol_lookback,
        vol_target=vol_target,
        sl_atr_mult=sl_atr_mult,
        rebalance_interval=rebalance_interval,
        long_only=long_only,
        session_filter=session_filter,
    )
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=TOTAL_CAPITAL,
        initial_aggressive_cash=0.0,
        run_id=f"vmg_{momentum_window}_{vol_lookback}_{int(vol_target*100)}",
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
    max_dd = max(m["max_dd_pct"], 0.01)
    annualized = m["total_return_pct"] * (PERIODS_PER_YEAR / len(df))
    calmar = annualized / max_dd if max_dd > 0 else 0.0
    sharpe = _sharpe_from_equity(result.equity_curve_total, PERIODS_PER_YEAR)

    return TuneRow(
        momentum_window=momentum_window,
        vol_lookback=vol_lookback,
        vol_target=vol_target,
        sl_atr_mult=sl_atr_mult,
        rebalance_interval=rebalance_interval,
        long_only=long_only,
        session_filter=session_filter,
        total_return_pct=m["total_return_pct"],
        max_dd_pct=m["max_dd_pct"],
        calmar=calmar,
        sharpe=sharpe,
        total_trades=m["total_trades"],
    )


def main() -> None:
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} bars of {DATA_PATH}")

    momentum_windows = [72, 168, 240]
    vol_lookbacks = [168, 240]
    vol_targets = [0.10, 0.15, 0.20]
    sl_mults = [2.0, 2.5, 3.0]
    rebalance_intervals = [24]
    long_only_opts = [False, True]
    session_opts = [False, True]

    combos = list(itertools.product(
        momentum_windows, vol_lookbacks, vol_targets,
        sl_mults, rebalance_intervals, long_only_opts, session_opts,
    ))
    print(f"Evaluating {len(combos)} configurations...")

    results: list[TuneRow] = []
    for i, (mw, vl, vt, sl, ri, lo, sess) in enumerate(combos):
        r = _run_config(
            df,
            momentum_window=mw, vol_lookback=vl, vol_target=vt,
            sl_atr_mult=sl, rebalance_interval=ri,
            long_only=lo, session_filter=sess,
        )
        results.append(r)
        if (i + 1) % 30 == 0:
            print(f"  {i + 1}/{len(combos)} done")

    eligible = [r for r in results if r.max_dd_pct < 20.0 and r.total_trades >= 50]
    if not eligible:
        eligible = [r for r in results if r.total_trades >= 30]
    eligible.sort(key=lambda r: r.calmar, reverse=True)

    print()
    print("Top 10 (Calmar, max_dd<20%, trades>=50):")
    header = (
        f"{'mw':>4} | {'vl':>4} | {'vt':>5} | {'sl':>4} | {'ri':>3} | "
        f"{'LO':>3} | {'sess':>5} | {'ret%':>8} | {'DD%':>7} | {'Calmar':>7} | {'Sharpe':>7} | {'trades':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in eligible[:10]:
        print(
            f"{r.momentum_window:>4} | {r.vol_lookback:>4} | {r.vol_target:>5.2f} | "
            f"{r.sl_atr_mult:>4.1f} | {r.rebalance_interval:>3} | "
            f"{str(r.long_only)[:3]:>3} | {str(r.session_filter)[:5]:>5} | "
            f"{r.total_return_pct:>8.2f} | {r.max_dd_pct:>7.2f} | "
            f"{r.calmar:>7.3f} | {r.sharpe:>7.3f} | {r.total_trades:>6}"
        )

    if eligible:
        best = eligible[0]
        print()
        print(
            f"Best: mw={best.momentum_window} vl={best.vol_lookback} vt={best.vol_target} "
            f"sl={best.sl_atr_mult} ri={best.rebalance_interval} "
            f"long_only={best.long_only} session={best.session_filter}"
        )
        print(
            f"  ret={best.total_return_pct:.2f}% dd={best.max_dd_pct:.2f}% "
            f"Calmar={best.calmar:.3f} Sharpe={best.sharpe:.3f} trades={best.total_trades}"
        )


if __name__ == "__main__":
    main()
