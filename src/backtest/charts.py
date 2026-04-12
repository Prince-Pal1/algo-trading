"""Plotly chart renderers for backtest results.

Each function returns a plotly.graph_objects.Figure or an HTML string.
Phase A: equity_curve + drawdown.
Phase C: monthly heatmap, trade distribution, rolling metrics,
         monte carlo fan, trade timeline, win/loss streak,
         param sensitivity heatmap, strategy comparison radar.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd


# ---------------------------------------------------------------------------
# Phase A charts (unchanged)
# ---------------------------------------------------------------------------

def equity_curve(
    equity: pd.Series,
    title: str = "Equity Curve",
    benchmark: pd.Series | None = None,
) -> go.Figure:
    """Equity curve with optional benchmark overlay."""
    idx = _to_datetime_index(equity)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=idx, y=equity.values,
        name="Strategy",
        line=dict(color="#2196F3", width=2),
        hovertemplate="$%{y:,.2f}<extra>Strategy</extra>",
    ))

    if benchmark is not None:
        bm_idx = _to_datetime_index(benchmark)
        fig.add_trace(go.Scatter(
            x=bm_idx, y=benchmark.values,
            name="Buy & Hold",
            line=dict(color="#9E9E9E", width=1, dash="dot"),
            hovertemplate="$%{y:,.2f}<extra>Buy & Hold</extra>",
        ))

    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Equity ($)",
        template="plotly_white",
        hovermode="x unified",
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def drawdown_underwater(equity: pd.Series, title: str = "Drawdown") -> go.Figure:
    """Underwater drawdown chart — shows drawdown % from peak over time."""
    idx = _to_datetime_index(equity)
    peak = equity.cummax()
    dd_pct = ((equity - peak) / peak) * 100

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=idx, y=dd_pct.values,
        fill="tozeroy",
        name="Drawdown",
        line=dict(color="#F44336", width=1),
        fillcolor="rgba(244, 67, 54, 0.3)",
        hovertemplate="%{y:.2f}%<extra>Drawdown</extra>",
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Drawdown (%)",
        template="plotly_white",
        hovermode="x unified",
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def equity_and_drawdown(
    equity: pd.Series,
    title: str = "Strategy Performance",
    benchmark: pd.Series | None = None,
) -> go.Figure:
    """Combined equity + drawdown in a dual-panel chart (shared x-axis)."""
    idx = _to_datetime_index(equity)
    peak = equity.cummax()
    dd_pct = ((equity - peak) / peak) * 100

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.7, 0.3],
        subplot_titles=("Equity Curve", "Drawdown"),
    )

    fig.add_trace(go.Scatter(
        x=idx, y=equity.values,
        name="Strategy",
        line=dict(color="#2196F3", width=2),
        hovertemplate="$%{y:,.2f}<extra>Strategy</extra>",
    ), row=1, col=1)

    if benchmark is not None:
        bm_idx = _to_datetime_index(benchmark)
        fig.add_trace(go.Scatter(
            x=bm_idx, y=benchmark.values,
            name="Buy & Hold",
            line=dict(color="#9E9E9E", width=1, dash="dot"),
            hovertemplate="$%{y:,.2f}<extra>Buy & Hold</extra>",
        ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=idx, y=dd_pct.values,
        fill="tozeroy",
        name="Drawdown",
        line=dict(color="#F44336", width=1),
        fillcolor="rgba(244, 67, 54, 0.3)",
        hovertemplate="%{y:.2f}%<extra>Drawdown</extra>",
        showlegend=False,
    ), row=2, col=1)

    fig.update_layout(
        title=title,
        template="plotly_white",
        hovermode="x unified",
        height=600,
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
        margin=dict(l=60, r=20, t=60, b=40),
    )
    fig.update_yaxes(title_text="Equity ($)", row=1, col=1)
    fig.update_yaxes(title_text="DD (%)", row=2, col=1)

    return fig


# ---------------------------------------------------------------------------
# Phase C charts
# ---------------------------------------------------------------------------

def monthly_heatmap(equity: pd.Series, title: str = "Monthly Returns") -> go.Figure:
    """Green/red calendar heatmap of monthly returns."""
    eq = equity.copy()
    eq.index = _to_datetime_index(eq)
    monthly = eq.resample("ME").last().pct_change().dropna() * 100

    # Build year × month matrix
    years = sorted(monthly.index.year.unique())
    months = list(range(1, 13))
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    z = []
    for year in years:
        row = []
        for month in months:
            vals = monthly[(monthly.index.year == year) & (monthly.index.month == month)]
            row.append(round(float(vals.iloc[0]), 2) if len(vals) > 0 else None)
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=month_names,
        y=[str(y) for y in years],
        colorscale=[[0, "#c62828"], [0.5, "#ffffff"], [1, "#2e7d32"]],
        zmid=0,
        text=[[f"{v:.1f}%" if v is not None else "" for v in row] for row in z],
        texttemplate="%{text}",
        hovertemplate="Year: %{y}<br>Month: %{x}<br>Return: %{z:.2f}%<extra></extra>",
        colorbar=dict(title="Return %"),
    ))
    fig.update_layout(
        title=title,
        template="plotly_white",
        height=max(250, 60 * len(years) + 100),
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def trade_distribution(trades: list[Any], title: str = "Trade P&L Distribution") -> go.Figure:
    """Histogram of trade P&L values."""
    if not trades:
        return _empty_figure(title)

    pnls = [float(t.pnl) for t in trades]
    colors = ["#2e7d32" if p > 0 else "#c62828" for p in pnls]

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=pnls,
        nbinsx=30,
        marker_color="#2196F3",
        hovertemplate="P&L: $%{x:.2f}<br>Count: %{y}<extra></extra>",
    ))
    # Add zero line
    fig.add_vline(x=0, line_dash="dash", line_color="red", line_width=1)

    fig.update_layout(
        title=title,
        xaxis_title="P&L ($)",
        yaxis_title="Frequency",
        template="plotly_white",
        height=350,
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def rolling_sharpe(
    equity: pd.Series,
    window: int = 60,
    title: str = "Rolling Sharpe Ratio",
) -> go.Figure:
    """Rolling Sharpe ratio over time with threshold lines."""
    eq = equity.copy()
    eq.index = _to_datetime_index(eq)
    daily = eq.resample("D").last().dropna()
    returns = daily.pct_change().dropna()

    if len(returns) < window:
        return _empty_figure(title)

    ann = np.sqrt(365)
    roll_mean = returns.rolling(window).mean()
    roll_std = returns.rolling(window).std()
    roll_sr = (roll_mean / roll_std * ann).dropna()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=roll_sr.index, y=roll_sr.values,
        name=f"Sharpe ({window}d)",
        line=dict(color="#2196F3", width=2),
    ))
    # Threshold lines
    fig.add_hline(y=0, line_dash="dash", line_color="#999", line_width=1)
    fig.add_hline(y=1, line_dash="dot", line_color="#4CAF50", line_width=1,
                  annotation_text="1.0")
    fig.add_hline(y=2, line_dash="dot", line_color="#2e7d32", line_width=1,
                  annotation_text="2.0")

    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Sharpe Ratio",
        template="plotly_white",
        height=350,
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def rolling_metrics(
    equity: pd.Series,
    window: int = 60,
    title: str = "Rolling Metrics",
) -> go.Figure:
    """Multi-panel: rolling Sharpe, win rate proxy, and volatility."""
    eq = equity.copy()
    eq.index = _to_datetime_index(eq)
    daily = eq.resample("D").last().dropna()
    returns = daily.pct_change().dropna()

    if len(returns) < window:
        return _empty_figure(title)

    ann = np.sqrt(365)
    roll_mean = returns.rolling(window).mean()
    roll_std = returns.rolling(window).std()
    roll_sr = (roll_mean / roll_std * ann).dropna()
    roll_vol = (roll_std * ann * 100).dropna()  # annualized vol %
    roll_wr = (returns.rolling(window).apply(lambda x: (x > 0).mean()) * 100).dropna()

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.34, 0.33, 0.33],
        subplot_titles=("Rolling Sharpe", "Rolling Win Rate (%)", "Rolling Volatility (%)"),
    )

    fig.add_trace(go.Scatter(
        x=roll_sr.index, y=roll_sr.values,
        name="Sharpe", line=dict(color="#2196F3", width=1.5),
    ), row=1, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="#999", row=1, col=1)

    fig.add_trace(go.Scatter(
        x=roll_wr.index, y=roll_wr.values,
        name="Win Rate", line=dict(color="#FF9800", width=1.5),
    ), row=2, col=1)
    fig.add_hline(y=50, line_dash="dash", line_color="#999", row=2, col=1)

    fig.add_trace(go.Scatter(
        x=roll_vol.index, y=roll_vol.values,
        name="Volatility", line=dict(color="#9C27B0", width=1.5),
    ), row=3, col=1)

    fig.update_layout(
        title=title,
        template="plotly_white",
        height=650,
        showlegend=False,
        margin=dict(l=60, r=20, t=60, b=40),
    )
    return fig


def monte_carlo_fan(
    trade_pnls: list[float],
    n_sims: int = 1000,
    rng_seed: int = 42,
    title: str = "Monte Carlo Simulation",
) -> go.Figure:
    """Fan chart with confidence bands from trade shuffling."""
    if len(trade_pnls) < 5:
        return _empty_figure(title)

    rng = np.random.default_rng(rng_seed)
    arr = np.array(trade_pnls)
    n_trades = len(arr)

    # Run simulations — cumulative equity paths
    paths = np.zeros((n_sims, n_trades + 1))
    for i in range(n_sims):
        shuffled = rng.permutation(arr)
        paths[i, 1:] = np.cumsum(shuffled)

    # Compute percentiles
    p5 = np.percentile(paths, 5, axis=0)
    p25 = np.percentile(paths, 25, axis=0)
    p50 = np.percentile(paths, 50, axis=0)
    p75 = np.percentile(paths, 75, axis=0)
    p95 = np.percentile(paths, 95, axis=0)
    x = list(range(n_trades + 1))

    fig = go.Figure()

    # 5-95 band
    fig.add_trace(go.Scatter(
        x=x, y=p95, mode="lines", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=p5, mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor="rgba(33, 150, 243, 0.1)",
        name="5th-95th pctl",
    ))

    # 25-75 band
    fig.add_trace(go.Scatter(
        x=x, y=p75, mode="lines", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=p25, mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor="rgba(33, 150, 243, 0.25)",
        name="25th-75th pctl",
    ))

    # Median
    fig.add_trace(go.Scatter(
        x=x, y=p50,
        name="Median",
        line=dict(color="#2196F3", width=2),
    ))

    # Actual path
    actual = np.concatenate([[0], np.cumsum(arr)])
    fig.add_trace(go.Scatter(
        x=x, y=actual,
        name="Actual",
        line=dict(color="#F44336", width=2, dash="dot"),
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Trade #",
        yaxis_title="Cumulative P&L ($)",
        template="plotly_white",
        height=400,
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def trade_timeline(
    trades: list[Any],
    ohlcv: pd.DataFrame | None = None,
    title: str = "Trade Entry/Exit Timeline",
) -> go.Figure:
    """Scatter of trade entry/exit prices over time, colored by P&L."""
    if not trades:
        return _empty_figure(title)

    entries = [float(t.entry_price) for t in trades]
    exits = [float(t.exit_price) for t in trades]
    pnls = [float(t.pnl) for t in trades]
    entry_idx = list(range(len(trades)))
    colors = ["#2e7d32" if p > 0 else "#c62828" for p in pnls]

    fig = go.Figure()

    # Entry markers
    fig.add_trace(go.Scatter(
        x=entry_idx, y=entries,
        mode="markers",
        name="Entry",
        marker=dict(symbol="triangle-up", size=10, color=colors),
        hovertemplate="Trade %{x}<br>Entry: $%{y:,.2f}<extra>Entry</extra>",
    ))

    # Exit markers
    fig.add_trace(go.Scatter(
        x=entry_idx, y=exits,
        mode="markers",
        name="Exit",
        marker=dict(symbol="triangle-down", size=10, color=colors),
        hovertemplate="Trade %{x}<br>Exit: $%{y:,.2f}<extra>Exit</extra>",
    ))

    # Lines connecting entry to exit
    for i, (ent, ext, pnl) in enumerate(zip(entries, exits, pnls)):
        c = "#2e7d32" if pnl > 0 else "#c62828"
        fig.add_trace(go.Scatter(
            x=[i, i], y=[ent, ext],
            mode="lines",
            line=dict(color=c, width=1),
            showlegend=False,
            hoverinfo="skip",
        ))

    fig.update_layout(
        title=title,
        xaxis_title="Trade #",
        yaxis_title="Price ($)",
        template="plotly_white",
        height=400,
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def win_loss_streak(trades: list[Any], title: str = "Win/Loss Streaks") -> go.Figure:
    """Bar chart of consecutive win/loss streaks."""
    if not trades:
        return _empty_figure(title)

    pnls = [float(t.pnl) for t in trades]
    streaks = []
    current = 0
    is_win = pnls[0] > 0

    for p in pnls:
        if (p > 0) == is_win:
            current += 1
        else:
            streaks.append((current, is_win))
            is_win = p > 0
            current = 1
    streaks.append((current, is_win))

    lengths = [s[0] if s[1] else -s[0] for s in streaks]
    colors = ["#2e7d32" if l > 0 else "#c62828" for l in lengths]
    labels = [f"{'Win' if l > 0 else 'Loss'} x{abs(l)}" for l in lengths]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=list(range(len(lengths))),
        y=lengths,
        marker_color=colors,
        text=labels,
        textposition="outside",
        hovertemplate="Streak: %{text}<extra></extra>",
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Streak #",
        yaxis_title="Streak Length (+ win, - loss)",
        template="plotly_white",
        height=350,
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def param_sensitivity_heatmap(
    grid: list,
    param1_name: str,
    param2_name: str,
    metric: str = "sharpe",
    title: str | None = None,
) -> go.Figure:
    """2D heatmap of parameter sensitivity results.

    Args:
        grid: List of SensitivityCell objects with param1_value, param2_value, and metric attrs.
        param1_name/param2_name: Parameter axis labels.
        metric: Which metric to display (sharpe, total_return_pct, max_drawdown_pct).
    """
    if not grid:
        return _empty_figure(title or "Parameter Sensitivity")

    p1_vals = sorted(set(c.param1_value for c in grid))
    p2_vals = sorted(set(c.param2_value for c in grid))

    # Build matrix
    lookup = {(c.param1_value, c.param2_value): getattr(c, metric, 0) for c in grid}
    z = []
    for p2 in p2_vals:
        row = [lookup.get((p1, p2), 0) for p1 in p1_vals]
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=[str(v) for v in p1_vals],
        y=[str(v) for v in p2_vals],
        colorscale=[[0, "#c62828"], [0.5, "#ffffff"], [1, "#2e7d32"]],
        zmid=0,
        text=[[f"{v:.3f}" for v in row] for row in z],
        texttemplate="%{text}",
        hovertemplate=f"{param1_name}: %{{x}}<br>{param2_name}: %{{y}}<br>{metric}: %{{z:.3f}}<extra></extra>",
        colorbar=dict(title=metric.replace("_", " ").title()),
    ))

    fig.update_layout(
        title=title or f"Parameter Sensitivity — {metric}",
        xaxis_title=param1_name,
        yaxis_title=param2_name,
        template="plotly_white",
        height=400,
        margin=dict(l=80, r=20, t=50, b=60),
    )
    return fig


def strategy_comparison_radar(
    strategies: dict[str, dict],
    title: str = "Strategy Comparison",
) -> go.Figure:
    """Spider/radar chart comparing multiple strategies.

    Args:
        strategies: {name: {metric_name: value, ...}, ...}
            Metrics should be normalized 0-1 or comparable scales.
    """
    if not strategies:
        return _empty_figure(title)

    # Use common metric names
    metric_keys = ["sharpe", "sortino", "win_rate_pct", "profit_factor", "calmar"]
    metric_labels = ["Sharpe", "Sortino", "Win Rate", "Profit Factor", "Calmar"]

    fig = go.Figure()
    colors = ["#2196F3", "#F44336", "#4CAF50", "#FF9800", "#9C27B0"]

    for i, (name, metrics) in enumerate(strategies.items()):
        values = [metrics.get(k, 0) for k in metric_keys]
        # Close the polygon
        values_closed = values + [values[0]]
        labels_closed = metric_labels + [metric_labels[0]]

        fig.add_trace(go.Scatterpolar(
            r=values_closed,
            theta=labels_closed,
            fill="toself",
            name=name,
            line=dict(color=colors[i % len(colors)]),
            opacity=0.6,
        ))

    fig.update_layout(
        title=title,
        polar=dict(radialaxis=dict(visible=True)),
        template="plotly_white",
        height=450,
        margin=dict(l=60, r=60, t=60, b=40),
    )
    return fig


def regime_performance(
    regime_results: list,
    title: str = "Regime Performance",
) -> go.Figure:
    """Grouped bar chart of strategy vs buy-hold per regime."""
    if not regime_results:
        return _empty_figure(title)

    regimes = [r.regime for r in regime_results]
    strat_ret = [r.strategy_return_pct for r in regime_results]
    bh_ret = [r.bh_return_pct for r in regime_results]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=regimes, y=strat_ret,
        name="Strategy",
        marker_color="#2196F3",
    ))
    fig.add_trace(go.Bar(
        x=regimes, y=bh_ret,
        name="Buy & Hold",
        marker_color="#9E9E9E",
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Regime",
        yaxis_title="Return (%)",
        barmode="group",
        template="plotly_white",
        height=350,
        margin=dict(l=60, r=20, t=50, b=40),
    )
    return fig


def crash_stress_chart(
    events: list,
    title: str = "Crash Stress Test Results",
) -> go.Figure:
    """Horizontal bar chart comparing strategy drawdown vs BTC drop per crash."""
    if not events:
        return _empty_figure(title)

    names = [e.event_name for e in events]
    strat_dd = [-e.max_drawdown_pct for e in events]  # negative for display
    btc_drop = [e.btc_drop_pct for e in events]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=names, x=btc_drop,
        name="BTC Drop",
        orientation="h",
        marker_color="#F44336",
    ))
    fig.add_trace(go.Bar(
        y=names, x=strat_dd,
        name="Strategy DD",
        orientation="h",
        marker_color="#2196F3",
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Drawdown (%)",
        barmode="group",
        template="plotly_white",
        height=max(300, 60 * len(events) + 100),
        margin=dict(l=160, r=20, t=50, b=40),
    )
    return fig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_datetime_index(series: pd.Series) -> pd.DatetimeIndex:
    """Convert Unix ms index to DatetimeIndex if needed."""
    try:
        if not isinstance(series.index, pd.DatetimeIndex):
            return pd.to_datetime(series.index, unit="ms")
        return series.index
    except Exception:
        return series.index


def _empty_figure(title: str) -> go.Figure:
    """Return an empty figure with a message."""
    fig = go.Figure()
    fig.add_annotation(
        text="Insufficient data",
        xref="paper", yref="paper",
        x=0.5, y=0.5, showarrow=False,
        font=dict(size=16, color="#999"),
    )
    fig.update_layout(
        title=title,
        template="plotly_white",
        height=300,
    )
    return fig
