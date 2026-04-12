"""Comprehensive comparison: SMA Crossover vs BB+RSI Mean Reversion.

Runs both strategies across multiple timeframes AND time intervals
with identical data ranges for fair comparison.

Matrix:
  Timeframes: 1m, 5m, 15m, 30m, 1h, 1d
  Intervals:  1 day, 1 week, 1 month, 3 months, 6 months, 1 year
  (lower TFs skip longer intervals where data isn't available)

Both strategies use:
  - 0.04% commission (realistic Binance rate)
  - 0.02% slippage
  - 1% risk per trade, 2x max notional
  - Same data, same engine, same config

Usage:
    python -m scripts.compare_strategies
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy
from src.strategies.day_trading.bb_rsi_mr import BBRSIMeanRevStrategy
from src.utils.types import Signal, SignalAction


# ── SMA Crossover Strategy ──────────────────────────────────────────────────

class SMACrossoverStrategy(BaseStrategy):
    """SMA(10)/SMA(20) crossover — long only, no stops."""

    def __init__(self):
        super().__init__(name="sma_xover", markets=["BTCUSDT"], timeframe="1h")

    def on_features(self, symbol, timeframe, features) -> Signal | None:
        prev = self._prev_features
        if prev is None:
            return None

        sma10 = features.get("SMA_10")
        sma20 = features.get("SMA_20")
        prev_sma10 = prev.get("SMA_10")
        prev_sma20 = prev.get("SMA_20")

        if any(v is None or pd.isna(v) for v in [sma10, sma20, prev_sma10, prev_sma20]):
            return None

        if prev_sma10 <= prev_sma20 and sma10 > sma20 and self._position == "FLAT":
            return Signal(
                symbol=symbol, action=SignalAction.LONG, confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
            )

        if prev_sma10 >= prev_sma20 and sma10 < sma20 and self._position == "LONG":
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
            )
        return None


# ── Config ───────────────────────────────────────────────────────────────────

# Realistic trading config (same for both strategies)
BACKTEST_CONFIG = BacktestConfig(
    initial_capital=10_000.0,
    commission_pct=0.0004,    # 0.04% (Binance maker)
    slippage_pct=0.0002,      # 0.02%
    risk_per_trade=0.01,      # 1% risk per trade
    max_notional_pct=2.0,     # 2x max notional
)

# BB+RSI validated params from STATE.md
BB_RSI_PARAMS = dict(
    bb_period=20, rsi_period=14, adx_period=14, atr_period=14,
    rsi_overbought=75.0, rsi_oversold=25.0, adx_threshold=20.0,
    sl_atr_mult=3.0, max_hold_bars=48, cooldown_bars=5,
)

# Indicators needed
SMA_INDICATORS = ["sma_10", "sma_20"]
BB_RSI_INDICATORS = ["bbands_20", "rsi_14", "adx_14", "atr_14"]

# Test matrix
TIMEFRAMES = ["5m", "15m", "30m", "1h"]

# Intervals defined as (label, days_back)
INTERVALS = [
    ("1 month", 30),
    ("3 months", 90),
    ("6 months", 180),
    ("1 year", 365),
]

SYMBOL = "BTCUSDT"


# ── Helpers ──────────────────────────────────────────────────────────────────

def create_bb_rsi(tf: str) -> BBRSIMeanRevStrategy:
    return BBRSIMeanRevStrategy(
        name="bb_rsi_mr",
        markets=[SYMBOL],
        timeframe=tf,
        **BB_RSI_PARAMS,
    )


def run_backtest(strategy, data, tf, indicators):
    engine = BacktestEngine(config=BACKTEST_CONFIG)
    result = engine.run(
        strategy=strategy, data=data, symbol=SYMBOL,
        timeframe=tf, indicators=indicators,
    )
    return result


def extract_metrics(result, data_days: float) -> dict:
    m = result.metrics
    if not m:
        return {
            "trades": 0, "ret": 0, "ann_ret": 0, "wr": 0,
            "pf": 0, "sharpe": 0, "max_dd": 0, "comm": 0,
        }

    total_ret = m.get("total_return_pct", 0)
    years = data_days / 365.25
    if total_ret > -100 and years > 0:
        ann_ret = ((1 + total_ret / 100) ** (1 / years) - 1) * 100
    else:
        ann_ret = total_ret

    return {
        "trades": m.get("total_trades", 0),
        "ret": round(total_ret, 2),
        "ann_ret": round(ann_ret, 1),
        "wr": round(m.get("win_rate_pct", 0), 1),
        "pf": round(m.get("profit_factor", 0), 3),
        "sharpe": round(m.get("sharpe", 0), 3),
        "max_dd": round(m.get("max_drawdown_pct", 0), 2),
        "comm": round(m.get("total_commission", 0), 2),
    }


async def main() -> None:
    print("=" * 130)
    print("  COMPREHENSIVE STRATEGY COMPARISON: SMA CROSSOVER vs BB+RSI MEAN REVERSION")
    print("  Symbol: BTCUSDT | Commission: 0.04% | Slippage: 0.02% | Risk: 1% per trade")
    print("=" * 130)

    downloader = BinanceDownloader()

    # First, download the maximum data range needed (1 year back from today)
    end_date = "2026-04-12"
    end_dt = datetime(2026, 4, 12, tzinfo=timezone.utc)

    # Store all results: results[tf][interval] = {"sma": {...}, "bbrsi": {...}}
    all_results = {}

    for tf in TIMEFRAMES:
        all_results[tf] = {}
        print(f"\n{'─' * 130}")
        print(f"  TIMEFRAME: {tf}")
        print(f"{'─' * 130}")

        for interval_label, days_back in INTERVALS:
            start_dt = end_dt - timedelta(days=days_back)
            start_date = start_dt.strftime("%Y-%m-%d")

            print(f"    {interval_label} ({start_date} → {end_date})...", end=" ", flush=True)

            try:
                df = await downloader.download(
                    symbol=SYMBOL, timeframe=tf,
                    start_date=start_date, end_date=end_date,
                )

                if len(df) < 50:
                    print(f"Too few candles ({len(df)}), skipping.")
                    all_results[tf][interval_label] = None
                    continue

                # Run SMA Crossover
                sma_strat = SMACrossoverStrategy()
                sma_result = run_backtest(sma_strat, df.copy(), tf, SMA_INDICATORS)
                sma_metrics = extract_metrics(sma_result, days_back)

                # Run BB+RSI Mean Reversion
                bbrsi_strat = create_bb_rsi(tf)
                bbrsi_result = run_backtest(bbrsi_strat, df.copy(), tf, BB_RSI_INDICATORS)
                bbrsi_metrics = extract_metrics(bbrsi_result, days_back)

                # Buy & hold return
                bh_ret = round((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100, 2)

                all_results[tf][interval_label] = {
                    "sma": sma_metrics,
                    "bbrsi": bbrsi_metrics,
                    "bh": bh_ret,
                    "candles": len(df),
                }

                print(
                    f"{len(df):>7,} candles | "
                    f"SMA: {sma_metrics['trades']:>4} tr, {sma_metrics['ret']:>8.2f}% | "
                    f"BB+RSI: {bbrsi_metrics['trades']:>4} tr, {bbrsi_metrics['ret']:>8.2f}% | "
                    f"B&H: {bh_ret:>8.2f}%"
                )

            except Exception as e:
                print(f"Error: {e}")
                all_results[tf][interval_label] = None

    await downloader.close()

    # ── Print comprehensive tables ───────────────────────────────────────
    print("\n\n")
    print("=" * 130)
    print("  DETAILED RESULTS BY TIMEFRAME AND INTERVAL")
    print("=" * 130)

    for tf in TIMEFRAMES:
        print(f"\n{'━' * 130}")
        print(f"  TIMEFRAME: {tf}")
        print(f"{'━' * 130}")

        header = (
            f"  {'Interval':<12} │ {'Strategy':<10} │ {'Trades':>6} │ {'Return%':>9} │ "
            f"{'Ann.Ret%':>9} │ {'WinRate':>7} │ {'PF':>7} │ {'Sharpe':>7} │ "
            f"{'MaxDD%':>7} │ {'Commis$':>9} │ {'B&H%':>8}"
        )
        print(header)
        print(f"  {'─' * 12}─┼─{'─' * 10}─┼─{'─' * 6}─┼─{'─' * 9}─┼─{'─' * 9}─┼─{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 9}─┼─{'─' * 8}")

        for interval_label, _ in INTERVALS:
            data = all_results[tf].get(interval_label)
            if data is None:
                print(f"  {interval_label:<12} │ {'—':^10} │ {'—':^6} │ {'—':^9} │ {'—':^9} │ {'—':^7} │ {'—':^7} │ {'—':^7} │ {'—':^7} │ {'—':^9} │ {'—':^8}")
                continue

            sma = data["sma"]
            bbrsi = data["bbrsi"]
            bh = data["bh"]

            # SMA row
            print(
                f"  {interval_label:<12} │ {'SMA':<10} │ {sma['trades']:>6} │ "
                f"{sma['ret']:>8.2f}% │ {sma['ann_ret']:>8.1f}% │ {sma['wr']:>6.1f}% │ "
                f"{sma['pf']:>7.3f} │ {sma['sharpe']:>7.3f} │ {sma['max_dd']:>6.2f}% │ "
                f"${sma['comm']:>8.2f} │ {bh:>7.2f}%"
            )

            # BB+RSI row
            print(
                f"  {'':12} │ {'BB+RSI':<10} │ {bbrsi['trades']:>6} │ "
                f"{bbrsi['ret']:>8.2f}% │ {bbrsi['ann_ret']:>8.1f}% │ {bbrsi['wr']:>6.1f}% │ "
                f"{bbrsi['pf']:>7.3f} │ {bbrsi['sharpe']:>7.3f} │ {bbrsi['max_dd']:>6.2f}% │ "
                f"${bbrsi['comm']:>8.2f} │ {'':>8}"
            )

            # Separator between intervals
            print(f"  {'─' * 12}─┼─{'─' * 10}─┼─{'─' * 6}─┼─{'─' * 9}─┼─{'─' * 9}─┼─{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 9}─┼─{'─' * 8}")

    # ── Summary table: Winner by category ────────────────────────────────
    print("\n\n")
    print("=" * 130)
    print("  WINNER SUMMARY (who wins each metric in each TF/interval combo)")
    print("=" * 130)

    sma_wins = 0
    bbrsi_wins = 0
    bh_wins = 0
    total_comparisons = 0

    print(f"\n  {'TF':<5} {'Interval':<12} │ {'Return':^12} │ {'Win Rate':^12} │ {'PF':^12} │ {'Sharpe':^12} │ {'MaxDD':^12} │ {'vs B&H':^12}")
    print(f"  {'─'*5} {'─'*12}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")

    for tf in TIMEFRAMES:
        for interval_label, _ in INTERVALS:
            data = all_results[tf].get(interval_label)
            if data is None:
                continue

            sma = data["sma"]
            bbrsi = data["bbrsi"]
            bh = data["bh"]
            total_comparisons += 1

            def winner(sma_v, bbrsi_v, higher_better=True):
                if higher_better:
                    return "SMA" if sma_v > bbrsi_v else ("BB+RSI" if bbrsi_v > sma_v else "TIE")
                else:
                    return "SMA" if sma_v < bbrsi_v else ("BB+RSI" if bbrsi_v < sma_v else "TIE")

            w_ret = winner(sma["ret"], bbrsi["ret"])
            w_wr = winner(sma["wr"], bbrsi["wr"])
            w_pf = winner(sma["pf"], bbrsi["pf"])
            w_sharpe = winner(sma["sharpe"], bbrsi["sharpe"])
            w_dd = winner(sma["max_dd"], bbrsi["max_dd"], higher_better=False)

            best_ret = max(sma["ret"], bbrsi["ret"])
            w_bh = "BEATS" if best_ret > bh else "LOSES"
            if w_bh == "BEATS":
                bh_wins += 1  # strategy beat buy&hold

            for w in [w_ret, w_wr, w_pf, w_sharpe, w_dd]:
                if w == "SMA":
                    sma_wins += 1
                elif w == "BB+RSI":
                    bbrsi_wins += 1

            print(
                f"  {tf:<5} {interval_label:<12} │ {w_ret:^12} │ {w_wr:^12} │ "
                f"{w_pf:^12} │ {w_sharpe:^12} │ {w_dd:^12} │ {w_bh:^12}"
            )

    print(f"\n  SCORECARD: SMA wins {sma_wins} │ BB+RSI wins {bbrsi_wins} │ "
          f"Beat B&H: {bh_wins}/{total_comparisons}")

    print(f"\n{'=' * 130}")


if __name__ == "__main__":
    asyncio.run(main())
