#!/usr/bin/env python3
"""M5 donchian_gold retune with scaled channel periods.

1h defaults were (20, 55, 120). On M5, the equivalent time horizons are
12x longer in bars. This script tests multiple scaling factors to find
the most profitable donchian configuration on M5 data.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import pandas as pd

from src.backtest.book import SUB_BOOK_INSTITUTIONAL
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy


DATA_PATH = "data/historical/XAUUSD_5m.parquet"
TOTAL_CAPITAL = 10_000.0
PERIODS_PER_YEAR = 74880.0  # 5m × 12 bars/h × 6240 h/yr

# Channel period sets (scaled from 1h (20, 55, 120)):
# - 12x: (240, 660, 1440) full time-equivalent
# - 6x: (120, 330, 720) half-equivalent (reacts faster)
# - 3x: (60, 165, 360) quarter-equivalent (much faster)
# - (200, 400, 800) custom geometric spacing
CHANNEL_SETS = [
    (240, 660, 1440),
    (120, 330, 720),
    (60, 165, 360),
    (200, 400, 800),
]


@dataclass
class TuneRow:
    dc_short: int
    dc_medium: int
    dc_long: int
    sl_atr_mult: float
    adx_threshold: float | None
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


def _build_indicators(channel_set: tuple[int, int, int]) -> list[str]:
    return [
        f"donchian_{channel_set[0]}",
        f"donchian_{channel_set[1]}",
        f"donchian_{channel_set[2]}",
        "atr_14", "atr_20", "adx_14",
    ]


def _run_config(
    df: pd.DataFrame,
    *,
    channel_set: tuple[int, int, int],
    sl_atr_mult: float,
    adx_threshold: float | None,
    risk_pct: float,
    session_filter: bool,
) -> TuneRow:
    strategy = DonchianGoldStrategy(
        session_filter=session_filter,
        adx_trend_threshold=adx_threshold,
        dc_short=channel_set[0],
        dc_medium=channel_set[1],
        dc_long=channel_set[2],
        sl_atr_mult=sl_atr_mult,
        max_risk_per_trade=risk_pct,
        timeframe="5m",
    )
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=TOTAL_CAPITAL,
        initial_aggressive_cash=0.0,
        run_id=f"m5_{channel_set[0]}_{sl_atr_mult}_{adx_threshold}",
    )
    result = engine.run(
        strategy=strategy,
        data=df,
        symbol="XAUUSD",
        timeframe="5m",
        leverage=10.0,
        sub_book=SUB_BOOK_INSTITUTIONAL,
        indicators=_build_indicators(channel_set),
    )
    m = result.metrics
    max_dd = max(m["max_dd_pct"], 0.01)
    annualized = m["total_return_pct"] * (PERIODS_PER_YEAR / len(df))
    calmar = annualized / max_dd if max_dd > 0 else 0.0
    sharpe = _sharpe_from_equity(result.equity_curve_total, PERIODS_PER_YEAR)

    return TuneRow(
        dc_short=channel_set[0],
        dc_medium=channel_set[1],
        dc_long=channel_set[2],
        sl_atr_mult=sl_atr_mult,
        adx_threshold=adx_threshold,
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

    sl_mults = [2.5, 3.0]
    adx_thresholds = [20.0, 25.0]
    risk_pcts = [0.01, 0.02]
    session_opts = [True]  # session filter always-on for gold strategies

    combos = list(itertools.product(
        CHANNEL_SETS, sl_mults, adx_thresholds, risk_pcts, session_opts,
    ))
    print(f"Evaluating {len(combos)} configurations (may take a while on 140k bars)...")

    results: list[TuneRow] = []
    for i, (cs, sl, adx, risk, sess) in enumerate(combos):
        r = _run_config(
            df,
            channel_set=cs, sl_atr_mult=sl,
            adx_threshold=adx, risk_pct=risk,
            session_filter=sess,
        )
        results.append(r)
        print(
            f"  {i + 1}/{len(combos)}: dc={cs} sl={sl} adx={adx} risk={risk} "
            f"→ ret={r.total_return_pct:.2f}% dd={r.max_dd_pct:.2f}% "
            f"calmar={r.calmar:.3f} trades={r.total_trades}"
        )

    eligible = [r for r in results if r.max_dd_pct < 20.0 and r.total_trades >= 30]
    if not eligible:
        print("\nNo config cleared max_dd<20% + trades>=30. Showing top 5 by Calmar regardless:")
        eligible = sorted(results, key=lambda r: r.calmar, reverse=True)[:5]

    eligible.sort(key=lambda r: r.calmar, reverse=True)
    print()
    print("Top 5 (Calmar, max_dd<20%, trades>=30):")
    header = (
        f"{'dc':>18} | {'sl':>4} | {'adx':>5} | {'risk':>5} | "
        f"{'ret%':>8} | {'DD%':>7} | {'Calmar':>7} | {'Sharpe':>7} | {'trades':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in eligible[:5]:
        adx_str = f"{r.adx_threshold:.0f}" if r.adx_threshold else "none"
        print(
            f"{r.dc_short:>4}/{r.dc_medium:>4}/{r.dc_long:>4} | "
            f"{r.sl_atr_mult:>4.1f} | {adx_str:>5} | {r.risk_pct:>5.3f} | "
            f"{r.total_return_pct:>8.2f} | {r.max_dd_pct:>7.2f} | "
            f"{r.calmar:>7.3f} | {r.sharpe:>7.3f} | {r.total_trades:>6}"
        )

    if eligible:
        best = eligible[0]
        gate_pass = best.calmar > 1.0 and best.max_dd_pct < 20.0
        print()
        print(f"Best M5 config: dc=({best.dc_short},{best.dc_medium},{best.dc_long}) "
              f"sl={best.sl_atr_mult} adx={best.adx_threshold} risk={best.risk_pct}")
        print(f"  ret={best.total_return_pct:.2f}% dd={best.max_dd_pct:.2f}% "
              f"calmar={best.calmar:.3f} sharpe={best.sharpe:.3f} trades={best.total_trades}")
        print(f"  Decision gate (Calmar > 1.0 AND max_dd < 20%): "
              f"{'✅ PASS — ship as donchian_gold_m5' if gate_pass else '❌ FAIL — keep 1h only'}")


if __name__ == "__main__":
    main()
