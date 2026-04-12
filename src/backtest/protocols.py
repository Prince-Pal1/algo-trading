"""New test protocols — Phase B additions to the validation system.

Each protocol is a standalone async function that uses the existing
BacktestEngine (sync) for execution and BinanceDownloader (async) for data.

Protocols defined here:
    smoke           — 30-day sanity check, single TF
    spot_check      — 3-month check that strategy generates trades
    psr_check       — Probabilistic Sharpe Ratio on prior result (instant)
    monte_carlo     — 10K trade shuffles → confidence intervals
    param_sensitivity_2d — Vary 2 params ±20% on a 5x5 grid
    crash_stress    — Test on 6 known crypto crash events
    deflated_sharpe — DSR correcting for N strategies tested

Usage from the validator's tier system:
    result = await run_smoke(factory, symbol, indicators, config)
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import structlog

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from src.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from src.backtest.metrics import (
    bootstrap_confidence,
    deflated_sharpe as compute_deflated_sharpe,
    probabilistic_sharpe,
)
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy

log = structlog.get_logger("protocols")

StrategyFactory = Callable[[str], BaseStrategy]


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class ProtocolResult:
    """Base result for any protocol."""
    protocol: str
    passed: bool
    summary: str
    runtime_seconds: float = 0.0
    detail: dict = field(default_factory=dict)


@dataclass
class MonteCarloResult(ProtocolResult):
    """Result from Monte Carlo trade shuffling."""
    n_sims: int = 0
    percentiles: dict = field(default_factory=dict)  # {"p5": ..., "p50": ..., "p95": ...}
    prob_profitable: float = 0.0  # fraction of sims with positive P&L


@dataclass
class CrashEventResult:
    """Performance during a single crash event."""
    event_id: str
    event_name: str
    start: str
    end: str
    btc_drop_pct: float
    strategy_return_pct: float
    max_drawdown_pct: float
    trades: int
    survived: bool  # drawdown < 2x BTC drop


@dataclass
class CrashStressResult(ProtocolResult):
    """Result from crash stress testing across all events."""
    events: list[CrashEventResult] = field(default_factory=list)
    events_survived: int = 0
    events_tested: int = 0


@dataclass
class SensitivityCell:
    """One cell in the param sensitivity grid."""
    param1_value: float
    param2_value: float
    sharpe: float
    total_return_pct: float
    max_drawdown_pct: float
    total_trades: int


@dataclass
class SensitivityResult(ProtocolResult):
    """Result from 2D parameter sensitivity analysis."""
    param1_name: str = ""
    param2_name: str = ""
    grid: list[SensitivityCell] = field(default_factory=list)
    base_sharpe: float = 0.0
    sharpe_std: float = 0.0  # std dev of Sharpe across grid
    robust: bool = False  # True if most neighbors have positive Sharpe


# ---------------------------------------------------------------------------
# Helper: download data for a date range
# ---------------------------------------------------------------------------

async def _download_range(
    symbol: str, tf: str, start: str, end: str
) -> pd.DataFrame:
    """Download OHLCV data for a specific date range."""
    dl = BinanceDownloader()
    try:
        data = await dl.download(
            symbol=symbol, timeframe=tf,
            start_date=start, end_date=end,
        )
        return data
    finally:
        await dl.close()


def _run_engine(
    factory: StrategyFactory,
    symbol: str,
    tf: str,
    indicators: list[str],
    data: pd.DataFrame,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run a single backtest — sync call."""
    cfg = config or BacktestConfig(
        initial_capital=10_000.0,
        commission_pct=0.0004,
        slippage_pct=0.0002,
        risk_per_trade=0.01,
        max_notional_pct=2.0,
    )
    engine = BacktestEngine(config=cfg)
    strategy = factory(tf)
    return engine.run(
        strategy=strategy, data=data,
        symbol=symbol, timeframe=tf, indicators=indicators,
    )


# ---------------------------------------------------------------------------
# Protocol: smoke (Tier 1 — LITE)
# ---------------------------------------------------------------------------

