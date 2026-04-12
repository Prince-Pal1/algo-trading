"""Phase 6 — Backtest engine vs TradingView Strategy Tester comparison.

Runs a simple SMA(10)/SMA(20) long-only crossover on BTCUSDT 1H using our
BacktestEngine, dumps trades to JSON, then compares against TradingView's
Strategy Tester output (also dumped to JSON via the MCP tool `data_get_trades`).

Usage:
    # Step 1 — run our backtest, dump trades
    python3 -m scripts.verify_strategy_vs_tv backtest

    # Step 2 — (interactively) inject Pine strategy in TV, capture its trades
    #          to tests/fixtures/tv_strategy_trades.json

    # Step 3 — compare trade-by-trade
    python3 -m scripts.verify_strategy_vs_tv compare

Fixture files:
    tests/fixtures/our_strategy_trades.json   — our engine's trades
    tests/fixtures/tv_strategy_trades.json    — TradingView's trades
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy
from src.utils.types import Signal, SignalAction

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"
OUR_TRADES_PATH = FIXTURES / "our_strategy_trades.json"
TV_TRADES_PATH = FIXTURES / "tv_strategy_trades.json"

# Short window for a manageable trade list that matches TV's visible range.
START_DATE = "2023-01-01"
END_DATE = "2026-04-13"


class SMACrossoverStrategy(BaseStrategy):
    """SMA(10)/SMA(20) long-only crossover — identical to verify_engine.py."""

    def __init__(self):
        super().__init__(name="sma_crossover_verify", markets=["BTCUSDT"], timeframe="1h")

    def on_features(self, symbol, timeframe, features):
        prev = self._prev_features
        if prev is None:
            return None
        sma10, sma20 = features.get("SMA_10"), features.get("SMA_20")
        p10, p20 = prev.get("SMA_10"), prev.get("SMA_20")
        if any(v is None or pd.isna(v) for v in [sma10, sma20, p10, p20]):
            return None
        if p10 <= p20 and sma10 > sma20 and self._position == "FLAT":
            return Signal(
                symbol=symbol, action=SignalAction.LONG, confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
                stop_loss=None, take_profit=None, risk_pct=None,
            )
        if p10 >= p20 and sma10 < sma20 and self._position == "LONG":
            return Signal(
                symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                strategy_name=self.name, timeframe=timeframe,
            )
        return None


def ts_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def run_backtest() -> None:
    """Run the SMA crossover backtest and dump trades to JSON."""
    print(f"Downloading BTCUSDT 1H {START_DATE} → {END_DATE}...")
    downloader = BinanceDownloader()
    try:
        df = await downloader.download(
            symbol="BTCUSDT", timeframe="1h",
            start_date=START_DATE, end_date=END_DATE,
        )
    finally:
        await downloader.close()
    print(f"  {len(df):,} bars loaded.")

    config = BacktestConfig(
        initial_capital=10_000.0,
        commission_pct=0.0,
        slippage_pct=0.0,
        risk_per_trade=1.0,
        max_notional_pct=1.0,
    )
    engine = BacktestEngine(config=config)
    result = engine.run(
        strategy=SMACrossoverStrategy(),
        data=df, symbol="BTCUSDT", timeframe="1h",
        indicators=["sma_10", "sma_20"],
    )

    # Our engine fills on NEXT bar's open after signal bar — encode that
    # explicitly so we're comparing against the bar TV actually executes on.
    trades_out = []
    n = len(df)
    for t in result.trades:
        entry_bar = min(t.entry_idx + 1, n - 1)
        exit_bar = min(t.exit_idx + 1, n - 1) if t.exit_reason == "signal" else t.exit_idx
        trades_out.append({
            "entry_time": ts_to_iso(int(df.iloc[entry_bar]["timestamp"])),
            "exit_time": ts_to_iso(int(df.iloc[exit_bar]["timestamp"])),
            "side": t.side,
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "pnl": round(t.pnl, 2),
            "pnl_pct": round(t.pnl_pct * 100, 4),
            "exit_reason": t.exit_reason,
        })

    FIXTURES.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy": "SMA(10)/SMA(20) long-only crossover",
        "symbol": "BTCUSDT", "timeframe": "1h",
        "start_date": START_DATE, "end_date": END_DATE,
        "config": {
            "initial_capital": 10_000.0,
            "commission_pct": 0.0, "slippage_pct": 0.0,
            "risk_per_trade": 1.0, "max_notional_pct": 1.0,
        },
        "metrics": {
            "total_trades": result.metrics.get("total_trades", 0),
            "final_equity": round(result.metrics.get("final_equity", 0), 2),
            "total_return_pct": round(result.metrics.get("total_return_pct", 0), 4),
            "win_rate_pct": round(result.metrics.get("win_rate_pct", 0), 2),
            "profit_factor": round(result.metrics.get("profit_factor", 0), 4),
            "max_drawdown_pct": round(result.metrics.get("max_drawdown_pct", 0), 4),
        },
        "trades": trades_out,
    }
    OUR_TRADES_PATH.write_text(json.dumps(payload, indent=2))
    print(f"  Wrote {len(trades_out)} trades → {OUR_TRADES_PATH}")
    print(f"  Final equity: ${payload['metrics']['final_equity']:,.2f}")


def compare() -> None:
    """Load our trades + TV trades, compare side-by-side."""
    if not OUR_TRADES_PATH.exists():
        sys.exit(f"ERROR: {OUR_TRADES_PATH} missing — run `backtest` first.")
    if not TV_TRADES_PATH.exists():
        sys.exit(
            f"ERROR: {TV_TRADES_PATH} missing. Capture TV Strategy Tester trades "
            f"via TradingView MCP `data_get_trades` and save to that path."
        )

    ours = json.loads(OUR_TRADES_PATH.read_text())
    tv = json.loads(TV_TRADES_PATH.read_text())
    our_trades = ours["trades"]
    tv_trades = tv["trades"] if isinstance(tv, dict) and "trades" in tv else tv

    print("=" * 100)
    print("  STRATEGY COMPARISON — Our Engine vs TradingView")
    print("=" * 100)
    print(f"  Our engine trades: {len(our_trades)}")
    print(f"  TV trades:         {len(tv_trades)}")
    print(f"  Our final equity:  ${ours['metrics']['final_equity']:,.2f}")
    print(f"  Our total return:  {ours['metrics']['total_return_pct']:.2f}%")
    if isinstance(tv, dict) and "metrics" in tv:
        m = tv["metrics"]
        if "final_equity" in m:
            print(f"  TV final equity:   ${m['final_equity']:,.2f}")
        if "total_return_pct" in m:
            print(f"  TV total return:   {m['total_return_pct']:.2f}%")

    print("\n  Trade-by-trade comparison (entry time | side | ours entry / TV entry | "
          "ours exit / TV exit | Δ_entry | Δ_exit):")
    print("  " + "─" * 110)

    max_n = max(len(our_trades), len(tv_trades))
    matches = 0
    for i in range(max_n):
        o = our_trades[i] if i < len(our_trades) else None
        t = tv_trades[i] if i < len(tv_trades) else None
        if o and t:
            d_entry = abs(o["entry_price"] - t["entry_price"])
            d_exit = abs(o["exit_price"] - t["exit_price"])
            match = d_entry < 1.0 and d_exit < 1.0
            matches += int(match)
            flag = "✓" if match else "✗"
            print(f"  {i+1:>3} {flag} {o['entry_time']} {o['side']:<5} "
                  f"{o['entry_price']:>10,.2f}/{t['entry_price']:>10,.2f}  "
                  f"{o['exit_price']:>10,.2f}/{t['exit_price']:>10,.2f}  "
                  f"Δe={d_entry:>6.2f} Δx={d_exit:>6.2f}")
        elif o:
            print(f"  {i+1:>3} ✗ {o['entry_time']} {o['side']:<5} "
                  f"{o['entry_price']:>10,.2f}/    MISSING  "
                  f"{o['exit_price']:>10,.2f}/    MISSING  (only in ours)")
        else:
            print(f"  {i+1:>3} ✗ {t.get('entry_time', '?'):<17} {t.get('side', '?'):<5} "
                  f"    MISSING/{t.get('entry_price', 0):>10,.2f}  "
                  f"    MISSING/{t.get('exit_price', 0):>10,.2f}  (only in TV)")

    print("  " + "─" * 110)
    print(f"  Exact matches (Δ<$1 both legs): {matches}/{max_n}")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("backtest", "compare"):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "backtest":
        asyncio.run(run_backtest())
    else:
        compare()


if __name__ == "__main__":
    main()
