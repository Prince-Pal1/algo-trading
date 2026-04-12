"""Run SMA(10)/SMA(20) crossover across ALL timeframes on BTCUSDT.

Analyzes how timeframe affects: trade count, return, win rate, drawdown, etc.
Goal: understand the relationship between timeframe and strategy performance.

Usage:
    python -m scripts.verify_all_timeframes
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction


class SMACrossoverStrategy(BaseStrategy):
    """SMA(10)/SMA(20) crossover — long only, no stops."""

    def __init__(self):
        super().__init__(name="sma_xover", markets=["BTCUSDT"], timeframe="1h")

    def on_features(self, symbol: str, timeframe: str, features: pd.Series) -> Signal | None:
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


TIMEFRAMES = [
    ("5m",  "5min"),
    ("15m", "15min"),
    ("30m", "30min"),
    ("1h",  "1hour"),
    ("2h",  "2hour"),
    ("4h",  "4hour"),
    ("1d",  "1day"),
    ("1w",  "1week"),
]


async def main() -> None:
    print("=" * 120)
    print("  SMA(10)/SMA(20) CROSSOVER — ALL TIMEFRAMES")
    print("  Symbol: BTCUSDT | Period: Jan 2023 - Apr 2026 | Commission: 0% | Sizing: 100% equity")
    print("=" * 120)

    downloader = BinanceDownloader()
    config = BacktestConfig(
        initial_capital=10_000.0,
        commission_pct=0.0,
        slippage_pct=0.0,
        risk_per_trade=1.0,
        max_notional_pct=1.0,
    )

    results = []

    for tf, tf_label in TIMEFRAMES:
        print(f"\n  Downloading {tf}...", end=" ", flush=True)
        try:
            df = await downloader.download(
                symbol="BTCUSDT",
                timeframe=tf,
                start_date="2023-01-01",
                end_date="2026-04-12",
            )
            print(f"{len(df):,} candles.", end=" ", flush=True)

            if len(df) < 50:
                print("Too few candles, skipping.")
                continue

            engine = BacktestEngine(config=config)
            strategy = SMACrossoverStrategy()
            result = engine.run(
                strategy=strategy, data=df, symbol="BTCUSDT",
                timeframe=tf, indicators=["sma_10", "sma_20"],
            )

            m = result.metrics
            n_trades = m.get("total_trades", 0)

            # Calculate annualized return
            if len(df) > 1:
                first_ts = df["timestamp"].iloc[0]
                last_ts = df["timestamp"].iloc[-1]
                years = (last_ts - first_ts) / (1000 * 86400 * 365.25)
            else:
                years = 1

            total_ret = m.get("total_return_pct", 0)
            if total_ret > -100:
                ann_ret = ((1 + total_ret / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
            else:
                ann_ret = -100

            trades_per_year = n_trades / years if years > 0 else 0

            # Time in market (approximate)
            if n_trades > 0 and len(df) > 0:
                bars_in_trade = sum(t.exit_idx - t.entry_idx for t in result.trades)
                time_in_market = bars_in_trade / len(df) * 100
            else:
                time_in_market = 0

            # Average trade duration in bars
            if n_trades > 0:
                avg_bars = sum(t.exit_idx - t.entry_idx for t in result.trades) / n_trades
            else:
                avg_bars = 0

            # Average win and loss
            winners = [t for t in result.trades if t.pnl > 0]
            losers = [t for t in result.trades if t.pnl <= 0]
            avg_win_pct = sum(t.pnl_pct for t in winners) / len(winners) * 100 if winners else 0
            avg_loss_pct = sum(t.pnl_pct for t in losers) / len(losers) * 100 if losers else 0

            row = {
                "tf": tf,
                "candles": len(df),
                "trades": n_trades,
                "trades_yr": round(trades_per_year, 1),
                "total_ret": round(total_ret, 2),
                "ann_ret": round(ann_ret, 2),
                "win_rate": round(m.get("win_rate_pct", 0), 1),
                "pf": round(m.get("profit_factor", 0), 3),
                "sharpe": round(m.get("sharpe", 0), 3),
                "max_dd": round(m.get("max_drawdown_pct", 0), 2),
                "avg_bars": round(avg_bars, 1),
                "time_in_mkt": round(time_in_market, 1),
                "avg_win": round(avg_win_pct, 3),
                "avg_loss": round(avg_loss_pct, 3),
                "final_eq": round(m.get("final_equity", 0), 2),
            }
            results.append(row)
            print(f"Done. {n_trades} trades, {total_ret:.1f}% return")

        except Exception as e:
            print(f"Error: {e}")

    await downloader.close()

    # ── Print results table ──────────────────────────────────────────────
    print("\n\n")
    print("=" * 140)
    print("  RESULTS BY TIMEFRAME")
    print("=" * 140)
    print(
        f"  {'TF':<5} {'Candles':>8} {'Trades':>7} {'Tr/Yr':>7} "
        f"{'Total%':>9} {'Ann%':>8} {'WinRate':>8} {'PF':>7} {'Sharpe':>7} "
        f"{'MaxDD%':>8} {'AvgBars':>8} {'InMkt%':>7} "
        f"{'AvgWin%':>9} {'AvgLoss%':>9} {'Final$':>12}"
    )
    print(f"  {'─' * 5} {'─' * 8} {'─' * 7} {'─' * 7} {'─' * 9} {'─' * 8} {'─' * 8} {'─' * 7} {'─' * 7} {'─' * 8} {'─' * 8} {'─' * 7} {'─' * 9} {'─' * 9} {'─' * 12}")

    for r in results:
        print(
            f"  {r['tf']:<5} {r['candles']:>8,} {r['trades']:>7} {r['trades_yr']:>7.1f} "
            f"{r['total_ret']:>8.2f}% {r['ann_ret']:>7.2f}% {r['win_rate']:>7.1f}% {r['pf']:>7.3f} {r['sharpe']:>7.3f} "
            f"{r['max_dd']:>7.2f}% {r['avg_bars']:>8.1f} {r['time_in_mkt']:>6.1f}% "
            f"{r['avg_win']:>8.3f}% {r['avg_loss']:>8.3f}% ${r['final_eq']:>11,.2f}"
        )

    # ── Buy & Hold benchmark ─────────────────────────────────────────────
    print(f"\n  {'BTC Buy & Hold (Jan 2023 - Apr 2026)':.<50} ~336% total, ~68% annualized")

    # ── Analysis ─────────────────────────────────────────────────────────
    print("\n\n")
    print("=" * 140)
    print("  ANALYSIS")
    print("=" * 140)

    if results:
        best = max(results, key=lambda r: r["total_ret"])
        worst = min(results, key=lambda r: r["total_ret"])
        best_sharpe = max(results, key=lambda r: r["sharpe"])
        most_trades = max(results, key=lambda r: r["trades"])

        print(f"\n  Best total return:   {best['tf']} → {best['total_ret']:.2f}%")
        print(f"  Worst total return:  {worst['tf']} → {worst['total_ret']:.2f}%")
        print(f"  Best Sharpe:         {best_sharpe['tf']} → {best_sharpe['sharpe']:.3f}")
        print(f"  Most trades:         {most_trades['tf']} → {most_trades['trades']} ({most_trades['trades_yr']:.0f}/yr)")
        print()

        # Pattern analysis
        print("  KEY OBSERVATIONS:")
        print("  ─────────────────")

        # Check if higher TF = better
        tf_order = ["5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"]
        rets = [(r["tf"], r["total_ret"]) for r in results]
        print(f"  Return by TF:  {', '.join(f'{tf}: {ret:.0f}%' for tf, ret in rets)}")

        win_rates = [(r["tf"], r["win_rate"]) for r in results]
        print(f"  Win rate by TF: {', '.join(f'{tf}: {wr:.0f}%' for tf, wr in win_rates)}")

        sharpes = [(r["tf"], r["sharpe"]) for r in results]
        print(f"  Sharpe by TF:  {', '.join(f'{tf}: {s:.2f}' for tf, s in sharpes)}")

        # Trade frequency analysis
        print()
        for r in results:
            beat_bh = "BEATS BUY&HOLD" if r["total_ret"] > 336 else "LOSES to B&H"
            print(f"  {r['tf']:>4}: {r['trades_yr']:.0f} trades/yr, avg hold {r['avg_bars']:.0f} bars, in market {r['time_in_mkt']:.0f}% of time → {beat_bh}")

    print(f"\n{'=' * 140}")


if __name__ == "__main__":
    asyncio.run(main())
