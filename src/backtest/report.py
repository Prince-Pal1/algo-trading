"""HTML report generator — renders self-contained HTML from metrics + equity.

Invariant #2: Does NOT recompute Sharpe/DD.  Reads them from the metrics dict
that was already computed by compute_metrics() and stored in the DB.
Charts are generated from raw equity/trades data; metric *values* come from
the dict passed in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from src.backtest.charts import (
    crash_stress_chart,
    equity_and_drawdown,
    monte_carlo_fan,
    monthly_heatmap,
    regime_performance,
    rolling_metrics,
    trade_distribution,
    trade_timeline,
    win_loss_streak,
)

_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "config"
_TEMPLATE_FILE = "report_template.html"


def render_report(
    strategy_id: str,
    metrics: dict,
    equity: pd.Series,
    trades: list,
    *,
    run_id: int = 0,
    symbol: str = "",
    timeframe: str = "",
    data_fingerprint: str = "",
    protocol_results: list | None = None,
    regime_results: list | None = None,
    crash_events: list | None = None,
    mc_trade_pnls: list[float] | None = None,
) -> str:
    """Render a self-contained HTML report.

    Args:
        strategy_id: Catalog strategy ID.
        metrics: The SAME dict returned by compute_metrics() and stored in DB.
        equity: Equity curve Series (for chart rendering).
        trades: Trade list (for trade analysis charts).
        run_id: SQLite run ID.
        symbol/timeframe/data_fingerprint: Metadata.
        protocol_results: Optional list of ProtocolResult for validation summary.
        regime_results: Optional list of RegimeResult for regime chart.
        crash_events: Optional list of CrashEventResult for crash stress chart.
        mc_trade_pnls: Optional trade P&L list for Monte Carlo fan chart.

    Returns:
        Self-contained HTML string (includes Plotly JS inline).
    """
    # Build equity + drawdown chart (always present)
    fig = equity_and_drawdown(equity, title=f"{strategy_id} — {symbol} {timeframe}")
    chart_html = fig.to_html(include_plotlyjs=True, full_html=False)

    # Derive date range
    start_date, end_date = _date_range(equity)

    # Build metric cards — values FROM the dict, not recalculated
    metric_cards = _build_metric_cards(metrics)

    # Build extended sections (Phase C)
    extra_sections = _build_extra_sections(
        equity=equity,
        trades=trades,
        regime_results=regime_results,
        crash_events=crash_events,
        mc_trade_pnls=mc_trade_pnls,
        protocol_results=protocol_results,
    )

    # Render template
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=False)
    template = env.get_template(_TEMPLATE_FILE)

    return template.render(
        strategy_id=strategy_id,
        run_id=run_id,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        data_fingerprint=data_fingerprint,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        metric_cards=metric_cards,
        equity_chart_html=chart_html,
        extra_sections=extra_sections,
    )


def render_comparison(
    runs: list[dict],
) -> str:
    """Side-by-side HTML comparison of multiple runs.

    Args:
        runs: List of dicts with keys: strategy_id, metrics, equity, run_id.

    Returns:
        Self-contained HTML string.
    """
    from src.backtest.charts import strategy_comparison_radar

    strategies = {}
    for r in runs:
        name = f"{r.get('strategy_id', '?')} (#{r.get('run_id', '?')})"
        strategies[name] = r.get("metrics", {})

    radar = strategy_comparison_radar(strategies)
    radar_html = radar.to_html(include_plotlyjs=True, full_html=False)

    html_parts = [
        "<!DOCTYPE html><html><head><title>Strategy Comparison</title>",
        "<style>body{font-family:sans-serif;max-width:1200px;margin:0 auto;padding:20px}",
        "table{border-collapse:collapse;width:100%}th,td{padding:8px 12px;border:1px solid #ddd;text-align:right}",
        "th{background:#f5f5f5;text-align:left}.positive{color:#2e7d32}.negative{color:#c62828}</style>",
        "</head><body>",
        "<h1>Strategy Comparison</h1>",
        radar_html,
        "<h2>Metrics</h2><table><tr><th>Metric</th>",
    ]

    for r in runs:
        html_parts.append(f"<th>{r.get('strategy_id', '?')} #{r.get('run_id', '?')}</th>")
    html_parts.append("</tr>")

    metric_keys = [
        ("Total Return %", "total_return_pct"),
        ("Sharpe", "sharpe"),
        ("Sortino", "sortino"),
        ("Max Drawdown %", "max_drawdown_pct"),
        ("Calmar", "calmar"),
        ("Win Rate %", "win_rate_pct"),
        ("Profit Factor", "profit_factor"),
        ("Total Trades", "total_trades"),
        ("PSR", "psr"),
    ]
    for label, key in metric_keys:
        html_parts.append(f"<tr><td><strong>{label}</strong></td>")
        for r in runs:
            val = r.get("metrics", {}).get(key)
            if val is not None:
                cls = ""
                if key in ("total_return_pct", "sharpe", "sortino", "calmar"):
                    cls = ' class="positive"' if val > 0 else ' class="negative"'
                html_parts.append(f"<td{cls}>{val}</td>")
            else:
                html_parts.append("<td>-</td>")
        html_parts.append("</tr>")

    html_parts.append("</table></body></html>")
    return "\n".join(html_parts)


# ---------------------------------------------------------------------------
# Extended section builders (Phase C)
# ---------------------------------------------------------------------------

def _build_extra_sections(
    equity: pd.Series,
    trades: list,
    regime_results: list | None = None,
    crash_events: list | None = None,
    mc_trade_pnls: list[float] | None = None,
    protocol_results: list | None = None,
) -> list[str]:
    """Build optional HTML sections for the report."""
    sections: list[str] = []

    # Convert equity to daily for length checks
    try:
        eq = equity.copy()
        eq.index = pd.to_datetime(eq.index, unit="ms")
        daily = eq.resample("D").last().dropna()
        n_daily = len(daily)
    except Exception:
        n_daily = 0

    # Monthly heatmap (needs > 60 days)
    if n_daily > 60:
        try:
            fig = monthly_heatmap(equity)
            sections.append(
                '<h3>Monthly Returns</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Rolling metrics (needs > 90 days)
    if n_daily > 90:
        try:
            fig = rolling_metrics(equity, window=60)
            sections.append(
                '<h3>Rolling Metrics (60-day window)</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Trade distribution
    if trades and len(trades) >= 5:
        try:
            fig = trade_distribution(trades)
            sections.append(
                '<h3>Trade P&L Distribution</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Trade timeline
    if trades and len(trades) >= 3:
        try:
            fig = trade_timeline(trades)
            sections.append(
                '<h3>Trade Timeline</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Win/loss streaks
    if trades and len(trades) >= 5:
        try:
            fig = win_loss_streak(trades)
            sections.append(
                '<h3>Win/Loss Streaks</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Monte Carlo fan
    if mc_trade_pnls and len(mc_trade_pnls) >= 5:
        try:
            fig = monte_carlo_fan(mc_trade_pnls, n_sims=1000)
            sections.append(
                '<h3>Monte Carlo Simulation (1K paths)</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Regime performance
    if regime_results:
        try:
            fig = regime_performance(regime_results)
            sections.append(
                '<h3>Regime Performance</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Crash stress chart
    if crash_events:
        try:
            fig = crash_stress_chart(crash_events)
            sections.append(
                '<h3>Crash Stress Test</h3>'
                + fig.to_html(include_plotlyjs=False, full_html=False)
            )
        except Exception:
            pass

    # Protocol results summary table
    if protocol_results:
        try:
            sections.append(_protocol_results_html(protocol_results))
        except Exception:
            pass

    # Trade log table
    if trades and len(trades) > 0:
        try:
            sections.append(_trade_log_html(trades))
        except Exception:
            pass

    return sections


def _protocol_results_html(results: list) -> str:
    """Build a protocol results summary table."""
    rows = []
    for r in results:
        status = '<span style="color:#2e7d32">PASS</span>' if r.passed else '<span style="color:#c62828">FAIL</span>'
        rows.append(
            f"<tr><td>{r.protocol}</td><td>{status}</td>"
            f"<td>{r.summary}</td><td>{r.runtime_seconds:.1f}s</td></tr>"
        )
    return (
        '<h3>Validation Protocol Results</h3>'
        '<table style="width:100%;border-collapse:collapse;margin:10px 0">'
        '<tr style="background:#f5f5f5"><th style="padding:8px;border:1px solid #ddd;text-align:left">Protocol</th>'
        '<th style="padding:8px;border:1px solid #ddd">Status</th>'
        '<th style="padding:8px;border:1px solid #ddd;text-align:left">Summary</th>'
        '<th style="padding:8px;border:1px solid #ddd">Runtime</th></tr>'
        + "\n".join(rows)
        + "</table>"
    )


def _trade_log_html(trades: list, max_rows: int = 100) -> str:
    """Build an HTML table of trades (capped at max_rows)."""
    header = (
        '<h3>Trade Log</h3>'
        '<table style="width:100%;border-collapse:collapse;margin:10px 0;font-size:13px">'
        '<tr style="background:#f5f5f5">'
        '<th style="padding:6px;border:1px solid #ddd">#</th>'
        '<th style="padding:6px;border:1px solid #ddd">Side</th>'
        '<th style="padding:6px;border:1px solid #ddd">Entry</th>'
        '<th style="padding:6px;border:1px solid #ddd">Exit</th>'
        '<th style="padding:6px;border:1px solid #ddd">Qty</th>'
        '<th style="padding:6px;border:1px solid #ddd">P&L</th>'
        '<th style="padding:6px;border:1px solid #ddd">P&L %</th>'
        '<th style="padding:6px;border:1px solid #ddd">Reason</th>'
        '</tr>'
    )
    rows = []
    for i, t in enumerate(trades[:max_rows], 1):
        pnl = float(t.pnl)
        color = "#2e7d32" if pnl > 0 else "#c62828" if pnl < 0 else "#333"
        rows.append(
            f'<tr><td style="padding:6px;border:1px solid #ddd">{i}</td>'
            f'<td style="padding:6px;border:1px solid #ddd">{t.side}</td>'
            f'<td style="padding:6px;border:1px solid #ddd">${float(t.entry_price):,.2f}</td>'
            f'<td style="padding:6px;border:1px solid #ddd">${float(t.exit_price):,.2f}</td>'
            f'<td style="padding:6px;border:1px solid #ddd">{float(t.quantity):.6f}</td>'
            f'<td style="padding:6px;border:1px solid #ddd;color:{color}">${pnl:.2f}</td>'
            f'<td style="padding:6px;border:1px solid #ddd;color:{color}">{float(t.pnl_pct)*100:.2f}%</td>'
            f'<td style="padding:6px;border:1px solid #ddd">{t.exit_reason}</td></tr>'
        )

    footer = ""
    if len(trades) > max_rows:
        footer = f'<tr><td colspan="8" style="padding:8px;text-align:center;color:#999">Showing {max_rows} of {len(trades)} trades</td></tr>'

    return header + "\n".join(rows) + footer + "</table>"


# ---------------------------------------------------------------------------
# Metric card builder
# ---------------------------------------------------------------------------

def _build_metric_cards(m: dict) -> list[dict]:
    """Build display cards from metrics dict.  Values are READ, not computed."""
    cards = []

    def _add(label: str, key: str, fmt: str = "{:.2f}", suffix: str = "", threshold: float | None = None):
        val = m.get(key)
        if val is None:
            return
        display = fmt.format(val) + suffix
        if threshold is not None:
            color = "positive" if val > threshold else ("negative" if val < -abs(threshold) else "neutral")
        else:
            color = "neutral"
        cards.append({"label": label, "display": display, "color_class": color})

    _add("Total Return", "total_return_pct", "{:.2f}", "%", threshold=0)
    _add("Sharpe Ratio", "sharpe", "{:.3f}", "", threshold=0)
    _add("Sortino Ratio", "sortino", "{:.3f}", "", threshold=0)
    _add("Max Drawdown", "max_drawdown_pct", "{:.2f}", "%")
    if cards and cards[-1]["label"] == "Max Drawdown":
        dd = m.get("max_drawdown_pct", 0)
        cards[-1]["color_class"] = "negative" if dd > 10 else "neutral"

    _add("Calmar Ratio", "calmar", "{:.3f}", "", threshold=0)
    _add("Win Rate", "win_rate_pct", "{:.1f}", "%", threshold=50)
    _add("Profit Factor", "profit_factor", "{:.3f}", "", threshold=1)
    _add("Total Trades", "total_trades", "{:d}", "")
    _add("Commission", "total_commission", "${:.2f}", "")
    _add("Buy & Hold", "buy_hold_return_pct", "{:.2f}", "%", threshold=0)
    _add("PSR", "psr", "{:.1%}", "", threshold=0.95)

    return cards


def _date_range(equity: pd.Series) -> tuple[str, str]:
    """Extract human-readable date range from equity curve index."""
    try:
        start = pd.to_datetime(equity.index[0], unit="ms").strftime("%Y-%m-%d")
        end = pd.to_datetime(equity.index[-1], unit="ms").strftime("%Y-%m-%d")
        return start, end
    except Exception:
        return str(equity.index[0]), str(equity.index[-1])