async def run_smoke(
    factory: StrategyFactory,
    symbol: str,
    indicators: list[str],
    config: BacktestConfig | None = None,
    tf: str = "1h",
    days: int = 30,
) -> ProtocolResult:
    """Smoke test — recent 30 days, single TF, basic metrics check.

    Passes if: backtest completes without error and produces metrics.
    """
    t0 = time.monotonic()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    try:
        data = await _download_range(
            symbol, tf,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )
        if len(data) < 20:
            return ProtocolResult(
                protocol="smoke", passed=False,
                summary=f"Insufficient data: {len(data)} candles (need >= 20)",
                runtime_seconds=time.monotonic() - t0,
            )

        result = _run_engine(factory, symbol, tf, indicators, data, config)
        m = result.metrics
        elapsed = time.monotonic() - t0

        log.info(
            "smoke_complete", symbol=symbol, tf=tf,
            trades=m.get("total_trades", 0),
            sharpe=m.get("sharpe", 0),
            return_pct=m.get("total_return_pct", 0),
        )

        return ProtocolResult(
            protocol="smoke",
            passed=True,
            summary=(
                f"Smoke OK — {m.get('total_trades', 0)} trades, "
                f"Sharpe {m.get('sharpe', 0):.3f}, "
                f"Return {m.get('total_return_pct', 0):.2f}%"
            ),
            runtime_seconds=elapsed,
            detail=m,
        )
    except Exception as e:
        return ProtocolResult(
            protocol="smoke", passed=False,
            summary=f"Smoke FAILED: {e}",
            runtime_seconds=time.monotonic() - t0,
        )


# ---------------------------------------------------------------------------
# Protocol: spot_check (Tier 1 — LITE)
# ---------------------------------------------------------------------------

async def run_spot_check(
    factory: StrategyFactory,
    symbol: str,
    indicators: list[str],
    config: BacktestConfig | None = None,
    tf: str = "1h",
    days: int = 90,
    min_trades: int = 5,
) -> ProtocolResult:
    """Spot check — 3-month window, verify strategy actually generates trades.

    Passes if: >= min_trades trades generated.
    """
    t0 = time.monotonic()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    try:
        data = await _download_range(
            symbol, tf,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )
        if len(data) < 50:
            return ProtocolResult(
                protocol="spot_check", passed=False,
                summary=f"Insufficient data: {len(data)} candles",
                runtime_seconds=time.monotonic() - t0,
            )

        result = _run_engine(factory, symbol, tf, indicators, data, config)
        m = result.metrics
        n_trades = m.get("total_trades", 0)
        elapsed = time.monotonic() - t0
        passed = n_trades >= min_trades

        return ProtocolResult(
            protocol="spot_check",
            passed=passed,
            summary=(
                f"Spot check {'OK' if passed else 'FAIL'} — "
                f"{n_trades} trades (need >= {min_trades}), "
                f"PF {m.get('profit_factor', 0):.3f}"
            ),
            runtime_seconds=elapsed,
            detail=m,
        )
    except Exception as e:
        return ProtocolResult(
            protocol="spot_check", passed=False,
            summary=f"Spot check FAILED: {e}",
            runtime_seconds=time.monotonic() - t0,
        )


# ---------------------------------------------------------------------------
# Protocol: psr_check (Tier 1 — LITE, instant)
# ---------------------------------------------------------------------------

def run_psr_check(
    metrics: dict,
    equity_curve: pd.Series | None = None,
    threshold: float = 0.95,
) -> ProtocolResult:
    """PSR check — validates statistical significance of a prior result.

    Passes if: PSR >= threshold (default 0.95 = 95% confidence).
    This is SYNC — no data download needed, runs on existing results.
    """
    t0 = time.monotonic()
    psr = metrics.get("psr")

    if psr is None and equity_curve is not None and len(equity_curve) > 2:
        try:
            eq = equity_curve.copy()
            eq.index = pd.to_datetime(eq.index, unit="ms")
            daily = eq.resample("D").last().dropna()
            returns = daily.pct_change().dropna()
            psr = probabilistic_sharpe(returns)
        except Exception:
            psr = None

    if psr is None:
        return ProtocolResult(
            protocol="psr_check", passed=False,
            summary="PSR unavailable — insufficient data",
            runtime_seconds=time.monotonic() - t0,
        )

    passed = psr >= threshold
    return ProtocolResult(
        protocol="psr_check",
        passed=passed,
        summary=(
            f"PSR {'OK' if passed else 'FAIL'} — "
            f"PSR={psr:.4f} ({'≥' if passed else '<'} {threshold})"
        ),
        runtime_seconds=time.monotonic() - t0,
        detail={"psr": psr, "threshold": threshold},
    )


# ---------------------------------------------------------------------------
# Protocol: monte_carlo (Tier 2 — STANDARD)
# ---------------------------------------------------------------------------

