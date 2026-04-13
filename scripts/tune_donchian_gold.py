#!/usr/bin/env python3
"""Parameter tuning grid for donchian_gold on XAUUSD 1h."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import pandas as pd

from src.backtest.book import SUB_BOOK_INSTITUTIONAL
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy


DATA_PATH = "data/historical/XAUUSD_1h.parquet"
INDICATORS = [
    "donchian_20", "donchian_40", "donchian_55", "donchian_80", "donchian_120",
    "atr_14", "atr_20", "adx_14",
]
TOTAL_CAPITAL = 10_000.0
PERIODS_PER_YEAR = 6240.0


@dataclass
class TuneRow:
    dc_short: int
    dc_medium: int
    dc_long: int
    sl_atr_mult: float
    adx_threshold: float | None
    min_channels: int
    risk_pct: float
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
    dc_short: int,
    dc_medium: int,
    dc_long: int,
    sl_atr_mult: float,
    adx_threshold: float | None,
    min_channels: int,
    risk_pct: float,
    session_filter: bool,
) -> TuneRow:
    strategy = DonchianGoldStrategy(
        session_filter=session_filter,
        adx_trend_threshold=adx_threshold,
        dc_short=dc_short,
        dc_medium=dc_medium,
        dc_long=dc_long,
        sl_atr_mult=sl_atr_mult,
        min_channels=min_channels,
        max_risk_per_trade=risk_pct,
    )
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=TOTAL_CAPITAL,
        initial_aggressive_cash=0.0,
        run_id=f"tune_{dc_short}_{sl_atr_mult}_{adx_threshold}_{min_channels}",
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
        dc_short=dc_short,
        dc_medium=dc_medium,
        dc_long=dc_long,
        sl_atr_mult=sl_atr_mult,
        adx_threshold=adx_threshold,
        min_channels=min_channels,
        risk_pct=risk_pct,
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

    channel_sets = [
        (20, 55, 120),
        (40, 80, 160),
        (20, 80, 160),
    ]
    sl_mults = [1.5, 2.0, 2.5, 3.0]
    adx_thresholds = [None, 20.0, 25.0]
    min_channels_opts = [1, 2]
    risk_pcts = [0.005, 0.01, 0.02]
    session_opts = [True, False]

    combos = list(itertools.product(
        channel_sets, sl_mults, adx_thresholds,
        min_channels_opts, risk_pcts, session_opts,
    ))
    print(f"Evaluating {len(combos)} configurations...")
    print()

    results: list[TuneRow] = []
    for i, ((dc_s, dc_m, dc_l), sl, adx, mc, risk, sess) in enumerate(combos):
        r = _run_config(
            df,
            dc_short=dc_s, dc_medium=dc_m, dc_long=dc_l,
            sl_atr_mult=sl, adx_threshold=adx, min_channels=mc,
            risk_pct=risk, session_filter=sess,
        )
        results.append(r)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(combos)} done")

    # Rank by Calmar subject to max_dd < 20% and >= 50 trades
    eligible = [r for r in results if r.max_dd_pct < 20.0 and r.total_trades >= 50]
    if not eligible:
        eligible = [r for r in results if r.total_trades >= 50]

    eligible.sort(key=lambda r: r.calmar, reverse=True)

    print()
    print("Top 10 configurations (ranked by Calmar, max_dd<20%, trades>=50):")
    print()
    header = (
        f"{'dc':>13} | {'sl':>4} | {'adx':>5} | {'mc':>3} | {'risk':>5} | "
        f"{'sess':>5} | {'ret%':>8} | {'DD%':>7} | {'Calmar':>7} | {'Sharpe':>7} | {'trades':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in eligible[:10]:
        adx_str = f"{r.adx_threshold:.0f}" if r.adx_threshold is not None else "none"
        print(
            f"{r.dc_short:>3}/{r.dc_medium:>3}/{r.dc_long:>3} | "
            f"{r.sl_atr_mult:>4.1f} | "
            f"{adx_str:>5} | "
            f"{r.min_channels:>3} | "
            f"{r.risk_pct:>5.3f} | "
            f"{str(r.session_filter)[:5]:>5} | "
            f"{r.total_return_pct:>8.2f} | "
            f"{r.max_dd_pct:>7.2f} | "
            f"{r.calmar:>7.3f} | "
            f"{r.sharpe:>7.3f} | "
            f"{r.total_trades:>6}"
        )

    if eligible:
        best = eligible[0]
        print()
        print(
            f"Best config: dc=({best.dc_short},{best.dc_medium},{best.dc_long}) "
            f"sl_atr_mult={best.sl_atr_mult} adx={best.adx_threshold} "
            f"min_channels={best.min_channels} risk_pct={best.risk_pct} "
            f"session_filter={best.session_filter}"
        )
        print(
            f"  return={best.total_return_pct:.2f}% max_dd={best.max_dd_pct:.2f}% "
            f"calmar={best.calmar:.3f} sharpe={best.sharpe:.3f} trades={best.total_trades}"
        )


if __name__ == "__main__":
    main()
