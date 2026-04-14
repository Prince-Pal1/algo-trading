#!/usr/bin/env python3
"""Tier 5 M1 revival research — Task #100 (2026-04-14).

Revisit the two bar-level-killed aggressive strategies at M1 cadence
now that G.5b's M1PathModel makes tick-adjacent intrabar ordering
available:

  1. candle_burst_hunter — run it on M1 bars where a "burst" is detected
     within 1 minute instead of within 5 minutes. The entry fires at
     M1 close, which is at most 1 minute into the burst (vs 5 minutes
     on M5). Grid sweeps burst_atr_mult + hard_sl_pct.

  2. news_spike_fade — run it on M1 bars with the new peak_reversal
     entry mode. The strategy waits for a reversal bar within the news
     window and enters at the close of that bar. On M1 this is 1 minute
     after the peak. Grid sweeps spike_trigger_pips + reversal_pips.

Gate: any config with `return_pct > 0` AND `max_dd_pct < 30`
AND `total_trades >= 20` on 2yr M1 data → candidate for revival.
Otherwise: amend the obituary with M1 results.

Scope: NOT full strategy development process. This is "does M1 fix
the fundamental problem?". If yes, the strategy graduates to the
normal 7-stage dev process. If no, we've ruled out M1 as a rescue.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass

import pandas as pd
import structlog

# Silence the per-signal structlog stream — keeps the sweep output readable.
# Only WARN+ gets through, so backtest start/complete INFO events vanish.
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from src.backtest.costs import load_news_calendar_csv
from src.backtest.leveraged_engine import LeveragedBacktestEngine, SUB_BOOK_AGGRESSIVE
from src.backtest.path import BrownianBridgeModel
from src.strategies.aggressive.candle_burst_hunter import CandleBurstHunterStrategy
from src.strategies.aggressive.news_spike_fade import NewsSpikeFadeStrategy


M1_PATH = "data/historical/XAUUSD_1m.parquet"
NEWS_CSV = "config/news_calendar.csv"
OUT_PATH = "data/tier5_m1_revival.json"
INDICATORS_M1 = ["ema_9", "ema_21", "atr_20"]
# Use last 12 months of M1 data (~350k bars) for first-pass research to
# keep sweep time under 5 minutes. If any config passes the gate, we
# re-run on full 2yr for OOS validation before reviving the strategy.
SLICE_BARS = 350_000


@dataclass
class ConfigResult:
    strategy: str
    params: dict
    trades: int
    return_pct: float
    max_dd_pct: float
    final_equity: float
    stop_outs: int


def _load_m1_data() -> tuple[pd.DataFrame, object]:
    print(f"Loading {M1_PATH}...", flush=True)
    m1 = pd.read_parquet(M1_PATH).sort_values("timestamp").reset_index(drop=True)
    if len(m1) > SLICE_BARS:
        m1 = m1.tail(SLICE_BARS).reset_index(drop=True)
    print(f"  {len(m1):,} M1 bars (tail slice)", flush=True)
    # M1 is the base timeframe for this sweep — no sub-bar intrabar data
    # to path-model. BrownianBridgeModel is correct when the base
    # timeframe IS the atomic unit. M1PathModel is for M5/H1 backtests
    # where M1 sub-bars exist INSIDE each bar.
    pm = BrownianBridgeModel(run_id="tier5_m1")
    return m1, pm


def _run_one(
    strategy,
    m1: pd.DataFrame,
    pm,
    leverage: float = 500.0,
) -> ConfigResult:
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=7_000.0,
        initial_aggressive_cash=3_000.0,
        path_model=pm,
        run_id=f"tier5_m1_{strategy.name}",
    )
    result = engine.run(
        strategy=strategy,
        data=m1,
        symbol="XAUUSD",
        timeframe="1m",
        leverage=leverage,
        sub_book=SUB_BOOK_AGGRESSIVE,
        indicators=INDICATORS_M1,
    )
    trades = result.trades
    curve = result.equity_curve_aggressive
    initial = 3_000.0
    final = curve[-1] if curve else initial
    return_pct = (final - initial) / initial * 100.0
    peak = initial
    max_dd = 0.0
    for eq in curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return ConfigResult(
        strategy=strategy.name,
        params={},
        trades=len(trades),
        return_pct=return_pct,
        max_dd_pct=max_dd,
        final_equity=final,
        stop_outs=result.broker_stop_out_count,
    )


def _sweep_candle_burst(m1: pd.DataFrame, pm) -> list[ConfigResult]:
    print("\n── candle_burst_hunter @ M1 cadence ──")
    results: list[ConfigResult] = []

    # Grid: burst_atr_mult × hard_sl_pct × max_hold_bars (in M1 bars)
    configs = [
        # Very aggressive — catch any burst
        {"burst_atr_mult": 1.5, "hard_sl_pct": 0.003, "max_hold_bars": 30,
         "atr_period": 20, "trail_activation_pips": 3.0, "trail_distance_pips": 1.5},
        {"burst_atr_mult": 2.0, "hard_sl_pct": 0.003, "max_hold_bars": 30,
         "atr_period": 20, "trail_activation_pips": 3.0, "trail_distance_pips": 1.5},
        # Tighter SL to cut the losers faster
        {"burst_atr_mult": 2.0, "hard_sl_pct": 0.0015, "max_hold_bars": 20,
         "atr_period": 20, "trail_activation_pips": 2.0, "trail_distance_pips": 1.0},
        {"burst_atr_mult": 2.5, "hard_sl_pct": 0.0015, "max_hold_bars": 20,
         "atr_period": 20, "trail_activation_pips": 2.0, "trail_distance_pips": 1.0},
        # Wider SL to ride through noise
        {"burst_atr_mult": 2.0, "hard_sl_pct": 0.005, "max_hold_bars": 60,
         "atr_period": 20, "trail_activation_pips": 5.0, "trail_distance_pips": 2.5},
        # Very tight burst threshold → more trades
        {"burst_atr_mult": 1.3, "hard_sl_pct": 0.002, "max_hold_bars": 15,
         "atr_period": 20, "trail_activation_pips": 2.0, "trail_distance_pips": 1.0},
    ]

    for cfg in configs:
        strat = CandleBurstHunterStrategy(
            timeframe="1m",
            max_risk_per_trade=0.05,
            **cfg,
        )
        r = _run_one(strat, m1, pm, leverage=500.0)
        r.params = cfg
        results.append(r)
        print(f"  cfg={cfg} → trades={r.trades}, ret={r.return_pct:+.2f}%, "
              f"dd={r.max_dd_pct:.2f}%, stop_outs={r.stop_outs}", flush=True)

    return results


def _sweep_news_spike_fade(m1: pd.DataFrame, pm) -> list[ConfigResult]:
    print("\n── news_spike_fade @ M1 cadence (peak_reversal=True) ──")
    news_windows = load_news_calendar_csv(NEWS_CSV)
    print(f"  {len(news_windows)} news windows loaded")

    results: list[ConfigResult] = []

    # Grid: spike_trigger × reversal_pips × target_pct × time_stop (M1 bars)
    configs = [
        {"spike_trigger_pips": 30.0, "reversal_pips": 3.0, "target_pct": 0.5,
         "sl_mult": 1.2, "time_stop_bars": 30},
        {"spike_trigger_pips": 30.0, "reversal_pips": 5.0, "target_pct": 0.5,
         "sl_mult": 1.2, "time_stop_bars": 30},
        {"spike_trigger_pips": 30.0, "reversal_pips": 3.0, "target_pct": 0.7,
         "sl_mult": 1.0, "time_stop_bars": 45},
        {"spike_trigger_pips": 50.0, "reversal_pips": 5.0, "target_pct": 0.5,
         "sl_mult": 1.2, "time_stop_bars": 30},
        {"spike_trigger_pips": 50.0, "reversal_pips": 8.0, "target_pct": 0.6,
         "sl_mult": 1.0, "time_stop_bars": 45},
        # Very tight reversal — catch the stall early
        {"spike_trigger_pips": 30.0, "reversal_pips": 2.0, "target_pct": 0.5,
         "sl_mult": 1.2, "time_stop_bars": 30},
        # Wider target — let winners run
        {"spike_trigger_pips": 30.0, "reversal_pips": 3.0, "target_pct": 1.0,
         "sl_mult": 1.2, "time_stop_bars": 45},
    ]

    for cfg in configs:
        strat = NewsSpikeFadeStrategy(
            timeframe="1m",
            max_risk_per_trade=0.02,
            news_windows=news_windows,
            require_peak_reversal=True,
            **cfg,
        )
        r = _run_one(strat, m1, pm, leverage=500.0)
        r.params = cfg
        results.append(r)
        print(f"  cfg={cfg} → trades={r.trades}, ret={r.return_pct:+.2f}%, "
              f"dd={r.max_dd_pct:.2f}%, stop_outs={r.stop_outs}", flush=True)

    return results


def _verdict(results: list[ConfigResult]) -> tuple[str, str]:
    """Gate: return_pct > 0 AND max_dd < 30 AND trades >= 20."""
    candidates = [
        r for r in results
        if r.return_pct > 0 and r.max_dd_pct < 30 and r.trades >= 20
    ]
    if candidates:
        best = max(candidates, key=lambda r: r.return_pct / max(r.max_dd_pct, 1.0))
        return "REVIVE", f"best: {best.strategy} {best.params} → "\
               f"+{best.return_pct:.2f}% / {best.max_dd_pct:.2f}% DD / {best.trades} trades"
    return "KILL", "no config meets gate (return > 0, dd < 30, trades >= 20)"


def main() -> None:
    print("Tier 5 M1 revival research — Task #100")
    print("=" * 65)

    m1, pm = _load_m1_data()

    cb_results = _sweep_candle_burst(m1, pm)
    nsf_results = _sweep_news_spike_fade(m1, pm)

    print()
    print("=" * 65)
    print("Verdicts:")
    cb_verdict, cb_msg = _verdict(cb_results)
    nsf_verdict, nsf_msg = _verdict(nsf_results)
    print(f"  candle_burst_hunter: {cb_verdict} — {cb_msg}")
    print(f"  news_spike_fade:     {nsf_verdict} — {nsf_msg}")

    out = {
        "task": "#100 Tier 5 M1 revival",
        "date": "2026-04-14",
        "candle_burst": {
            "verdict": cb_verdict,
            "summary": cb_msg,
            "configs": [{"params": r.params, "trades": r.trades,
                         "return_pct": r.return_pct, "max_dd_pct": r.max_dd_pct,
                         "stop_outs": r.stop_outs}
                        for r in cb_results],
        },
        "news_spike_fade": {
            "verdict": nsf_verdict,
            "summary": nsf_msg,
            "configs": [{"params": r.params, "trades": r.trades,
                         "return_pct": r.return_pct, "max_dd_pct": r.max_dd_pct,
                         "stop_outs": r.stop_outs}
                        for r in nsf_results],
        },
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
