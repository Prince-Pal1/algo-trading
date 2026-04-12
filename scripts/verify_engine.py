"""Verify backtest engine against TradingView's strategy tester.

Runs a simple SMA(10)/SMA(20) crossover on BTCUSDT 1H (Jan 2023 - Apr 2026)
with zero commission, zero slippage, 100% equity sizing — matching TradingView
defaults for an apples-to-apples comparison.

Usage:
    python -m scripts.verify_engine
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction


# ── SMA Crossover Strategy ───────────────────────────────────────────────────


class SMACrossoverStrategy(BaseStrategy):
    """Simple SMA(10)/SMA(20) crossover — long only, no stops.

    LONG  when SMA_10 crosses above SMA_20.
    CLOSE when SMA_10 crosses below SMA_20.
    """

    def __init__(self):
        super().__init__(
            name="sma_crossover_verify",
            markets=["BTCUSDT"],
            timeframe="1h",
        )

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        prev = self._prev_features
        if prev is None:
            return None

        sma10 = features.get("SMA_10")
        sma20 = features.get("SMA_20")
        prev_sma10 = prev.get("SMA_10")
        prev_sma20 = prev.get("SMA_20")

        # Need all four values to detect a crossover
        if any(v is None or pd.isna(v) for v in [sma10, sma20, prev_sma10, prev_sma20]):
            return None

        # Bullish crossover: SMA10 crosses above SMA20
        if prev_sma10 <= prev_sma20 and sma10 > sma20 and self._position == "FLAT":
            return Signal(
                symbol=symbol,
                action=SignalAction.LONG,
                confidence=1.0,
                strategy_name=self.name,
                timeframe=timeframe,
                stop_loss=None,      # No stop loss -> uses fallback sizing
                take_profit=None,
                risk_pct=None,       # Will use engine's risk_per_trade (1.0)
            )

        # Bearish crossover: SMA10 crosses below SMA20
        if prev_sma10 >= prev_sma20 and sma10 < sma20 and self._position == "LONG":
            return Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                confidence=1.0,
                strategy_name=self.name,
                timeframe=timeframe,
            )

        return None


# ── Helpers ──────────────────────────────────────────────────────────────────


def ts_to_str(ts_ms: int) -> str:
    """Convert unix-ms timestamp to human-readable UTC string."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_trades_table(trades, df, label: str) -> None:
    """Print a formatted table of trades."""
    print(f"\n{'─' * 100}")
    print(f"  {label}")
    print(f"{'─' * 100}")
    print(
        f"  {'#':>4}  {'Entry Date':<17} {'Exit Date':<17} "
        f"{'Entry Price':>12} {'Exit Price':>12} {'Side':<6} {'PnL':>12} {'PnL%':>8}"
    )
    print(f"  {'─' * 4}  {'─' * 17} {'─' * 17} {'─' * 12} {'─' * 12} {'─' * 6} {'─' * 12} {'─' * 8}")
    for i, t in enumerate(trades, 1):
        # entry_idx is the signal bar; actual entry is at next bar's open (entry_idx+1)
        entry_ts = int(df.iloc[min(t.entry_idx + 1, len(df) - 1)]["timestamp"])
        exit_ts = int(df.iloc[min(t.exit_idx + 1, len(df) - 1)]["timestamp"]) if t.exit_reason == "signal" else int(df.iloc[t.exit_idx]["timestamp"])
        print(
            f"  {i:>4}  {ts_to_str(entry_ts):<17} {ts_to_str(exit_ts):<17} "
            f"${t.entry_price:>11,.2f} ${t.exit_price:>11,.2f} {t.side:<6} "
            f"${t.pnl:>11,.2f} {t.pnl_pct:>7.2%}"
        )


# ── Main ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    print("=" * 100)
    print("  BACKTEST ENGINE VERIFICATION vs TradingView")
    print("  Strategy: SMA(10) / SMA(20) Crossover — Long Only")
    print("  Symbol: BTCUSDT | Timeframe: 1H | Period: Jan 2023 - Apr 2026")
    print("  Commission: 0% | Slippage: 0% | Sizing: 100% equity")
    print("=" * 100)

    # ── 1. Download data ─────────────────────────────────────────────────
    print("\n[1/3] Downloading BTCUSDT 1H data from Binance...")
    downloader = BinanceDownloader()
    try:
        df = await downloader.download(
            symbol="BTCUSDT",
            timeframe="1h",
            start_date="2023-01-01",
            end_date="2026-04-12",
        )
    finally:
        await downloader.close()

    print(f"       Downloaded {len(df):,} candles")
    print(f"       Range: {ts_to_str(int(df['timestamp'].iloc[0]))} → {ts_to_str(int(df['timestamp'].iloc[-1]))}")

    # Trim to match TradingView's trading range (Jan 5, 2023 00:00 UTC onward)
    # TV needs ~20 bars warmup for SMA(20), its first trade is Jan 5 00:00 UTC
    # We keep data from Jan 1 for SMA warmup but note TV starts trading from bar ~96

    # ── 2. Run backtest ──────────────────────────────────────────────────
    print("\n[2/3] Running backtest...")

    config = BacktestConfig(
        initial_capital=10_000.0,
        commission_pct=0.0,       # Zero commission (match TradingView)
        slippage_pct=0.0,        # Zero slippage (match TradingView)
        risk_per_trade=1.0,      # 100% of equity (fallback sizing)
        max_notional_pct=1.0,    # No leverage cap
    )

    engine = BacktestEngine(config=config)
    strategy = SMACrossoverStrategy()

    result = engine.run(
        strategy=strategy,
        data=df,
        symbol="BTCUSDT",
        timeframe="1h",
        indicators=["sma_10", "sma_20"],   # Only what we need
    )

    # ── 3. Print results ─────────────────────────────────────────────────
    print("\n[3/3] Results:\n")

    m = result.metrics
    print(f"  Total Trades:      {m.get('total_trades', 0)}")
    print(f"  Total Return:      {m.get('total_return_pct', 0):.2f}%")
    print(f"  Final Equity:      ${m.get('final_equity', 0):,.2f}")
    print(f"  Win Rate:          {m.get('win_rate_pct', 0):.1f}%")
    print(f"  Profit Factor:     {m.get('profit_factor', 0):.3f}")
    print(f"  Max Drawdown:      {m.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Sharpe Ratio:      {m.get('sharpe', 0):.3f}")
    print(f"  Commission:        ${m.get('total_commission', 0):.2f}")

    trades = result.trades
    n = len(trades)

    if n > 0:
        # First 10 trades
        first_10 = trades[:min(10, n)]
        print_trades_table(first_10, df, f"FIRST {len(first_10)} TRADES")

        # Last 10 trades (only if there are more than 10)
        if n > 10:
            last_10 = trades[-10:]
            print_trades_table(last_10, df, f"LAST 10 TRADES (of {n} total)")

    print(f"\n{'=' * 100}")
    print("  Compare these numbers against TradingView Strategy Tester:")
    print("  Pine: strategy.entry on ta.crossover(ta.sma(close,10), ta.sma(close,20))")
    print("        strategy.close on ta.crossunder(ta.sma(close,10), ta.sma(close,20))")
    print("  Settings: BTCUSDT, 1H, Initial Capital $10000, Order Size 100% of equity,")
    print("            Commission 0%, Slippage 0 ticks, process_orders_on_close=false")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    asyncio.run(main())
