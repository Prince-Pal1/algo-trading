"""Single source of truth for all backtest metrics.

Invariant #2: DB = Chart = JSON.  Every consumer reads from the same
`compute_metrics()` dict — no separate recalculation anywhere.

Extracted from BacktestEngine._compute_metrics() + new advanced metrics
(PSR, DSR, MinBTL, Calmar, buy-and-hold, bootstrap).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sp_stats  # type: ignore[import-untyped]


# ---------------------------------------------------------------------------
# Core metrics — THE one function.  Engine, ResultStore, and charts all use it.
# ---------------------------------------------------------------------------

def compute_metrics(
    equity_curve: pd.Series,
    trades: list[Any],
    initial_capital: float = 10_000.0,
    ohlcv: pd.DataFrame | None = None,
) -> dict:
    """Compute all backtest metrics from an equity curve and trade list.

    Args:
        equity_curve: Series indexed by timestamp (Unix ms) with equity values.
        trades: List of Trade dataclass instances (need .pnl, .commission attrs).
        initial_capital: Starting capital.
        ohlcv: Original OHLCV data (optional, used for buy-and-hold benchmark).

    Returns:
        Dict with every metric key.  This exact dict is stored in the DB,
        dumped to JSON, and passed to the chart/report renderer.
    """
    if len(equity_curve) < 2:
        return _empty_metrics(initial_capital)

    final_equity = float(equity_curve.iloc[-1])
    total_return = final_equity - initial_capital
    total_return_pct = (total_return / initial_capital) * 100

    # --- Daily returns (for Sharpe / Sortino) ---
    daily_returns = _to_daily_returns(equity_curve)

    avg_return = daily_returns.mean()
    std_return = daily_returns.std()
    downside = daily_returns[daily_returns < 0]
    downside_std = downside.std() if len(downside) > 0 else 0.0

    ann = np.sqrt(365)  # crypto trades 24/7
    sharpe = float((avg_return / std_return * ann) if std_return > 0 else 0.0)
    sortino = float((avg_return / downside_std * ann) if downside_std > 0 else 0.0)

    # --- Drawdown ---
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak
    max_drawdown = float(abs(drawdown.min()))

    # --- Calmar = annualised return / max drawdown ---
    n_days = max(len(daily_returns), 1)
    ann_return_pct = total_return_pct * (365 / n_days)
    calmar = float(ann_return_pct / (max_drawdown * 100)) if max_drawdown > 0 else 0.0

    # --- Trade stats ---
    n_trades = len(trades)
    if n_trades > 0:
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]
        win_rate = len(winners) / n_trades * 100
        gross_profit = sum(t.pnl for t in winners) if winners else 0.0
        gross_loss = abs(sum(t.pnl for t in losers)) if losers else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_win = gross_profit / len(winners) if winners else 0.0
        avg_loss = gross_loss / len(losers) if losers else 0.0
        avg_win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_win_loss_ratio = 0.0

    total_commission = float(sum(t.commission for t in trades))

    # --- Buy-and-hold benchmark ---
    bh_return_pct = _buy_hold_return(ohlcv) if ohlcv is not None else None

    # --- Probabilistic Sharpe Ratio ---
    psr = probabilistic_sharpe(daily_returns) if len(daily_returns) > 2 else None

    return {
        "initial_capital": initial_capital,
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return_pct, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "calmar": round(calmar, 3),
        "total_trades": n_trades,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 3),
        "avg_win_loss_ratio": round(avg_win_loss_ratio, 3),
        "total_commission": round(total_commission, 2),
        "buy_hold_return_pct": round(bh_return_pct, 2) if bh_return_pct is not None else None,
        "psr": round(psr, 4) if psr is not None else None,
    }


# ---------------------------------------------------------------------------
# Advanced statistical metrics
# ---------------------------------------------------------------------------

def probabilistic_sharpe(
    returns: pd.Series,
    sr_benchmark: float = 0.0,
) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado, 2012).

    Returns probability (0-1) that the true Sharpe exceeds sr_benchmark.
    """
    n = len(returns)
    if n < 3:
        return 0.0
    sr = returns.mean() / returns.std() if returns.std() > 0 else 0.0
    skew = float(returns.skew())
    kurt = float(returns.kurtosis())  # excess kurtosis

    sr_std = math.sqrt(
        (1 + 0.5 * sr**2 - skew * sr + ((kurt - 1) / 4) * sr**2) / (n - 1)
    )
    if sr_std <= 0:
        return 0.0
    z = (sr - sr_benchmark) / sr_std
    return float(sp_stats.norm.cdf(z))