async def run_monte_carlo(
    trades: list[Any],
    n_sims: int = 10_000,
    rng_seed: int = 42,
) -> MonteCarloResult:
    """Monte Carlo — shuffle trades 10K times, compute confidence intervals.

    Passes if: >= 60% of simulations are profitable.
    """
    t0 = time.monotonic()

    if len(trades) < 5:
        return MonteCarloResult(
            protocol="monte_carlo", passed=False, n_sims=0,
            summary=f"Too few trades ({len(trades)}) for Monte Carlo",
            runtime_seconds=time.monotonic() - t0,
        )

    trade_pnls = [float(t.pnl) for t in trades]
    ci = bootstrap_confidence(trade_pnls, n_sims=n_sims, rng_seed=rng_seed)

    # Also compute probability of being profitable
    rng = np.random.default_rng(rng_seed)
    arr = np.array(trade_pnls)
    sims_total = np.array([
        rng.choice(arr, size=len(arr), replace=True).sum()
        for _ in range(n_sims)
    ])
    prob_profitable = float(np.mean(sims_total > 0))

    elapsed = time.monotonic() - t0
    passed = prob_profitable >= 0.60

    return MonteCarloResult(
        protocol="monte_carlo",
        passed=passed,
        summary=(
            f"Monte Carlo {'OK' if passed else 'FAIL'} — "
            f"P(profit)={prob_profitable:.1%}, "
            f"median P&L={ci.get('p50', 0):.2f}, "
            f"5th pctl={ci.get('p5', 0):.2f}"
        ),
        runtime_seconds=elapsed,
        detail={"prob_profitable": prob_profitable, **ci},
        n_sims=n_sims,
        percentiles=ci,
        prob_profitable=prob_profitable,
    )


# ---------------------------------------------------------------------------
# Protocol: crash_stress (Tier 2 — STANDARD)
# ---------------------------------------------------------------------------

def _load_crash_events() -> list[dict]:
    """Load crash events from config/crash_events.toml."""
    toml_path = Path("config/crash_events.toml")
    if not toml_path.exists():
        log.warning("crash_events_missing", path=str(toml_path))
        return []
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("crash", [])


