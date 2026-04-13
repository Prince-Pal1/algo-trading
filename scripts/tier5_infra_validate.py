#!/usr/bin/env python3
"""Tier 5 infrastructure validation.

NOT full alpha research. Runs each of the 3 aggressive strategies with
its experimental entry signal enabled, through the LeveragedBacktestEngine,
and records a baseline Sharpe/Calmar/DD triple in data/tier5_baseline.json.

Do NOT promote any strategy to main regardless of results — this is
infrastructure-holds-up validation only. Full alpha research is task #80.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from src.backtest.book import SUB_BOOK_AGGRESSIVE
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.aggressive.candle_burst_hunter import CandleBurstHunterStrategy
from src.strategies.aggressive.hedged_structure_play import HedgedStructurePlayStrategy
from src.strategies.aggressive.news_spike_fade import NewsSpikeFadeStrategy


DATA_1H = "data/historical/XAUUSD_1h.parquet"
DATA_5M = "data/historical/XAUUSD_5m.parquet"
NEWS_CALENDAR_PATH = "config/news_calendar.csv"
OUTPUT_JSON = "data/tier5_baseline.json"
TOTAL_CAPITAL = 10_000.0
PERIODS_1H = 6240.0
PERIODS_5M = 74880.0


@dataclass
class BaselineResult:
    strategy: str
    timeframe: str
    return_pct: float
    max_dd_pct: float
    calmar: float
    sharpe: float
    total_trades: int
    broker_stop_outs: int
    note: str = ""


def _sharpe(curve: list[float], periods_per_year: float) -> float:
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


def _run(strategy, data_path: str, timeframe: str, indicators: list[str]) -> BaselineResult:
    df = pd.read_parquet(data_path)
    periods = PERIODS_5M if timeframe == "5m" else PERIODS_1H
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=0.0,
        initial_aggressive_cash=TOTAL_CAPITAL,
        run_id=f"tier5_{strategy.name}_{timeframe}",
    )
    result = engine.run(
        strategy=strategy,
        data=df,
        symbol="XAUUSD",
        timeframe=timeframe,
        leverage=500.0,
        sub_book=SUB_BOOK_AGGRESSIVE,
        indicators=indicators,
    )
    m = result.metrics
    max_dd = max(m["max_dd_pct"], 0.01)
    n_bars = len(df)
    annualized = m["total_return_pct"] * (periods / max(1, n_bars))
    calmar = annualized / max_dd if max_dd > 0 else 0.0
    sharpe = _sharpe(result.equity_curve_total, periods)

    return BaselineResult(
        strategy=strategy.name,
        timeframe=timeframe,
        return_pct=m["total_return_pct"],
        max_dd_pct=m["max_dd_pct"],
        calmar=calmar,
        sharpe=sharpe,
        total_trades=m["total_trades"],
        broker_stop_outs=m["broker_stop_outs"],
    )


def main() -> None:
    print("Tier 5 infrastructure validation (NOT alpha research)")
    print("=" * 60)
    print()

    baselines: list[BaselineResult] = []

    # 1. candle_burst_hunter with experimental EMA-trend filter on M5
    print("1. candle_burst_hunter (experimental EMA-trend filter, M5)")
    cbh = CandleBurstHunterStrategy(
        use_experimental_entry=True,
        burst_atr_mult=1.5,
        atr_period=20,
        timeframe="5m",
    )
    r = _run(cbh, DATA_5M, "5m", ["atr_14", "atr_20", "adx_14", "ema_21"])
    r.note = "experimental multi-timeframe EMA-trend filter"
    baselines.append(r)
    print(f"  → ret={r.return_pct:.2f}% dd={r.max_dd_pct:.2f}% "
          f"calmar={r.calmar:.3f} sharpe={r.sharpe:.3f} trades={r.total_trades}")

    # 2. news_spike_fade with post-news scan window on M5
    print()
    print("2. news_spike_fade (experimental post-news 3-bar scan, M5)")
    nsf = NewsSpikeFadeStrategy(
        use_experimental_entry=True,
        news_calendar_path=NEWS_CALENDAR_PATH,
        spike_trigger_pips=30.0,
        post_news_scan_bars=3,
        timeframe="5m",
    )
    r = _run(nsf, DATA_5M, "5m", ["atr_14"])
    r.note = "experimental post-news 3-bar scan"
    baselines.append(r)
    print(f"  → ret={r.return_pct:.2f}% dd={r.max_dd_pct:.2f}% "
          f"calmar={r.calmar:.3f} sharpe={r.sharpe:.3f} trades={r.total_trades}")

    # 3. hedged_structure_play with breakout entry seed on 1h
    print()
    print("3. hedged_structure_play (experimental breakout seed, 1h)")
    hsp = HedgedStructurePlayStrategy(
        use_experimental_entry=True,
        breakout_lookback_bars=24,  # 24 hours at 1h
        timeframe="1h",
    )
    r = _run(hsp, DATA_1H, "1h", ["atr_14"])
    r.note = "experimental breakout seed, 24-bar lookback"
    baselines.append(r)
    print(f"  → ret={r.return_pct:.2f}% dd={r.max_dd_pct:.2f}% "
          f"calmar={r.calmar:.3f} sharpe={r.sharpe:.3f} trades={r.total_trades}")

    # Write JSON
    out = {"baselines": [asdict(b) for b in baselines]}
    Path(OUTPUT_JSON).parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_JSON).write_text(json.dumps(out, indent=2))
    print()
    print(f"✓ Wrote {OUTPUT_JSON}")
    print()
    print("REMINDER: This is infrastructure validation, NOT alpha research.")
    print("Do NOT promote any of these baselines to main. Full research is task #80.")


if __name__ == "__main__":
    main()
