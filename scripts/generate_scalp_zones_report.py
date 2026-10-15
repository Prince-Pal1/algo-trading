#!/usr/bin/env python3
"""Generate an interactive HTML visual report of detected Gold Scalp Zones.

Visualizes detected 1-minute Gold scalp regimes with interactive candlestick charts,
regression trendlines, breakout points, and full metric breakdowns.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Project root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.strategies.scalping.scalp_zone_detector import ScalpZoneDetector


def detect_zones(df: pd.DataFrame, threshold: float = 85.0, cooldown_bars: int = 10) -> list[dict]:
    detector = ScalpZoneDetector(window_size=10, min_score_threshold=threshold)
    events = []
    last_event_bar = -999

    for i, row in df.iterrows():
        res = detector.update({
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "timestamp": row["timestamp"],
        })
        if res and res.is_scalpable:
            if i - last_event_bar >= cooldown_bars:
                last_event_bar = i
                events.append({
                    "bar_idx": i,
                    "timestamp": row["timestamp"],
                    "dt": row["dt"],
                    "direction": res.direction,
                    "score": res.composite_score,
                    "net_move": res.net_move_dollars,
                    "atr_norm_move": res.atr_norm_move,
                    "er": res.er,
                    "r2": res.r_squared,
                    "dir_ratio": res.directional_ratio,
                    "cv": res.cv_range,
                    "body_pct": res.median_body_pct,
                    "close": res.current_close,
                    "atr": res.atr_14,
                })
    return events


def create_chart_html(df: pd.DataFrame, event: dict, context_before: int = 4, context_after: int = 8) -> str:
    i = event["bar_idx"]
    start_idx = max(0, i - 9 - context_before)
    end_idx = min(len(df), i + context_after + 1)
    sub = df.iloc[start_idx:end_idx].copy().reset_index(drop=True)

    leg_start_in_sub = context_before if (i - 9 - context_before) >= 0 else (i - 9)
    leg_end_in_sub = leg_start_in_sub + 9

    # Fit linear regression line over the 10-candle leg
    leg_closes = sub["close"].iloc[leg_start_in_sub : leg_end_in_sub + 1].values
    x_leg = np.arange(len(leg_closes))
    slope, intercept = np.polyfit(x_leg, leg_closes, 1)
    trend_line = slope * x_leg + intercept

    # Extension of trendline 4 bars into future to show breakout
    x_ext = np.arange(len(leg_closes) + 4)
    trend_line_ext = slope * x_ext + intercept

    fig = make_subplots(
        rows=1, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
    )

    # Candlestick
    fig.add_trace(
        go.Candlestick(
            x=sub["dt"].dt.strftime("%H:%M"),
            open=sub["open"],
            high=sub["high"],
            low=sub["low"],
            close=sub["close"],
            name="XAUUSD 1m",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a",
            decreasing_fillcolor="#ef5350",
        )
    )

    # Trendline overlay
    x_dates = sub["dt"].dt.strftime("%H:%M").values
    trend_x = x_dates[leg_start_in_sub : leg_end_in_sub + 1]

    trend_color = "#29b6f6" if event["direction"] == "LONG" else "#ffca28"
    fig.add_trace(
        go.Scatter(
            x=trend_x,
            y=trend_line,
            mode="lines",
            name=f"Leg Trendline (R²={event['r2']:.2f})",
            line=dict(color=trend_color, width=2.5),
        )
    )

    # Extended projection
    if leg_end_in_sub + 4 < len(x_dates):
        ext_x = x_dates[leg_start_in_sub : leg_end_in_sub + 5]
        fig.add_trace(
            go.Scatter(
                x=ext_x,
                y=trend_line_ext,
                mode="lines",
                name="Trendline Extension",
                line=dict(color=trend_color, width=1.5, dash="dot"),
            )
        )

    # Highlight box for the detected 10-bar scalp regime
    fig.add_vrect(
        x0=x_dates[leg_start_in_sub],
        x1=x_dates[leg_end_in_sub],
        fillcolor="rgba(255, 255, 255, 0.06)",
        layer="below",
        line_width=1,
        line_color="rgba(255, 255, 255, 0.2)",
        annotation_text="Detected Scalp Leg",
        annotation_position="top left",
        annotation_font=dict(color="#ffffff", size=10),
    )

    badge_color = "#26a69a" if event["direction"] == "LONG" else "#ef5350"

    fig.update_layout(
        title=dict(
            text=(
                f"<b>{event['direction']} SCALP ZONE</b> &nbsp;|&nbsp; "
                f"<span style='color:{badge_color}'>Score: {event['score']:.1f}/100</span> &nbsp;|&nbsp; "
                f"Time: {event['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')} &nbsp;|&nbsp; "
                f"Price: ${event['close']:.2f}"
            ),
            font=dict(size=14, color="#ffffff"),
        ),
        template="plotly_dark",
        paper_bgcolor="#131722",
        plot_bgcolor="#131722",
        margin=dict(l=50, r=50, t=50, b=40),
        xaxis_rangeslider_visible=False,
        height=380,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=10, color="#b2b5be"),
        ),
    )

    return fig.to_html(full_html=False, include_plotlyjs=False)


def generate_report():
    print("Loading data/historical/XAUUSD_1m.parquet...")
    df = pd.read_parquet(REPO_ROOT / "data" / "historical" / "XAUUSD_1m.parquet")
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")

    # 1. Latest days in dataset (April 2026)
    df_latest = df[(df["dt"] >= "2026-04-06") & (df["dt"] <= "2026-04-13")].copy().reset_index(drop=True)
    events_latest = detect_zones(df_latest, threshold=85.0, cooldown_bars=10)

    # 2. Reference period (March 24-28, 2026)
    df_march = df[(df["dt"] >= "2026-03-24") & (df["dt"] <= "2026-03-29")].copy().reset_index(drop=True)
    events_march = detect_zones(df_march, threshold=85.0, cooldown_bars=10)

    print(f"Found {len(events_latest)} high-scoring zones in latest April 2026 days.")
    print(f"Found {len(events_march)} high-scoring zones in March 24-28 reference week.")

    # Select top 5 from latest days and top 5 from March reference week
    selected_latest = sorted(events_latest, key=lambda x: x["score"], reverse=True)[:5]
    selected_march = sorted(events_march, key=lambda x: x["score"], reverse=True)[:5]

    charts_latest_html = ""
    for ev in selected_latest:
        chart_html = create_chart_html(df_latest, ev)
        charts_latest_html += f"""
        <div class="card">
            <div class="metrics-grid">
                <div class="metric"><span class="m-label">Direction</span><span class="m-val {'long' if ev['direction']=='LONG' else 'short'}">{ev['direction']}</span></div>
                <div class="metric"><span class="m-label">Composite Score</span><span class="m-val highlight">{ev['score']:.1f}/100</span></div>
                <div class="metric"><span class="m-label">Leg Move</span><span class="m-val">${ev['net_move']:.2f} ({ev['atr_norm_move']:.1f}x ATR)</span></div>
                <div class="metric"><span class="m-label">Kaufman ER</span><span class="m-val">{ev['er']:.2f} (Straight Path)</span></div>
                <div class="metric"><span class="m-label">Trendline R²</span><span class="m-val">{ev['r2']:.2f} (Clean Fit)</span></div>
                <div class="metric"><span class="m-label">Directional %</span><span class="m-val">{ev['dir_ratio']*100:.0f}%</span></div>
            </div>
            {chart_html}
        </div>
        """

    charts_march_html = ""
    for ev in selected_march:
        chart_html = create_chart_html(df_march, ev)
        charts_march_html += f"""
        <div class="card">
            <div class="metrics-grid">
                <div class="metric"><span class="m-label">Direction</span><span class="m-val {'long' if ev['direction']=='LONG' else 'short'}">{ev['direction']}</span></div>
                <div class="metric"><span class="m-label">Composite Score</span><span class="m-val highlight">{ev['score']:.1f}/100</span></div>
                <div class="metric"><span class="m-label">Leg Move</span><span class="m-val">${ev['net_move']:.2f} ({ev['atr_norm_move']:.1f}x ATR)</span></div>
                <div class="metric"><span class="m-label">Kaufman ER</span><span class="m-val">{ev['er']:.2f} (Straight Path)</span></div>
                <div class="metric"><span class="m-label">Trendline R²</span><span class="m-val">{ev['r2']:.2f} (Clean Fit)</span></div>
                <div class="metric"><span class="m-label">Directional %</span><span class="m-val">{ev['dir_ratio']*100:.0f}%</span></div>
            </div>
            {chart_html}
        </div>
        """

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gold Scalp Zone Detector — Verification Report</title>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-main: #0c0e14;
            --bg-card: #131722;
            --border: #2a2e39;
            --text-main: #d1d4dc;
            --text-muted: #787b86;
            --accent-green: #26a69a;
            --accent-red: #ef5350;
            --accent-gold: #f0b90b;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-main);
            color: var(--text-main);
            font-family: 'Inter', -apple-system, sans-serif;
            line-height: 1.5;
            padding: 24px;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        header {{
            margin-bottom: 32px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }}
        h1 {{
            font-size: 26px;
            font-weight: 700;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .badge-gold {{
            background: rgba(240, 185, 11, 0.15);
            color: var(--accent-gold);
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 13px;
            font-weight: 600;
        }}
        p.subtitle {{ color: var(--text-muted); font-size: 14px; margin-top: 6px; }}
        
        .summary-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 32px;
        }}
        .stat-box {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }}
        .stat-box .title {{ font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-box .value {{ font-size: 24px; font-weight: 700; color: #fff; margin-top: 4px; font-family: 'JetBrains Mono', monospace; }}

        h2 {{ font-size: 18px; color: #ffffff; margin: 32px 0 16px 0; display: flex; align-items: center; gap: 8px; }}
        .section-tag {{ font-size: 12px; background: #2a2e39; color: #d1d4dc; padding: 2px 8px; border-radius: 4px; }}

        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 24px;
            overflow: hidden;
        }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            padding: 14px 18px;
            background: rgba(255, 255, 255, 0.02);
            border-bottom: 1px solid var(--border);
            font-family: 'JetBrains Mono', monospace;
        }}
        .metric {{ display: flex; flex-direction: column; }}
        .m-label {{ font-size: 11px; color: var(--text-muted); }}
        .m-val {{ font-size: 13px; font-weight: 600; color: #fff; margin-top: 2px; }}
        .m-val.long {{ color: var(--accent-green); }}
        .m-val.short {{ color: var(--accent-red); }}
        .m-val.highlight {{ color: var(--accent-gold); font-size: 15px; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Gold (XAUUSD) Scalp Zone Detector <span class="badge-gold">Verification Report</span></h1>
            <p class="subtitle">Visual verification of smooth 1-minute directional trendline-break setups detected by the Kaufman ER + Linearity R² algorithm.</p>
        </header>

        <div class="summary-stats">
            <div class="stat-box">
                <div class="title">Scoring Model</div>
                <div class="value">ER + R² (0-100)</div>
            </div>
            <div class="stat-box">
                <div class="title">Min Threshold</div>
                <div class="value">≥ 85.0 pts</div>
            </div>
            <div class="stat-box">
                <div class="title">Leg Fit Quality</div>
                <div class="value">R² > 0.85</div>
            </div>
            <div class="stat-box">
                <div class="title">Target Scalp</div>
                <div class="value">$2.00 / 30s</div>
            </div>
        </div>

        <h2>Latest Detected Zones in Dataset (April 2026) <span class="section-tag">{len(selected_latest)} Examples</span></h2>
        {charts_latest_html}

        <h2>Reference Week Zones (March 24–28, 2026) <span class="section-tag">{len(selected_march)} Examples</span></h2>
        {charts_march_html}
    </div>
</body>
</html>
"""

    report_path = REPO_ROOT / "reports" / "gold_scalp_zones_report.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(full_html, encoding="utf-8")
    print(f"\nSuccessfully generated interactive HTML report at:\n{report_path}")


if __name__ == "__main__":
    generate_report()