async def run_crash_stress(
    factory: StrategyFactory,
    symbol: str,
    indicators: list[str],
    config: BacktestConfig | None = None,
    tf: str = "1h",
    buffer_days: int = 30,
) -> CrashStressResult:
    """Crash stress test — run strategy during 6 known crypto crashes.

    For each crash event, downloads data from (start - buffer_days) to end,
    so the strategy has warm-up candles for indicators.

    Passes if: strategy survives >= 50% of events (drawdown < 2x BTC drop).
    """
    t0 = time.monotonic()
    events = _load_crash_events()

    if not events:
        return CrashStressResult(
            protocol="crash_stress", passed=False,
            summary="No crash events found in config/crash_events.toml",
            runtime_seconds=time.monotonic() - t0,
        )

    event_results: list[CrashEventResult] = []

    for ev in events:
        ev_id = ev["id"]
        ev_start = datetime.strptime(ev["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        ev_end = datetime.strptime(ev["end"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        btc_drop = ev.get("btc_drop_pct", -100)

        # Download with buffer for indicator warm-up
        dl_start = ev_start - timedelta(days=buffer_days)
        dl_end = ev_end + timedelta(days=1)  # inclusive end

        try:
            data = await _download_range(
                symbol, tf,
                dl_start.strftime("%Y-%m-%d"),
                dl_end.strftime("%Y-%m-%d"),
            )
            if len(data) < 30:
                log.warning("crash_insufficient_data", crash_id=ev_id, candles=len(data))
                continue

            result = _run_engine(factory, symbol, tf, indicators, data, config)
            m = result.metrics
            strat_ret = m.get("total_return_pct", 0)
            max_dd = m.get("max_drawdown_pct", 0)
            n_trades = m.get("total_trades", 0)

            # "Survived" = strategy drawdown is less than 2x the BTC drop
            # BTC drop is negative, max_dd is positive
            survived = max_dd < abs(btc_drop) * 2

            event_results.append(CrashEventResult(
                event_id=ev_id,
                event_name=ev.get("name", ev_id),
                start=ev["start"],
                end=ev["end"],
                btc_drop_pct=btc_drop,
                strategy_return_pct=round(strat_ret, 2),
                max_drawdown_pct=round(max_dd, 2),
                trades=n_trades,
                survived=survived,
            ))
            log.info(
                "crash_event_done", crash_id=ev_id,
                strat_ret=round(strat_ret, 2),
                max_dd=round(max_dd, 2),
                survived=survived,
            )

        except Exception as e:
            log.error("crash_event_error", crash_id=ev_id, error=str(e))

    elapsed = time.monotonic() - t0
    n_survived = sum(1 for e in event_results if e.survived)
    n_tested = len(event_results)
    passed = n_tested > 0 and n_survived >= n_tested * 0.5

    return CrashStressResult(
        protocol="crash_stress",
        passed=passed,
        summary=(
            f"Crash stress {'OK' if passed else 'FAIL'} — "
            f"survived {n_survived}/{n_tested} events"
        ),
        runtime_seconds=elapsed,
        events=event_results,
        events_survived=n_survived,
        events_tested=n_tested,
    )


# ---------------------------------------------------------------------------
# Protocol: param_sensitivity_2d (Tier 2 — STANDARD)
# ---------------------------------------------------------------------------

async def run_param_sensitivity_2d(
    factory_with_params: Callable[..., BaseStrategy],
    symbol: str,
    indicators: list[str],
    param1_name: str,
    param1_base: float,
    param2_name: str,
    param2_base: float,
    config: BacktestConfig | None = None,
    tf: str = "1h",
    days: int = 365,
    grid_pcts: tuple[float, ...] = (-0.20, -0.10, 0.0, 0.10, 0.20),
) -> SensitivityResult:
    """2D parameter sensitivity — vary 2 params ±20% on a 5x5 grid.

    Args:
        factory_with_params: Callable(tf, **{param1_name: v1, param2_name: v2}) -> strategy
        param1_name/param2_name: Parameter names to vary
        param1_base/param2_base: Baseline values
        grid_pcts: Percentage offsets to test (default: -20%, -10%, 0%, +10%, +20%)

    Passes if: Sharpe std across grid < 0.5 (stable edge) AND
               >= 60% of grid cells have positive Sharpe.
    """
    t0 = time.monotonic()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    try:
        data = await _download_range(
            symbol, tf,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )
    except Exception as e:
        return SensitivityResult(
            protocol="param_sensitivity_2d", passed=False,
            summary=f"Data download failed: {e}",
            runtime_seconds=time.monotonic() - t0,
        )

    if len(data) < 50:
        return SensitivityResult(
            protocol="param_sensitivity_2d", passed=False,
            summary=f"Insufficient data: {len(data)} candles",
            runtime_seconds=time.monotonic() - t0,
        )

    cfg = config or BacktestConfig(
        initial_capital=10_000.0,
        commission_pct=0.0004,
        slippage_pct=0.0002,
        risk_per_trade=0.01,
        max_notional_pct=2.0,
    )

    grid: list[SensitivityCell] = []
    base_sharpe = 0.0

    for pct1 in grid_pcts:
        v1 = param1_base * (1 + pct1)
        # For integer params (like periods), round
        if isinstance(param1_base, int) or param1_base == int(param1_base):
            v1 = max(1, int(round(v1)))

        for pct2 in grid_pcts:
            v2 = param2_base * (1 + pct2)
            if isinstance(param2_base, int) or param2_base == int(param2_base):
                v2 = max(1, int(round(v2)))

            try:
                strategy = factory_with_params(
                    tf, **{param1_name: v1, param2_name: v2}
                )
                engine = BacktestEngine(config=cfg)
                result = engine.run(
                    strategy=strategy, data=data.copy(),
                    symbol=symbol, timeframe=tf, indicators=indicators,
                )
                m = result.metrics
                cell = SensitivityCell(
                    param1_value=v1,
                    param2_value=v2,
                    sharpe=m.get("sharpe", 0),
                    total_return_pct=m.get("total_return_pct", 0),
                    max_drawdown_pct=m.get("max_drawdown_pct", 0),
                    total_trades=m.get("total_trades", 0),
                )
                grid.append(cell)

                if pct1 == 0.0 and pct2 == 0.0:
                    base_sharpe = cell.sharpe

            except Exception as e:
                log.warning(
                    "sensitivity_cell_error",
                    p1=f"{param1_name}={v1}", p2=f"{param2_name}={v2}",
                    error=str(e),
                )

    elapsed = time.monotonic() - t0

    if not grid:
        return SensitivityResult(
            protocol="param_sensitivity_2d", passed=False,
            summary="All grid cells failed",
            runtime_seconds=elapsed,
        )

    sharpes = [c.sharpe for c in grid]
    sharpe_std = float(np.std(sharpes))
    pct_positive = sum(1 for s in sharpes if s > 0) / len(sharpes)
    robust = sharpe_std < 0.5 and pct_positive >= 0.6
    passed = robust

    return SensitivityResult(
        protocol="param_sensitivity_2d",
        passed=passed,
        summary=(
            f"Sensitivity {'OK' if passed else 'FAIL'} — "
            f"Sharpe std={sharpe_std:.3f}, "
            f"{pct_positive:.0%} cells positive, "
            f"base Sharpe={base_sharpe:.3f}"
        ),
        runtime_seconds=elapsed,
        param1_name=param1_name,
        param2_name=param2_name,
        grid=grid,
        base_sharpe=base_sharpe,
        sharpe_std=sharpe_std,
        robust=robust,
    )


# ---------------------------------------------------------------------------
# Protocol: deflated_sharpe (Tier 2 — STANDARD, instant)
# ---------------------------------------------------------------------------

def run_deflated_sharpe(
    equity_curve: pd.Series,
    n_trials: int,
    threshold: float = 0.95,
) -> ProtocolResult:
    """Deflated Sharpe Ratio — corrects for multiple testing.

    SYNC — no data download needed. Runs on existing equity curve.

    Args:
        equity_curve: Equity curve from backtest.
        n_trials: Number of strategies or parameter combos tested.
        threshold: DSR must exceed this to pass (default 0.95).

    Passes if: DSR >= threshold.
    """
    t0 = time.monotonic()

    if len(equity_curve) < 10:
        return ProtocolResult(
            protocol="deflated_sharpe", passed=False,
            summary="Insufficient equity data for DSR",
            runtime_seconds=time.monotonic() - t0,
        )

    try:
        eq = equity_curve.copy()
        eq.index = pd.to_datetime(eq.index, unit="ms")
        daily = eq.resample("D").last().dropna()
        returns = daily.pct_change().dropna()
        dsr = compute_deflated_sharpe(returns, n_trials=n_trials)
    except Exception as e:
        return ProtocolResult(
            protocol="deflated_sharpe", passed=False,
            summary=f"DSR computation failed: {e}",
            runtime_seconds=time.monotonic() - t0,
        )

    passed = dsr >= threshold
    return ProtocolResult(
        protocol="deflated_sharpe",
        passed=passed,
        summary=(
            f"DSR {'OK' if passed else 'FAIL'} — "
            f"DSR={dsr:.4f} (n_trials={n_trials}, threshold={threshold})"
        ),
        runtime_seconds=time.monotonic() - t0,
        detail={"dsr": dsr, "n_trials": n_trials, "threshold": threshold},
    )


# ---------------------------------------------------------------------------
# Tier runners — convenience functions that group protocols
# ---------------------------------------------------------------------------

async def run_tier_lite(
    factory: StrategyFactory,
    symbol: str,
    indicators: list[str],
    config: BacktestConfig | None = None,
    tf: str = "1h",
) -> list[ProtocolResult]:
    """Tier 1: LITE — smoke + spot_check. < 1 minute."""
    results = []

    print("  [LITE] Running smoke test...")
    smoke = await run_smoke(factory, symbol, indicators, config, tf=tf)
    results.append(smoke)
    print(f"    {smoke.summary}")

    print("  [LITE] Running spot check...")
    spot = await run_spot_check(factory, symbol, indicators, config, tf=tf)
    results.append(spot)
    print(f"    {spot.summary}")

    return results


async def run_tier_standard(
    factory: StrategyFactory,
    symbol: str,
    indicators: list[str],
    config: BacktestConfig | None = None,
    tf: str = "1h",
    trades: list[Any] | None = None,
) -> list[ProtocolResult]:
    """Tier 2: STANDARD — lite + monte_carlo + crash_stress. 1-10 minutes."""
    results = await run_tier_lite(factory, symbol, indicators, config, tf=tf)

    if trades and len(trades) >= 5:
        print("  [STANDARD] Running Monte Carlo (10K sims)...")
        mc = await run_monte_carlo(trades)
        results.append(mc)
        print(f"    {mc.summary}")

    print("  [STANDARD] Running crash stress test...")
    crash = await run_crash_stress(factory, symbol, indicators, config, tf=tf)
    results.append(crash)
    print(f"    {crash.summary}")

    return results