def deflated_sharpe(
    returns: pd.Series,
    n_trials: int,
    sr_variance: float | None = None,
) -> float:
    """Deflated Sharpe Ratio — corrects for multiple testing.

    Args:
        returns: Strategy returns.
        n_trials: Number of strategies / parameter combos tried.
        sr_variance: Variance of Sharpe ratios across trials (estimated if None).
    """
    if n_trials < 1 or len(returns) < 3:
        return 0.0
    sr = returns.mean() / returns.std() if returns.std() > 0 else 0.0
    if sr_variance is None:
        sr_variance = 1.0  # conservative default
    # Expected max Sharpe under null (Euler-Mascheroni adjustment)
    euler = 0.5772
    expected_max_sr = math.sqrt(2 * math.log(n_trials)) * (
        1 - euler / (2 * math.log(n_trials))
    ) * math.sqrt(sr_variance)
    return probabilistic_sharpe(returns, sr_benchmark=expected_max_sr)


def minimum_backtest_length(
    sharpe: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    confidence: float = 0.95,
) -> float:
    """Minimum Backtest Length (MinBTL) in observations.

    Returns the minimum number of return observations needed for the
    Sharpe ratio to be statistically significant at `confidence` level.
    """
    if sharpe <= 0:
        return float("inf")
    z = sp_stats.norm.ppf(confidence)
    excess_kurt = kurtosis - 3
    return (
        1
        + (1 - skew * sharpe + ((excess_kurt) / 4) * sharpe**2)
        * (z / sharpe) ** 2
    )


def bootstrap_confidence(
    trade_pnls: list[float],
    n_sims: int = 10_000,
    percentiles: tuple[float, ...] = (5, 25, 50, 75, 95),
    rng_seed: int = 42,
) -> dict[str, float]:
    """Bootstrap confidence intervals on total P&L.

    Resamples trades with replacement and returns percentile bands.
    """
    if not trade_pnls:
        return {f"p{p}": 0.0 for p in percentiles}
    rng = np.random.default_rng(rng_seed)
    arr = np.array(trade_pnls)
    sims = np.array([
        rng.choice(arr, size=len(arr), replace=True).sum()
        for _ in range(n_sims)
    ])
    result = {}
    for p in percentiles:
        result[f"p{p}"] = float(np.percentile(sims, p))
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_daily_returns(equity_curve: pd.Series) -> pd.Series:
    """Convert timestamp-indexed equity curve to daily returns."""
    try:
        eq = equity_curve.copy()
        eq.index = pd.to_datetime(eq.index, unit="ms")
        daily = eq.resample("D").last().dropna()
        return daily.pct_change().dropna()
    except Exception:
        return equity_curve.pct_change().dropna()


def _buy_hold_return(ohlcv: pd.DataFrame) -> float:
    """Calculate buy-and-hold return % from OHLCV data."""
    if ohlcv is None or len(ohlcv) < 2:
        return 0.0
    first_close = ohlcv.iloc[0]["close"]
    last_close = ohlcv.iloc[-1]["close"]
    return ((last_close - first_close) / first_close) * 100


def _empty_metrics(initial_capital: float) -> dict:
    return {
        "initial_capital": initial_capital,
        "final_equity": initial_capital,
        "total_return": 0.0,
        "total_return_pct": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown_pct": 0.0,
        "calmar": 0.0,
        "total_trades": 0,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
        "avg_win_loss_ratio": 0.0,
        "total_commission": 0.0,
        "buy_hold_return_pct": None,
        "psr": None,
    }
