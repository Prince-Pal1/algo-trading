#!/usr/bin/env python3
"""Leverage sweep for donchian_gold on XAUUSD 1h.

Runs the strategy through LeveragedBacktestEngine at L ∈ {1, 5, 10, 25, 50}
and reports return / DD / stop-outs / trades per level.
"""

from __future__ import annotations

import pandas as pd

from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy


LEVERAGE_LEVELS = [1.0, 5.0, 10.0, 25.0, 50.0]
DATA_PATH = "data/historical/XAUUSD_1h.parquet"


def _fmt(val: float) -> str:
    return f"{val:>10.2f}"


def main() -> None:
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} bars of {DATA_PATH}")
    print()

    header = f"{'L':>5} | {'return%':>10} | {'inst%':>10} | {'maxDD%':>10} | {'trades':>6} | {'stopouts':>8}"
    print(header)
    print("-" * len(header))

    for L in LEVERAGE_LEVELS:
        strat = DonchianGoldStrategy(
            session_filter=True,
            adx_trend_threshold=20.0,
            max_risk_per_trade=0.01,
        )
        engine = LeveragedBacktestEngine(
            initial_institutional_cash=10_000.0,
            initial_aggressive_cash=0.0,
            run_id=f"donchian_gold_L{int(L)}",
        )
        result = engine.run(
            strategy=strat,
            data=df,
            symbol="XAUUSD",
            timeframe="1h",
            leverage=L,
            indicators=[
                "donchian_20", "donchian_55", "donchian_120",
                "atr_14", "adx_14",
            ],
        )
        m = result.metrics
        print(
            f"{int(L):>5} | "
            f"{_fmt(m['total_return_pct'])} | "
            f"{_fmt(m['institutional_return_pct'])} | "
            f"{_fmt(m['max_dd_pct'])} | "
            f"{m['total_trades']:>6} | "
            f"{m['broker_stop_outs']:>8}"
        )


if __name__ == "__main__":
    main()
