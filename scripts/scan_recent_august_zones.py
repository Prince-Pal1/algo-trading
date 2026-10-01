#!/usr/bin/env python3
"""Scan and visualize top Gold Scalp Zones after August 15, 2026.

Analyzes 1-minute Gold data from August 16 to August 21, 2026 and produces
both console tables and an interactive Plotly HTML report with candlestick charts.
"""

from __future__ import annotations

import sys
from pathlib import Path
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.strategies.scalping.scalp_zone_detector import ScalpZoneDetector
from scripts.generate_scalp_zones_report import create_chart_html


def scan_august_zones():
    parquet_path = REPO_ROOT / "data" / "historical" / "XAUUSD_1m_aug15_23.parquet"
    if not parquet_path.exists():
        print("Parquet file not found:", parquet_path)
        return

    df = pd.read_parquet(parquet_path)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values(by="timestamp").reset_index(drop=True)

    print("=" * 80)
    print(f"GOLD 1M SCALP ZONE ANALYSIS: AFTER AUGUST 15, 2026")
    print(f"Data Range: {df['dt'].min()} to {df['dt'].max()} ({len(df):,} 1m bars)")
    print(f"Price Range: ${df['low'].min():.2f} - ${df['high'].max():.2f}")
    print("=" * 80)

    # Use a lower threshold (e.g. 70.0) to capture all top scorers in this recent period
    detector = ScalpZoneDetector(window_size=10, min_score_threshold=70.0)
    events = []
    last_event_bar = -999
    cooldown_bars = 8

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

    print(f"\nTotal Detected Scalp Setups (Score >= 70.0): {len(events)}")

    # Sort by score to get top scorers
    top_events = sorted(events, key=lambda x: x["score"], reverse=True)

    print("\n" + "-" * 80)
    print("TOP 12 HIGHEST-SCORING SCALP REGIMES (August 16 – August 21, 2026)")
    print("-" * 80)
    for idx, ev in enumerate(top_events[:12], 1):
        print(
            f"{idx:2d}. {ev['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')} | "
            f"Dir: {ev['direction']:5s} | Score: {ev['score']:5.1f}/100 | "
            f"Price: ${ev['close']:7.2f} | Move: ${ev['net_move']:5.2f} ({ev['atr_norm_move']:.1f}x ATR) | "
            f"ER: {ev['er']:.2f} | R²: {ev['r2']:.2f} | Dir%: {ev['dir_ratio']*100:.0f}%"
        )

    # Chronological recent setups
    recent_events = sorted(events, key=lambda x: x["dt"])[-10:]
    print("\n" + "-" * 80)
    print("MOST RECENT SCALP REGIMES IN CHRONOLOGICAL ORDER")
    print("-" * 80)
    for ev in recent_events:
        print(
            f"Time: {ev['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')} | "
            f"Dir: {ev['direction']:5s} | Score: {ev['score']:5.1f}/100 | "
            f"Price: ${ev['close']:7.2f} | Move: ${ev['net_move']:5.2f} ({ev['atr_norm_move']:.1f}x ATR) | "
            f"ER: {ev['er']:.2f} | R²: {ev['r2']:.2f}"
        )

    # Generate visual HTML report
    selected_for_report = top_events[:10]
    charts_html = ""
    for ev in selected_for_report:
        chart_html = create_chart_html(df, ev, context_before=5, context_after=8)
        charts_html += f"""
        <div class="card">
            <div class="metrics-grid">
                <div class="metric"><span class="m-label">Direction</span><span class="m-val {'long' if ev['direction']=='LONG' else 'short'}">{ev['direction']}</span></div>
                <div class="metric"><span class="m-label">Composite Score</span><span class="m-val highlight">{ev['score']:.1f}/100</span></div>
                <div class="metric"><span class="m-label">Price Level</span><span class="m-val">${ev['close']:.2f}</span></div>
                <div class="metric"><span class="m-label">Leg Move</span><span class="m-val">${ev['net_move']:.2f} ({ev['atr_norm_move']:.1f}x ATR)</span></div>
                <div class="metric"><span class="m-label">Kaufman ER</span><span class="m-val">{ev['er']:.2f}</span></div>
                <div class="metric"><span class="m-label">Trendline R²</span><span class="m-val">{ev['r2']:.2f}</span></div>
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
    <title>Gold Scalp Zones — August 15-21, 2026 Setups</title>
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
            margin-bottom: 28px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }}
        h1 {{
            font-size: 24px;
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
            margin-bottom: 28px;
        }}
        .stat-box {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px 16px;
        }}
        .stat-box .title {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-box .value {{ font-size: 22px; font-weight: 700; color: #fff; margin-top: 4px; font-family: 'JetBrains Mono', monospace; }}

        h2 {{ font-size: 18px; color: #ffffff; margin: 24px 0 16px 0; }}
        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 20px;
            overflow: hidden;
        }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: 12px;
            padding: 12px 16px;
            background: rgba(255, 255, 255, 0.02);
            border-bottom: 1px solid var(--border);
            font-family: 'JetBrains Mono', monospace;
        }}
        .metric {{ display: flex; flex-direction: column; }}
        .m-label {{ font-size: 11px; color: var(--text-muted); }}
        .m-val {{ font-size: 13px; font-weight: 600; color: #fff; margin-top: 2px; }}
        .m-val.long {{ color: var(--accent-green); }}
        .m-val.short {{ color: var(--accent-red); }}
        .m-val.highlight {{ color: var(--accent-gold); font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Gold (XAUUSD) Scalp Zones <span class="badge-gold">August 16–21, 2026</span></h1>
            <p class="subtitle">Interactive 1-minute candlestick visual verification of smooth directional legs detected after August 15, 2026.</p>
        </header>

        <div class="summary-stats">
            <div class="stat-box">
                <div class="title">Period Analyzed</div>
                <div class="value">Aug 16 – 21, 2026</div>
            </div>
            <div class="stat-box">
                <div class="title">Total 1m Bars</div>
                <div class="value">{len(df):,}</div>
            </div>
            <div class="stat-box">
                <div class="title">Detected Zones (≥70 pts)</div>
                <div class="value">{len(events)}</div>
            </div>
            <div class="stat-box">
                <div class="title">Top Scorers Shown</div>
                <div class="value">{len(selected_for_report)}</div>
            </div>
        </div>

        <h2>Top 10 High-Quality Scalp Setups (August 16–21, 2026)</h2>
        {charts_html}
    </div>
</body>
</html>
"""
    out_file = REPO_ROOT / "reports" / "august_2026_scalp_zones_report.html"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(full_html, encoding="utf-8")
    print(f"\n✓ Generated interactive visual report for August 15+ at:\n{out_file}")


if __name__ == "__main__":
    scan_august_zones()
