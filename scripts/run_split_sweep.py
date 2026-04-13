#!/usr/bin/env python3
"""G.2g split sweep: find the Calmar-optimal institutional_pct for the gold book."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from src.backtest.book import SUB_BOOK_AGGRESSIVE, SUB_BOOK_INSTITUTIONAL
from src.backtest.costs import load_news_calendar_csv
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.aggressive.candle_burst_hunter import CandleBurstHunterStrategy
from src.strategies.aggressive.news_spike_fade import NewsSpikeFadeStrategy
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy


DATA_PATH = "data/historical/XAUUSD_1h.parquet"
NEWS_CALENDAR_PATH = "config/news_calendar.csv"
INDICATORS = [
    "donchian_20", "donchian_55", "donchian_120",
    "atr_14", "atr_20", "adx_14",
]
TOTAL_CAPITAL = 10_000.0
SPLITS = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95]
# Bars per year for XAUUSD 1h (5 trading days × 24h = 120h/week × 52 = 6240).
# Used for Sharpe annualization.
PERIODS_PER_YEAR = 6240.0


@dataclass
class SplitResult:
    institutional_pct: float
    total_return_pct: float
    institutional_return_pct: float
    aggressive_return_pct: float
    max_dd_pct: float
    calmar: float
    sharpe: float
    total_trades: int
    broker_stop_outs: int
    final_total_equity: float


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


def _run_split(df: pd.DataFrame, institutional_pct: float) -> SplitResult:
    institutional_cash = TOTAL_CAPITAL * institutional_pct
    aggressive_cash = TOTAL_CAPITAL * (1.0 - institutional_pct)

    # Fresh strategy instances per split so internal state doesn't leak
    donchian = DonchianGoldStrategy(session_filter=True, adx_trend_threshold=20.0)
    candle_burst = CandleBurstHunterStrategy(
        atr_period=20, burst_atr_mult=1.5,
    )
    news_fade = NewsSpikeFadeStrategy(
        news_calendar_path=NEWS_CALENDAR_PATH,
        spike_trigger_pips=30.0,
    )

    engine = LeveragedBacktestEngine(
        initial_institutional_cash=institutional_cash,
        initial_aggressive_cash=aggressive_cash,
        run_id=f"split_{int(institutional_pct * 100)}",
    )
    result = engine.run_multi(
        strategy_routes=[
            (donchian, SUB_BOOK_INSTITUTIONAL, 25.0),
            (candle_burst, SUB_BOOK_AGGRESSIVE, 500.0),
            (news_fade, SUB_BOOK_AGGRESSIVE, 500.0),
        ],
        data=df,
        indicators=INDICATORS,
    )

    m = result.metrics
    max_dd = max(m["max_dd_pct"], 0.01)  # avoid div-by-zero
    annualized = m["total_return_pct"] * (PERIODS_PER_YEAR / len(df))
    calmar = annualized / max_dd if max_dd > 0 else 0.0
    sharpe = _sharpe_from_equity(result.equity_curve_total, PERIODS_PER_YEAR)

    return SplitResult(
        institutional_pct=institutional_pct,
        total_return_pct=m["total_return_pct"],
        institutional_return_pct=m["institutional_return_pct"],
        aggressive_return_pct=m["aggressive_return_pct"],
        max_dd_pct=m["max_dd_pct"],
        calmar=calmar,
        sharpe=sharpe,
        total_trades=m["total_trades"],
        broker_stop_outs=m["broker_stop_outs"],
        final_total_equity=m["final_total_equity"],
    )


def main() -> None:
    df = pd.read_parquet(DATA_PATH)
    news_windows = load_news_calendar_csv(NEWS_CALENDAR_PATH)
    print(f"Loaded {len(df):,} bars of {DATA_PATH}")
    print(f"Loaded {len(news_windows)} news windows from {NEWS_CALENDAR_PATH}")
    print(f"Total capital per split: ${TOTAL_CAPITAL:,.0f}")
    print(f"PERIODS_PER_YEAR (Sharpe annualization): {PERIODS_PER_YEAR}")
    print()

    header = (
        f"{'inst%':>6} | {'total%':>8} | {'inst_ret%':>10} | {'aggr_ret%':>10} | "
        f"{'maxDD%':>8} | {'Calmar':>7} | {'Sharpe':>7} | {'trades':>6} | {'stops':>5}"
    )
    print(header)
    print("-" * len(header))

    results: list[SplitResult] = []
    for pct in SPLITS:
        r = _run_split(df, pct)
        results.append(r)
        print(
            f"{int(pct * 100):>5}% | "
            f"{r.total_return_pct:>8.2f} | "
            f"{r.institutional_return_pct:>10.2f} | "
            f"{r.aggressive_return_pct:>10.2f} | "
            f"{r.max_dd_pct:>8.2f} | "
            f"{r.calmar:>7.3f} | "
            f"{r.sharpe:>7.3f} | "
            f"{r.total_trades:>6} | "
            f"{r.broker_stop_outs:>5}"
        )

    # Pick Calmar-optimal subject to max_dd < 30%
    eligible = [r for r in results if r.max_dd_pct < 30.0]
    if eligible:
        best = max(eligible, key=lambda r: r.calmar)
        print()
        print(f"Calmar-optimal split (max_dd < 30%): institutional_pct = {best.institutional_pct:.2f}")
        print(
            f"  Calmar={best.calmar:.3f}, Sharpe={best.sharpe:.3f}, "
            f"total_return={best.total_return_pct:.2f}%, max_dd={best.max_dd_pct:.2f}%, "
            f"trades={best.total_trades}, stop_outs={best.broker_stop_outs}"
        )
    else:
        print("\nNo split satisfies max_dd < 30% constraint — all overshot.")
        best = max(results, key=lambda r: r.calmar)
        print(f"Best unconstrained Calmar: institutional_pct={best.institutional_pct:.2f}")


if __name__ == "__main__":
    main()
