"""QuantStats bridge — generates tearsheets from BacktestResult equity curves.

One-liner fallback when custom HTML reports aren't needed.

Usage:
    from src.backtest.quantstats_bridge import generate_tearsheet
    generate_tearsheet(equity_curve, output_path="reports/tearsheet.html")
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def generate_tearsheet(
    equity: pd.Series,
    output_path: str = "reports/tearsheet.html",
    title: str = "Strategy Tearsheet",
    benchmark: str | None = None,
) -> Path:
    """Generate a QuantStats HTML tearsheet from an equity curve.

    Args:
        equity: Equity curve Series (indexed by Unix ms or datetime).
        output_path: Where to save the HTML file.
        title: Report title.
        benchmark: Optional benchmark ticker (e.g., "BTC-USD") for comparison.

    Returns:
        Path to the generated HTML file.
    """
    import quantstats as qs  # lazy import — heavy dependency

    # Convert to daily returns (QuantStats expects a returns Series)
    eq = equity.copy()
    if not isinstance(eq.index, pd.DatetimeIndex):
        eq.index = pd.to_datetime(eq.index, unit="ms")
    daily = eq.resample("D").last().dropna()
    returns = daily.pct_change().dropna()
    returns.index = returns.index.tz_localize(None)  # QuantStats prefers tz-naive

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if benchmark:
        qs.reports.html(
            returns, benchmark=benchmark,
            output=str(out), title=title,
            download_filename=str(out),
        )
    else:
        qs.reports.html(
            returns,
            output=str(out), title=title,
            download_filename=str(out),
        )

    return out


def get_stats(equity: pd.Series) -> dict:
    """Get a dict of QuantStats metrics from an equity curve."""
    import quantstats as qs

    eq = equity.copy()
    if not isinstance(eq.index, pd.DatetimeIndex):
        eq.index = pd.to_datetime(eq.index, unit="ms")
    daily = eq.resample("D").last().dropna()
    returns = daily.pct_change().dropna()
    returns.index = returns.index.tz_localize(None)

    # Extract key metrics
    return {
        "cagr": float(qs.stats.cagr(returns)),
        "sharpe": float(qs.stats.sharpe(returns)),
        "sortino": float(qs.stats.sortino(returns)),
        "max_drawdown": float(qs.stats.max_drawdown(returns)),
        "calmar": float(qs.stats.calmar(returns)),
        "volatility": float(qs.stats.volatility(returns)),
        "var": float(qs.stats.var(returns)),
        "cvar": float(qs.stats.cvar(returns)),
    }
