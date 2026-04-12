"""StrategyValidator — comprehensive multi-dimensional strategy validation.

Never trust a single backtest. This module runs strategies through multiple
validation protocols to expose survivorship bias, regime dependency, and
commission sensitivity before any strategy gets called "profitable".

Validation Protocols:
    1. multi_interval   — Same TF, multiple time windows (1mo → 3yr)
    2. multi_timeframe  — Same interval, multiple TFs (5m → 1d)
    3. regime_test      — Separate bull/bear/sideways performance
    4. commission_sweep  — Test with 0%, 0.02%, 0.04%, 0.1% commission
    5. full_validation  — All of the above combined

Usage:
    from src.backtest.validator import StrategyValidator

    validator = StrategyValidator(
        strategy_factory=lambda tf: MyStrategy(timeframe=tf),
        symbol="BTCUSDT",
        indicators=["ema_9", "ema_21"],
    )
    report = await validator.run("full_validation")
    report.print_summary()
    print(report.verdict)  # PASS / FAIL / MARGINAL

Why this exists:
    BB+RSI showed +7.19% on a 2yr bullish window but was NEGATIVE across
    most intervals and timeframes when tested properly. A single backtest
    window is survivorship bias. This module prevents that mistake.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from src.backtest.protocols import (
    ProtocolResult,
    run_crash_stress,
    run_monte_carlo,
    run_smoke,
    run_spot_check,
    run_tier_lite,
    run_tier_standard,
)
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy
from src.utils.logger import get_logger

log = get_logger("validator")


# ── Configuration ────────────────────────────────────────────────────────────

# Default intervals: (label, days_back)
DEFAULT_INTERVALS = [
    ("1 month", 30),
    ("3 months", 90),
    ("6 months", 180),
    ("1 year", 365),
    ("2 years", 730),
    ("3 years", 1095),
]

# Default timeframes to test
DEFAULT_TIMEFRAMES = ["5m", "15m", "30m", "1h"]

# Commission levels for sensitivity test
COMMISSION_LEVELS = [
    ("Zero", 0.0),
    ("Low (0.02%)", 0.0002),
    ("Binance Maker (0.04%)", 0.0004),
    ("High (0.1%)", 0.001),
]

# Realistic base config
DEFAULT_CONFIG = BacktestConfig(
    initial_capital=10_000.0,
    commission_pct=0.0004,
    slippage_pct=0.0002,
    risk_per_trade=0.01,
    max_notional_pct=2.0,
)

# Verdict thresholds
MIN_TRADES_FOR_SIGNIFICANCE = 10
MIN_PROFITABLE_INTERVALS = 0.5   # 50% of intervals must be profitable
MIN_PROFITABLE_TIMEFRAMES = 0.4  # 40% of TFs must be profitable
MIN_PROFIT_FACTOR = 1.1
MAX_ACCEPTABLE_DRAWDOWN = 30.0   # percent


# ── Data Types ───────────────────────────────────────────────────────────────

@dataclass
class CellResult:
    """Result for one (timeframe, interval) combination."""
    timeframe: str
    interval_label: str
    interval_days: int
    candles: int
    trades: int
    total_return_pct: float
    annualized_return_pct: float
    win_rate_pct: float
    profit_factor: float
    sharpe: float
    max_drawdown_pct: float
    total_commission: float
    buy_hold_return_pct: float
    beats_buy_hold: bool
    statistically_significant: bool  # enough trades to judge

    @property
    def profitable(self) -> bool:
        return self.total_return_pct > 0 and self.statistically_significant


@dataclass
class RegimeResult:
    """Performance in a specific market regime."""
    regime: str  # "bull", "bear", "sideways"
    period: str  # date range description
    days: int
    bh_return_pct: float
    strategy_return_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float


@dataclass
class CommissionResult:
    """Performance at a specific commission level."""
    commission_label: str
    commission_pct: float
    total_return_pct: float
    trades: int
    total_commission: float
    net_profit: float
    breakeven_commission: float | None  # max commission where strategy is still profitable


@dataclass
class ValidationReport:
    """Complete validation report with verdict."""
    strategy_name: str
    symbol: str
    timestamp: str

    # Protocol results
    multi_interval: list[CellResult] = field(default_factory=list)
    multi_timeframe: list[CellResult] = field(default_factory=list)
    regime_results: list[RegimeResult] = field(default_factory=list)
    commission_results: list[CommissionResult] = field(default_factory=list)

    # Aggregated scores
    intervals_profitable: int = 0
    intervals_tested: int = 0
    timeframes_profitable: int = 0
    timeframes_tested: int = 0
    avg_profit_factor: float = 0.0
    worst_drawdown: float = 0.0
    total_trades_all: int = 0
    beats_bh_count: int = 0
    beats_bh_total: int = 0

    @property
    def verdict(self) -> str:
        """PASS / MARGINAL / FAIL based on multi-dimensional results."""
        if self.intervals_tested == 0:
            return "FAIL — no data"

        pct_intervals_profitable = (
            self.intervals_profitable / self.intervals_tested
            if self.intervals_tested > 0 else 0
        )
        pct_tf_profitable = (
            self.timeframes_profitable / self.timeframes_tested
            if self.timeframes_tested > 0 else 0
        )

        fails = []
        if pct_intervals_profitable < MIN_PROFITABLE_INTERVALS:
            fails.append(
                f"Only {self.intervals_profitable}/{self.intervals_tested} "
                f"intervals profitable ({pct_intervals_profitable:.0%} < {MIN_PROFITABLE_INTERVALS:.0%})"
            )
        if pct_tf_profitable < MIN_PROFITABLE_TIMEFRAMES:
            fails.append(
                f"Only {self.timeframes_profitable}/{self.timeframes_tested} "
                f"timeframes profitable ({pct_tf_profitable:.0%} < {MIN_PROFITABLE_TIMEFRAMES:.0%})"
            )
        if self.avg_profit_factor < MIN_PROFIT_FACTOR and self.total_trades_all > 30:
            fails.append(f"Avg PF {self.avg_profit_factor:.3f} < {MIN_PROFIT_FACTOR}")
        if self.worst_drawdown > MAX_ACCEPTABLE_DRAWDOWN:
            fails.append(f"Worst DD {self.worst_drawdown:.1f}% > {MAX_ACCEPTABLE_DRAWDOWN}%")
        if self.total_trades_all < MIN_TRADES_FOR_SIGNIFICANCE:
            fails.append(f"Only {self.total_trades_all} total trades — insufficient data")

        if len(fails) == 0:
            return "PASS"
        elif len(fails) <= 2:
            return f"MARGINAL — {'; '.join(fails)}"
        else:
            return f"FAIL — {'; '.join(fails)}"

    def print_summary(self) -> None:
        """Print a formatted summary report."""
        print(f"\n{'=' * 120}")
        print(f"  STRATEGY VALIDATION REPORT: {self.strategy_name}")
        print(f"  Symbol: {self.symbol} | Generated: {self.timestamp}")
        print(f"{'=' * 120}")

        # ── Multi-interval results ───────────────────────────────────
        if self.multi_interval:
            print(f"\n{'━' * 120}")
            print("  PROTOCOL 1: MULTI-INTERVAL TEST")
            print(f"{'━' * 120}")
            self._print_cell_table(self.multi_interval)

        # ── Multi-timeframe results ──────────────────────────────────
        if self.multi_timeframe:
            print(f"\n{'━' * 120}")
            print("  PROTOCOL 2: MULTI-TIMEFRAME TEST")
            print(f"{'━' * 120}")
            self._print_cell_table(self.multi_timeframe)

        # ── Regime results ───────────────────────────────────────────
        if self.regime_results:
            print(f"\n{'━' * 120}")
            print("  PROTOCOL 3: REGIME TEST")
            print(f"{'━' * 120}")
            print(f"  {'Regime':<10} {'Period':<30} {'Days':>5} {'B&H%':>8} {'Strat%':>8} {'Trades':>7} {'WR%':>7} {'PF':>7}")
            print(f"  {'─'*10} {'─'*30} {'─'*5} {'─'*8} {'─'*8} {'─'*7} {'─'*7} {'─'*7}")
            for r in self.regime_results:
                print(
                    f"  {r.regime:<10} {r.period:<30} {r.days:>5} "
                    f"{r.bh_return_pct:>7.2f}% {r.strategy_return_pct:>7.2f}% "
                    f"{r.trades:>7} {r.win_rate_pct:>6.1f}% {r.profit_factor:>7.3f}"
                )

        # ── Commission sweep ─────────────────────────────────────────
        if self.commission_results:
            print(f"\n{'━' * 120}")
            print("  PROTOCOL 4: COMMISSION SENSITIVITY")
            print(f"{'━' * 120}")
            print(f"  {'Level':<25} {'Comm%':>7} {'Return%':>9} {'Trades':>7} {'Commission$':>12} {'Net Profit$':>12}")
            print(f"  {'─'*25} {'─'*7} {'─'*9} {'─'*7} {'─'*12} {'─'*12}")
            for c in self.commission_results:
                print(
                    f"  {c.commission_label:<25} {c.commission_pct*100:>6.3f}% "
                    f"{c.total_return_pct:>8.2f}% {c.trades:>7} "
                    f"${c.total_commission:>11.2f} ${c.net_profit:>11.2f}"
                )
            # Find breakeven
            profitable = [c for c in self.commission_results if c.total_return_pct > 0]
            unprofitable = [c for c in self.commission_results if c.total_return_pct <= 0]
            if profitable and unprofitable:
                be = max(c.commission_pct for c in profitable)
                print(f"\n  Breakeven commission: ~{be*100:.3f}%")
            elif not profitable:
                print(f"\n  Strategy is unprofitable at ALL commission levels!")
            else:
                print(f"\n  Strategy is profitable even at highest commission tested.")

        # ── Verdict ──────────────────────────────────────────────────
        print(f"\n{'━' * 120}")
        print("  SCORECARD")
        print(f"{'━' * 120}")
        print(f"  Intervals profitable:   {self.intervals_profitable}/{self.intervals_tested}")
        print(f"  Timeframes profitable:  {self.timeframes_profitable}/{self.timeframes_tested}")
        print(f"  Avg Profit Factor:      {self.avg_profit_factor:.3f}")
        print(f"  Worst Max Drawdown:     {self.worst_drawdown:.2f}%")
        print(f"  Total trades (all):     {self.total_trades_all}")
        print(f"  Beats Buy & Hold:       {self.beats_bh_count}/{self.beats_bh_total}")

        v = self.verdict
        marker = "PASS" if v.startswith("PASS") else ("MARGINAL" if v.startswith("MARGINAL") else "FAIL")
        print(f"\n  VERDICT: [{marker}] {v}")
        print(f"{'=' * 120}\n")

    @staticmethod
    def _print_cell_table(cells: list[CellResult]) -> None:
        print(
            f"  {'TF':<5} {'Interval':<12} {'Candles':>8} {'Trades':>7} │ "
            f"{'Return%':>9} {'Ann%':>8} {'WR%':>6} {'PF':>7} {'Sharpe':>7} "
            f"{'MaxDD%':>7} {'Comm$':>9} {'B&H%':>8} │ {'Status':<15}"
        )
        print(f"  {'─'*5} {'─'*12} {'─'*8} {'─'*7}─┼─{'─'*9} {'─'*8} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*9} {'─'*8}─┼─{'─'*15}")

        for c in cells:
            sig = "" if c.statistically_significant else " (few trades)"
            bh_flag = "BEAT B&H" if c.beats_buy_hold else ""
            profit_flag = "+" if c.profitable else "-"
            status = f"{profit_flag}{sig}{' ' + bh_flag if bh_flag else ''}"

            print(
                f"  {c.timeframe:<5} {c.interval_label:<12} {c.candles:>8,} {c.trades:>7} │ "
                f"{c.total_return_pct:>8.2f}% {c.annualized_return_pct:>7.1f}% "
                f"{c.win_rate_pct:>5.1f}% {c.profit_factor:>7.3f} {c.sharpe:>7.3f} "
                f"{c.max_drawdown_pct:>6.2f}% ${c.total_commission:>8.2f} "
                f"{c.buy_hold_return_pct:>7.2f}% │ {status:<15}"
            )


# ── Strategy Factory Type ────────────────────────────────────────────────────

StrategyFactory = Callable[[str], BaseStrategy]  # Takes timeframe, returns strategy


# ── Validator ────────────────────────────────────────────────────────────────

class StrategyValidator:
    """Comprehensive strategy validation across multiple dimensions.

    Args:
        strategy_factory: Callable that takes a timeframe string and returns
                         a fresh strategy instance. Called once per test run.
        symbol: Trading pair to test on (e.g., "BTCUSDT")
        indicators: List of indicator names for the backtest engine
        config: Base backtest config (commission/slippage/sizing)
        intervals: List of (label, days_back) tuples
        timeframes: List of timeframe strings
        end_date: End date for all tests (default: today)
    """

    def __init__(
        self,
        strategy_factory: StrategyFactory,
        symbol: str,
        indicators: list[str],
        config: BacktestConfig | None = None,
        intervals: list[tuple[str, int]] | None = None,
        timeframes: list[str] | None = None,
        end_date: str | None = None,
    ):
        self.strategy_factory = strategy_factory
        self.symbol = symbol
        self.indicators = indicators
        self.config = config or DEFAULT_CONFIG
        self.intervals = intervals or DEFAULT_INTERVALS
        self.timeframes = timeframes or DEFAULT_TIMEFRAMES
        self.end_date = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._end_dt = datetime.strptime(self.end_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        self._downloader: BinanceDownloader | None = None
        self._data_cache: dict[str, pd.DataFrame] = {}

    async def _get_downloader(self) -> BinanceDownloader:
        if self._downloader is None:
            self._downloader = BinanceDownloader()
        return self._downloader

    async def _close(self) -> None:
        if self._downloader:
            await self._downloader.close()
            self._downloader = None

    async def _download(
        self, tf: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Download data with caching."""
        key = f"{self.symbol}_{tf}_{start_date}_{end_date}"
        if key not in self._data_cache:
            dl = await self._get_downloader()
            self._data_cache[key] = await dl.download(
                symbol=self.symbol,
                timeframe=tf,
                start_date=start_date,
                end_date=end_date,
            )
        return self._data_cache[key].copy()

    def _run_one(
        self, tf: str, data: pd.DataFrame, config: BacktestConfig | None = None
    ) -> BacktestResult:
        """Run a single backtest."""
        cfg = config or self.config
        engine = BacktestEngine(config=cfg)
        strategy = self.strategy_factory(tf)
        return engine.run(
            strategy=strategy,
            data=data,
            symbol=self.symbol,
            timeframe=tf,
            indicators=self.indicators,
        )

    def _to_cell(
        self,
        tf: str,
        interval_label: str,
        interval_days: int,
        data: pd.DataFrame,
        result: BacktestResult,
    ) -> CellResult:
        """Convert a BacktestResult into a CellResult."""
        m = result.metrics
        n_trades = m.get("total_trades", 0)
        total_ret = m.get("total_return_pct", 0)

        years = interval_days / 365.25
        if total_ret > -100 and years > 0:
            ann_ret = ((1 + total_ret / 100) ** (1 / years) - 1) * 100
        else:
            ann_ret = total_ret

        bh_ret = (
            (data["close"].iloc[-1] / data["close"].iloc[0] - 1) * 100
            if len(data) > 1 else 0
        )

        return CellResult(
            timeframe=tf,
            interval_label=interval_label,
            interval_days=interval_days,
            candles=len(data),
            trades=n_trades,
            total_return_pct=round(total_ret, 2),
            annualized_return_pct=round(ann_ret, 1),
            win_rate_pct=round(m.get("win_rate_pct", 0), 1),
            profit_factor=round(m.get("profit_factor", 0), 3),
            sharpe=round(m.get("sharpe", 0), 3),
            max_drawdown_pct=round(m.get("max_drawdown_pct", 0), 2),
            total_commission=round(m.get("total_commission", 0), 2),
            buy_hold_return_pct=round(bh_ret, 2),
            beats_buy_hold=total_ret > bh_ret and n_trades >= MIN_TRADES_FOR_SIGNIFICANCE,
            statistically_significant=n_trades >= MIN_TRADES_FOR_SIGNIFICANCE,
        )

    # ── Protocol 1: Multi-Interval ───────────────────────────────────

    async def _run_multi_interval(self, tf: str) -> list[CellResult]:
        """Run strategy on one TF across all time intervals."""
        cells = []
        for label, days in self.intervals:
            start_dt = self._end_dt - timedelta(days=days)
            start_date = start_dt.strftime("%Y-%m-%d")

            try:
                data = await self._download(tf, start_date, self.end_date)
                if len(data) < 50:
                    log.warning("insufficient_data", tf=tf, interval=label, rows=len(data))
                    continue
                result = self._run_one(tf, data)
                cells.append(self._to_cell(tf, label, days, data, result))
                log.info(
                    "interval_done", tf=tf, interval=label,
                    trades=result.metrics.get("total_trades", 0),
                    ret=round(result.metrics.get("total_return_pct", 0), 2),
                )
            except Exception as e:
                log.error("interval_error", tf=tf, interval=label, error=str(e))

        return cells

    # ── Protocol 2: Multi-Timeframe ──────────────────────────────────

    async def _run_multi_timeframe(self, interval_days: int, interval_label: str) -> list[CellResult]:
        """Run strategy across all TFs on one time interval."""
        cells = []
        start_dt = self._end_dt - timedelta(days=interval_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        for tf in self.timeframes:
            try:
                data = await self._download(tf, start_date, self.end_date)
                if len(data) < 50:
                    log.warning("insufficient_data", tf=tf, interval=interval_label, rows=len(data))
                    continue
                result = self._run_one(tf, data)
                cells.append(self._to_cell(tf, interval_label, interval_days, data, result))
            except Exception as e:
                log.error("tf_error", tf=tf, interval=interval_label, error=str(e))

        return cells

    # ── Protocol 3: Regime Test ──────────────────────────────────────

    async def _run_regime_test(self, tf: str = "1h") -> list[RegimeResult]:
        """Identify bull/bear/sideways regimes and test in each.

        Uses rolling 30-day returns on daily data to classify regimes:
          bull:     >+15% in 30 days
          bear:     <-15% in 30 days
          sideways: between -15% and +15%
        """
        # Download max data on the target TF
        max_days = max(d for _, d in self.intervals)
        start_dt = self._end_dt - timedelta(days=max_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        try:
            data = await self._download(tf, start_date, self.end_date)
        except Exception as e:
            log.error("regime_download_error", error=str(e))
            return []

        if len(data) < 100:
            return []

        # Convert to daily to identify regimes
        df = data.copy()
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")
        daily = df.set_index("dt")["close"].resample("D").last().dropna()

        if len(daily) < 60:
            return []

        # 30-day rolling return
        rolling_ret = daily.pct_change(30) * 100

        # Classify each day
        regimes = []
        for dt, ret in rolling_ret.dropna().items():
            if ret > 15:
                regimes.append((dt, "bull"))
            elif ret < -15:
                regimes.append((dt, "bear"))
            else:
                regimes.append((dt, "sideways"))

        # Find contiguous regime blocks
        regime_blocks = []
        if regimes:
            current_regime = regimes[0][1]
            block_start = regimes[0][0]
            for dt, regime in regimes[1:]:
                if regime != current_regime:
                    regime_blocks.append((current_regime, block_start, dt))
                    current_regime = regime
                    block_start = dt
            regime_blocks.append((current_regime, block_start, regimes[-1][0]))

        # Filter blocks that are at least 14 days long
        regime_blocks = [
            (r, s, e) for r, s, e in regime_blocks
            if (e - s).days >= 14
        ]

        # Pick the longest block of each regime type
        results = []
        for regime_type in ["bull", "bear", "sideways"]:
            blocks = [b for b in regime_blocks if b[0] == regime_type]
            if not blocks:
                continue
            # Take the longest block
            longest = max(blocks, key=lambda b: (b[2] - b[1]).days)
            regime, start, end = longest

            # Filter data to this regime's date range
            start_ts = int(start.timestamp() * 1000)
            end_ts = int(end.timestamp() * 1000)
            regime_data = data[
                (data["timestamp"] >= start_ts) & (data["timestamp"] <= end_ts)
            ].copy()

            if len(regime_data) < 30:
                continue

            result = self._run_one(tf, regime_data)
            m = result.metrics
            bh_ret = (
                (regime_data["close"].iloc[-1] / regime_data["close"].iloc[0] - 1) * 100
                if len(regime_data) > 1 else 0
            )
            days = (end - start).days

            results.append(RegimeResult(
                regime=regime_type,
                period=f"{start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}",
                days=days,
                bh_return_pct=round(bh_ret, 2),
                strategy_return_pct=round(m.get("total_return_pct", 0), 2),
                trades=m.get("total_trades", 0),
                win_rate_pct=round(m.get("win_rate_pct", 0), 1),
                profit_factor=round(m.get("profit_factor", 0), 3),
            ))

        return results

    # ── Protocol 4: Commission Sensitivity ───────────────────────────

    async def _run_commission_sweep(self, tf: str = "1h", days: int = 365) -> list[CommissionResult]:
        """Test strategy at different commission levels."""
        start_dt = self._end_dt - timedelta(days=days)
        start_date = start_dt.strftime("%Y-%m-%d")

        try:
            data = await self._download(tf, start_date, self.end_date)
        except Exception as e:
            log.error("commission_download_error", error=str(e))
            return []

        if len(data) < 50:
            return []

        results = []
        for label, comm_pct in COMMISSION_LEVELS:
            cfg = BacktestConfig(
                initial_capital=self.config.initial_capital,
                commission_pct=comm_pct,
                slippage_pct=self.config.slippage_pct,
                risk_per_trade=self.config.risk_per_trade,
                max_notional_pct=self.config.max_notional_pct,
            )
            result = self._run_one(tf, data.copy(), config=cfg)
            m = result.metrics

            results.append(CommissionResult(
                commission_label=label,
                commission_pct=comm_pct,
                total_return_pct=round(m.get("total_return_pct", 0), 2),
                trades=m.get("total_trades", 0),
                total_commission=round(m.get("total_commission", 0), 2),
                net_profit=round(m.get("total_return", 0), 2),
                breakeven_commission=None,
            ))

        return results

    # ── Main Entry Points ────────────────────────────────────────────

    async def run(self, protocol: str = "full_validation") -> ValidationReport:
        """Run a validation protocol and return a report.

        Protocols:
            multi_interval   — Test one TF (1h) across all time intervals
            multi_timeframe  — Test one interval (1yr) across all TFs
            regime_test      — Test in bull/bear/sideways on 1h
            commission_sweep — Test commission sensitivity on 1h
            full_validation  — All of the above
        """
        strategy_name = self.strategy_factory("1h").name
        report = ValidationReport(
            strategy_name=strategy_name,
            symbol=self.symbol,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )

        try:
            if protocol in ("multi_interval", "full_validation"):
                print(f"\n  Running multi-interval test on all timeframes...")
                all_cells = []
                for tf in self.timeframes:
                    print(f"    TF={tf}...", end=" ", flush=True)
                    cells = await self._run_multi_interval(tf)
                    all_cells.extend(cells)
                    profitable = sum(1 for c in cells if c.profitable)
                    print(f"{len(cells)} intervals, {profitable} profitable")
                report.multi_interval = all_cells

            if protocol in ("multi_timeframe", "full_validation"):
                print(f"\n  Running multi-timeframe test on all intervals...")
                all_cells = []
                for label, days in self.intervals:
                    print(f"    Interval={label}...", end=" ", flush=True)
                    cells = await self._run_multi_timeframe(days, label)
                    all_cells.extend(cells)
                    profitable = sum(1 for c in cells if c.profitable)
                    print(f"{len(cells)} TFs, {profitable} profitable")
                report.multi_timeframe = all_cells

            if protocol in ("regime_test", "full_validation"):
                print(f"\n  Running regime test...")
                report.regime_results = await self._run_regime_test()
                for r in report.regime_results:
                    print(f"    {r.regime}: {r.strategy_return_pct:.2f}% ({r.trades} trades)")

            if protocol in ("commission_sweep", "full_validation"):
                print(f"\n  Running commission sweep...")
                report.commission_results = await self._run_commission_sweep()
                for c in report.commission_results:
                    print(f"    {c.commission_label}: {c.total_return_pct:.2f}%")

            # ── Compute aggregate scores ─────────────────────────────
            all_cells = report.multi_interval or report.multi_timeframe
            if not all_cells and report.multi_timeframe:
                all_cells = report.multi_timeframe

            # Combine both for scoring (deduplicate by tf+interval)
            seen = set()
            unique_cells = []
            for c in (report.multi_interval + report.multi_timeframe):
                key = (c.timeframe, c.interval_label)
                if key not in seen:
                    seen.add(key)
                    unique_cells.append(c)

            if unique_cells:
                significant = [c for c in unique_cells if c.statistically_significant]
                report.intervals_tested = len(significant)
                report.intervals_profitable = sum(1 for c in significant if c.profitable)

                # Per-timeframe: profitable if majority of intervals are profitable
                tf_scores = {}
                for c in significant:
                    tf_scores.setdefault(c.timeframe, []).append(c.profitable)
                report.timeframes_tested = len(tf_scores)
                report.timeframes_profitable = sum(
                    1 for cells in tf_scores.values()
                    if sum(cells) > len(cells) / 2
                )

                pfs = [c.profit_factor for c in significant if c.profit_factor > 0]
                report.avg_profit_factor = sum(pfs) / len(pfs) if pfs else 0

                dds = [c.max_drawdown_pct for c in unique_cells]
                report.worst_drawdown = max(dds) if dds else 0

                report.total_trades_all = sum(c.trades for c in unique_cells)
                report.beats_bh_total = len(significant)
                report.beats_bh_count = sum(1 for c in significant if c.beats_buy_hold)

        finally:
            await self._close()

        return report

    # ── Convenience: Quick single-protocol runs ──────────────────────

    async def run_multi_interval(self) -> ValidationReport:
        return await self.run("multi_interval")

    async def run_multi_timeframe(self) -> ValidationReport:
        return await self.run("multi_timeframe")

    async def run_regime_test(self) -> ValidationReport:
        return await self.run("regime_test")

    async def run_commission_sweep(self) -> ValidationReport:
        return await self.run("commission_sweep")

    async def run_full(self) -> ValidationReport:
        return await self.run("full_validation")

    # ── Tier-based dispatch (Phase B) ───────────────────────────────

    async def run_tier(
        self, tier: str = "lite", tf: str = "1h",
    ) -> tuple[ValidationReport | None, list[ProtocolResult]]:
        """Run a tier of protocols and return both legacy report + protocol results.

        Tiers:
            lite     — smoke + spot_check (< 1 min)
            standard — lite + existing 5 protocols + monte_carlo + crash_stress
            intense  — standard + walk-forward (future)
            research — all (future)

        Returns:
            (ValidationReport or None, list[ProtocolResult])
        """
        protocol_results: list[ProtocolResult] = []
        report: ValidationReport | None = None

        if tier == "lite":
            protocol_results = await run_tier_lite(
                self.strategy_factory, self.symbol,
                self.indicators, self.config, tf=tf,
            )

        elif tier == "standard":
            # Phase B protocols
            protocol_results = await run_tier_lite(
                self.strategy_factory, self.symbol,
                self.indicators, self.config, tf=tf,
            )

            # Run existing full_validation for the 4 legacy protocols
            print("\n  [STANDARD] Running legacy validation protocols...")
            report = await self.run("full_validation")

            # Run a quick backtest to get trades for Monte Carlo
            try:
                end_dt = self._end_dt
                start_dt = end_dt - timedelta(days=365)
                data = await self._download(
                    tf, start_dt.strftime("%Y-%m-%d"),
                    end_dt.strftime("%Y-%m-%d"),
                )
                if len(data) >= 50:
                    result = self._run_one(tf, data)
                    if result.trades and len(result.trades) >= 5:
                        print("\n  [STANDARD] Running Monte Carlo (10K sims)...")
                        mc = await run_monte_carlo(result.trades)
                        protocol_results.append(mc)
                        print(f"    {mc.summary}")
            except Exception as e:
                log.warning("monte_carlo_skipped", error=str(e))

            # Crash stress
            print("\n  [STANDARD] Running crash stress test...")
            crash = await run_crash_stress(
                self.strategy_factory, self.symbol,
                self.indicators, self.config, tf=tf,
            )
            protocol_results.append(crash)
            print(f"    {crash.summary}")

        elif tier in ("intense", "research"):
            # For now, run standard tier — intense/research protocols
            # will be added in future phases
            print(f"  Note: '{tier}' tier not fully implemented, running standard")
            return await self.run_tier("standard", tf=tf)

        else:
            print(f"  Unknown tier: {tier}")

        return report, protocol_results

    async def run_single_protocol(
        self, protocol: str, tf: str = "1h",
    ) -> ProtocolResult | ValidationReport:
        """Run a single protocol by name.

        Handles both new protocols (return ProtocolResult) and
        legacy protocols (return ValidationReport).
        """
        # New Phase B protocols
        if protocol == "smoke":
            return await run_smoke(
                self.strategy_factory, self.symbol,
                self.indicators, self.config, tf=tf,
            )
        elif protocol == "spot_check":
            return await run_spot_check(
                self.strategy_factory, self.symbol,
                self.indicators, self.config, tf=tf,
            )
        elif protocol == "crash_stress":
            return await run_crash_stress(
                self.strategy_factory, self.symbol,
                self.indicators, self.config, tf=tf,
            )
        # Legacy protocols
        elif protocol in (
            "multi_interval", "multi_timeframe", "regime_test",
            "commission_sweep", "full_validation",
        ):
            return await self.run(protocol)
        else:
            return ProtocolResult(
                protocol=protocol, passed=False,
                summary=f"Unknown protocol: {protocol}",
            )
