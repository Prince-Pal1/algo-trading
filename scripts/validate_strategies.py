"""Run both SMA Crossover and BB+RSI through the StrategyValidator.

Usage:
    python -m scripts.validate_strategies
    python -m scripts.validate_strategies --strategy sma
    python -m scripts.validate_strategies --strategy bbrsi
    python -m scripts.validate_strategies --protocol multi_interval
    python -m scripts.validate_strategies --strategy bbrsi --catalog-id bb_rsi_mr
"""

from __future__ import annotations

import asyncio
import sys

import pandas as pd

from src.backtest.validator import StrategyValidator
from src.strategies.base import BaseStrategy
from src.strategies.day_trading.bb_rsi_mr import BBRSIMeanRevStrategy
from src.utils.types import Signal, SignalAction


# ── Strategy Factories ───────────────────────────────────────────────────────

class SMACrossoverStrategy(BaseStrategy):
    """SMA(10)/SMA(20) crossover — long only."""

    def __init__(self, timeframe: str = "1h"):
        super().__init__(name="sma_crossover", markets=["BTCUSDT"], timeframe=timeframe)

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
            return Signal(symbol=symbol, action=SignalAction.LONG, confidence=1.0,
                          strategy_name=self.name, timeframe=timeframe)
        if prev_sma10 >= prev_sma20 and sma10 < sma20 and self._position == "LONG":
            return Signal(symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                          strategy_name=self.name, timeframe=timeframe)
        return None


def sma_factory(tf: str) -> BaseStrategy:
    return SMACrossoverStrategy(timeframe=tf)


def bbrsi_factory(tf: str) -> BaseStrategy:
    return BBRSIMeanRevStrategy(
        name="bb_rsi_mr", markets=["BTCUSDT"], timeframe=tf,
        bb_period=20, rsi_period=14, adx_period=14, atr_period=14,
        rsi_overbought=75.0, rsi_oversold=25.0, adx_threshold=20.0,
        sl_atr_mult=3.0, max_hold_bars=48, cooldown_bars=5,
    )


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Parse args
    strategy = "both"
    protocol = "full_validation"
    catalog_id: str | None = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--strategy" and i + 1 < len(args):
            strategy = args[i + 1]
        elif arg == "--protocol" and i + 1 < len(args):
            protocol = args[i + 1]
        elif arg == "--catalog-id" and i + 1 < len(args):
            catalog_id = args[i + 1]

    strategies_to_run = []
    if strategy in ("sma", "both"):
        strategies_to_run.append(("SMA Crossover", sma_factory, ["sma_10", "sma_20"]))
    if strategy in ("bbrsi", "both"):
        strategies_to_run.append(("BB+RSI MR", bbrsi_factory, ["bbands_20", "rsi_14", "adx_14", "atr_14"]))

    reports = []
    for name, factory, indicators in strategies_to_run:
        print(f"\n{'#' * 120}")
        print(f"  VALIDATING: {name}")
        print(f"{'#' * 120}")

        validator = StrategyValidator(
            strategy_factory=factory,
            symbol="BTCUSDT",
            indicators=indicators,
        )
        report = await validator.run(protocol)
        report.print_summary()
        reports.append((name, report))

        # Store validation result in catalog if --catalog-id was provided
        if catalog_id:
            from src.research.catalog import StrategyCatalog
            cat = StrategyCatalog()
            cat.load()
            row_id = cat.link_validation(catalog_id, report)
            print(f"  >> Validation stored in catalog (strategy={catalog_id}, row={row_id})")

    # Side-by-side verdict if both were run
    if len(reports) == 2:
        print(f"\n{'=' * 120}")
        print("  FINAL COMPARISON")
        print(f"{'=' * 120}")
        for name, report in reports:
            v = report.verdict
            marker = "PASS" if v.startswith("PASS") else ("MARGINAL" if v.startswith("MARGINAL") else "FAIL")
            print(f"  {name:<20} [{marker}] {v}")
        print(f"{'=' * 120}")


if __name__ == "__main__":
    asyncio.run(main())
