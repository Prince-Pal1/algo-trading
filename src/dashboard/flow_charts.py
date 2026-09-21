"""Plotly renderers for order-flow (CVD) data.

One public figure builder: `price_cvd_panels`, which stacks price, cumulative
delta, and per-bar delta as three panels sharing one x-axis.

Why stacked panels and not one chart with two y-axes: price and CVD have
unrelated units and scales, and a dual-axis chart lets the author slide one
series against the other until they appear to agree (or disagree). Separate
panels on a shared time axis let the reader compare *shape* — which is the
entire point of a divergence — without the axes implying a relationship that
was really chosen by scaling.

Palette: the diverging blue<->red pair, validated for colour-vision-deficient
separation in both light and dark modes (worst-pair CVD ΔE 21.6 light / 19.2
dark, normal-vision 32.3 / 29.0). Direction is always encoded by marker shape
as well as colour, so nothing here reads by colour alone. The dashboard is
light-mode throughout, so these render light; the dark steps are recorded
beside each constant for a future dark pass.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Diverging pair — buy/sell polarity.        (dark-mode step)
COLOR_BUY = "#2a78d6"      # blue            #3987e5
COLOR_SELL = "#e34948"     # red             #e66767
# Recessive ink for the price reference line and absorption shading.
COLOR_PRICE = "#52514e"    # secondary ink   #c3c2b7
COLOR_ABSORPTION = "#f0efec"  # neutral midpoint  #383835

_EMPTY_MESSAGE = (
    "No flow bars to plot — run: "
    "python -m scripts.cvd fetch SYMBOL --tf TF --start DATE"
)


def price_cvd_panels(
    bars: pd.DataFrame,
    divergences: pd.DataFrame | None = None,
    absorption: pd.DataFrame | None = None,
    title: str = "Order Flow",
    max_absorption_bands: int = 60,
) -> go.Figure:
    """Price / CVD / per-bar delta as three panels on a shared time axis.

    Args:
        bars: output of `src.data.cvd.compute_delta_bars` — needs timestamp,
            close, cvd, delta.
        divergences: output of `src.data.cvd.detect_divergences`; marked on the
            price panel as triangles (down = bearish, up = bullish).
        absorption: output of `src.data.cvd.detect_absorption`; shaded as
            vertical bands across all three panels.
        title: figure title.
        max_absorption_bands: cap on shaded bands. Plotly slows badly past a
            few hundred shapes, and a chart with hundreds of bands is unreadable
            anyway — the table view below the chart carries the full list.

    Returns:
        A plotly Figure. Empty input returns a figure carrying an explanatory
        annotation rather than an empty frame.
    """
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.44, 0.34, 0.22],
        vertical_spacing=0.06,
    )

    if bars is None or bars.empty:
        fig.add_annotation(
            text=_EMPTY_MESSAGE,
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=13, color=COLOR_PRICE),
        )
        fig.update_layout(
            template="plotly_white",
            title=dict(text=title, x=0, xanchor="left"),
            height=640,
        )
        return fig

    idx = pd.to_datetime(bars["timestamp"], unit="ms", utc=True)

    # ── Row 1: price ───────────────────────────────────────────────────
    fig.add_trace(
        go.Scatter(
            x=idx, y=bars["close"],
            name="Price",
            line=dict(color=COLOR_PRICE, width=2),
            hovertemplate="%{y:,.4f}<extra>Price</extra>",
        ),
        row=1, col=1,
    )

    if divergences is not None and not divergences.empty:
        _add_divergence_markers(fig, bars, divergences)

    # ── Row 2: CVD ─────────────────────────────────────────────────────
    fig.add_trace(
        go.Scatter(
            x=idx, y=bars["cvd"],
            name="CVD",
            line=dict(color=COLOR_BUY, width=2),
            hovertemplate="%{y:,.2f}<extra>CVD</extra>",
        ),
        row=2, col=1,
    )

    # ── Row 3: per-bar delta, split so the legend carries the meaning ──
    delta = bars["delta"].astype(float)
    buy_side = delta.where(delta >= 0)
    sell_side = delta.where(delta < 0)

    fig.add_trace(
        go.Bar(
            x=idx, y=buy_side,
            name="Buy-dominant bar",
            marker=dict(color=COLOR_BUY),
            hovertemplate="+%{y:,.2f}<extra>Buy-dominant</extra>",
        ),
        row=3, col=1,
    )
    fig.add_trace(
        go.Bar(
            x=idx, y=sell_side,
            name="Sell-dominant bar",
            marker=dict(color=COLOR_SELL),
            hovertemplate="%{y:,.2f}<extra>Sell-dominant</extra>",
        ),
        row=3, col=1,
    )

    if absorption is not None and not absorption.empty:
        _add_absorption_bands(fig, bars, absorption, max_absorption_bands)

    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top"),
        template="plotly_white",
        hovermode="x unified",
        barmode="relative",
        bargap=0.1,
        height=760,
        margin=dict(l=70, r=30, t=105, b=45),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="CVD (cumulative)", row=2, col=1)
    fig.update_yaxes(title_text="Delta / bar", row=3, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=3, col=1)
    return fig


def _add_divergence_markers(
    fig: go.Figure, bars: pd.DataFrame, divergences: pd.DataFrame
) -> None:
    """Triangles on the price panel — shape encodes direction, not just colour."""
    price_by_ts = dict(zip(bars["timestamp"], bars["close"]))
    span = float(bars["close"].max() - bars["close"].min()) or 1.0
    offset = span * 0.03

    specs = [
        ("bearish", "triangle-down", COLOR_SELL, +offset, "Bearish divergence"),
        ("bullish", "triangle-up", COLOR_BUY, -offset, "Bullish divergence"),
    ]
    for kind, symbol, color, shift, label in specs:
        subset = divergences[divergences["kind"] == kind]
        if subset.empty:
            continue
        xs, ys, texts = [], [], []
        for _, row in subset.iterrows():
            ts = int(row["timestamp"])
            price = price_by_ts.get(ts)
            if price is None:
                continue
            xs.append(pd.to_datetime(ts, unit="ms", utc=True))
            ys.append(price + shift)
            texts.append(f"{label}<br>strength {row['strength']:.2f}")
        if not xs:
            continue
        fig.add_trace(
            go.Scatter(
                x=xs, y=ys,
                mode="markers",
                name=label,
                marker=dict(symbol=symbol, size=11, color=color,
                            line=dict(color="#ffffff", width=1)),
                text=texts,
                hovertemplate="%{text}<extra></extra>",
            ),
            row=1, col=1,
        )


def _add_absorption_bands(
    fig: go.Figure, bars: pd.DataFrame, absorption: pd.DataFrame, cap: int
) -> None:
    """Shade absorption bars across every panel so they read as time context."""
    timestamps = bars["timestamp"].astype("int64")
    if len(timestamps) > 1:
        bar_ms = int(timestamps.diff().dropna().median())
    else:
        bar_ms = 60_000

    for ts in absorption["timestamp"].tail(cap).astype("int64"):
        fig.add_vrect(
            x0=pd.to_datetime(int(ts), unit="ms", utc=True),
            x1=pd.to_datetime(int(ts) + bar_ms, unit="ms", utc=True),
            fillcolor=COLOR_ABSORPTION,
            opacity=0.55,
            layer="below",
            line_width=0,
        )
