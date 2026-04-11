"""Walk-forward validation — tests strategy generalization on unseen data.

Splits historical data into in-sample (training) and out-of-sample (validation)
windows. The strategy must achieve Sharpe > threshold on out-of-sample data to pass.

Usage:
    validator = WalkForwardValidator()
    result = validator.run(EMAScalpStrategy, "ema_crossover", data)
    print(f"Passed: {result.passed}")
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from src.strategies.base import BaseStrategy
from src.utils.logger import get_logger

log = get_logger("walk_forward")


@dataclass
class WalkForwardConfig:
    in_sample_pct: float = 0.5     # 50% of data for in-sample
    min_sharpe: float = 1.0        # out-of-sample Sharpe threshold
    backtest_config: BacktestConfig = field(default_factory=BacktestConfig)


@dataclass
class SplitResult:
    split_idx: int
    in_sample: BacktestResult
    out_of_sample: BacktestResult
    is_sharpe: float               # in-sample Sharpe
    oos_sharpe: float              # out-of-sample Sharpe
    passed: bool


@dataclass
class WalkForwardResult:
    splits: list[SplitResult] = field(default_factory=list)
    passed: bool = False
    summary: dict = field(default_factory=dict)


class WalkForwardValidator:
    """Walk-forward validation engine."""

    def __init__(self, config: WalkForwardConfig | None = None):
        self.config = config or WalkForwardConfig()

    def run(
        self,
        strategy_cls: type[BaseStrategy],
        config_name: str,
        data: pd.DataFrame,
        symbol: str = "BTCUSDT",
        timeframe: str = "1m",
        indicators: list[str] | None = None,
    ) -> WalkForwardResult:
        """Run walk-forward validation.

        Args:
            strategy_cls: Strategy class (will be instantiated fresh for each split)
            config_name: Config section name in strategies.toml
            data: Full historical DataFrame
            symbol: Symbol name
            timeframe: Timeframe string
            indicators: Indicators to compute

        Returns:
            WalkForwardResult with per-split results and pass/fail
        """
        cfg = self.config
        n = len(data)
        split_point = int(n * cfg.in_sample_pct)

        if split_point < 100 or (n - split_point) < 100:
            log.warning("insufficient_data_for_walk_forward", total=n, split=split_point)
            return WalkForwardResult(summary={"error": "insufficient data"})

        in_sample_data = data.iloc[:split_point].reset_index(drop=True)
        out_of_sample_data = data.iloc[split_point:].reset_index(drop=True)

        log.info(
            "walk_forward_start",
            total_candles=n,
            in_sample=len(in_sample_data),
            out_of_sample=len(out_of_sample_data),
            min_sharpe=cfg.min_sharpe,
        )

        engine = BacktestEngine(config=cfg.backtest_config)

        # Run in-sample
        is_strategy = strategy_cls.from_config(config_name)
        is_result = engine.run(is_strategy, in_sample_data, symbol, timeframe, indicators)

        # Run out-of-sample (fresh strategy instance — no state leakage)
        oos_strategy = strategy_cls.from_config(config_name)
        oos_result = engine.run(oos_strategy, out_of_sample_data, symbol, timeframe, indicators)

        is_sharpe = is_result.metrics.get("sharpe", 0)
        oos_sharpe = oos_result.metrics.get("sharpe", 0)
        split_passed = oos_sharpe >= cfg.min_sharpe

        split = SplitResult(
            split_idx=0,
            in_sample=is_result,
            out_of_sample=oos_result,
            is_sharpe=is_sharpe,
            oos_sharpe=oos_sharpe,
            passed=split_passed,
        )

        log.info(
            "walk_forward_split",
            split=0,
            is_sharpe=round(is_sharpe, 3),
            oos_sharpe=round(oos_sharpe, 3),
            is_trades=is_result.metrics.get("total_trades", 0),
            oos_trades=oos_result.metrics.get("total_trades", 0),
            passed=split_passed,
        )

        overall_passed = split_passed

        summary = {
            "total_candles": n,
            "in_sample_candles": len(in_sample_data),
            "out_of_sample_candles": len(out_of_sample_data),
            "in_sample_sharpe": round(is_sharpe, 3),
            "out_of_sample_sharpe": round(oos_sharpe, 3),
            "in_sample_trades": is_result.metrics.get("total_trades", 0),
            "out_of_sample_trades": oos_result.metrics.get("total_trades", 0),
            "in_sample_return_pct": is_result.metrics.get("total_return_pct", 0),
            "out_of_sample_return_pct": oos_result.metrics.get("total_return_pct", 0),
            "min_sharpe_threshold": cfg.min_sharpe,
            "passed": overall_passed,
        }

        log.info("walk_forward_complete", **summary)

        return WalkForwardResult(
            splits=[split],
            passed=overall_passed,
            summary=summary,
        )
