"""Tests for src/dashboard/flow_charts.py — figure structure, not pixels.

The load-bearing assertion is `test_no_dual_axis`: price and CVD must stay in
separate panels on a shared x-axis. Overlaying them on two y-scales would let
the axis scaling decide whether the two series appear to agree, which is
precisely the judgement a divergence chart exists to leave to the reader.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.dashboard.flow_charts import (
    COLOR_BUY,
    COLOR_PRICE,
    COLOR_SELL,
    price_cvd_panels,
)

_BASE_TS = 1_757_000_000_000 // 300_000 * 300_000
_TF_MS = 300_000


def _bars(n: int = 60, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.05, 0.4, n))
    delta = rng.normal(0, 30, n)
    return pd.DataFrame({
        "timestamp": [_BASE_TS + i * _TF_MS for i in range(n)],
        "open": close, "high": close + 0.4, "low": close - 0.4, "close": close,
        "buy_volume": 60.0, "sell_volume": 40.0, "volume": 100.0,
        "delta": delta, "delta_high": 0.0, "delta_low": 0.0,
        "cvd": np.cumsum(delta), "trade_count": 20,
    })


def _divergences(bars: pd.DataFrame, kinds: list[str]) -> pd.DataFrame:
    rows = []
    for i, kind in enumerate(kinds):
        row = bars.iloc[i * 5]
        rows.append({
            "timestamp": int(row["timestamp"]), "kind": kind,
            "price_z": 2.0 if kind == "bearish" else -2.0,
            "cvd_z": -2.0 if kind == "bearish" else 2.0,
            "strength": 2.0, "close": row["close"], "cvd": row["cvd"],
        })
    return pd.DataFrame(rows)


def _absorption(bars: pd.DataFrame, count: int) -> pd.DataFrame:
    sub = bars.head(count)
    return pd.DataFrame({
        "timestamp": sub["timestamp"].to_numpy(),
        "kind": ["sell_absorbed"] * count,
        "delta_ratio": [-0.5] * count,
        "range_ratio": [0.3] * count,
        "close": sub["close"].to_numpy(),
        "delta": sub["delta"].to_numpy(),
        "volume": sub["volume"].to_numpy(),
    })


def _trace(fig, name: str):
    for t in fig.data:
        if t.name == name:
            return t
    return None


class TestStructure:
    def test_three_panels_share_one_x_axis(self):
        fig = price_cvd_panels(_bars())
        # Rows 1 and 2 hand their tick labels to row 3's shared axis.
        assert fig.layout.xaxis3.anchor == "y3"
        assert fig.layout.yaxis.domain[0] > fig.layout.yaxis2.domain[1]
        assert fig.layout.yaxis2.domain[0] > fig.layout.yaxis3.domain[1]

    def test_no_dual_axis(self):
        """No y-axis may overlay another — that is the dual-axis anti-pattern."""
        fig = price_cvd_panels(_bars(), _divergences(_bars(), ["bearish"]))
        for key in fig.layout:
            if key.startswith("yaxis"):
                assert getattr(fig.layout, key).overlaying is None, key

    def test_price_and_cvd_are_in_different_rows(self):
        fig = price_cvd_panels(_bars())
        assert _trace(fig, "Price").yaxis == "y"
        assert _trace(fig, "CVD").yaxis == "y2"

    def test_delta_bars_split_into_labelled_series(self):
        """Sign is carried by a legend entry, not by colour alone."""
        fig = price_cvd_panels(_bars())
        buy = _trace(fig, "Buy-dominant bar")
        sell = _trace(fig, "Sell-dominant bar")
        assert buy is not None and sell is not None
        assert buy.marker.color == COLOR_BUY
        assert sell.marker.color == COLOR_SELL
        assert buy.yaxis == "y3" and sell.yaxis == "y3"

    def test_delta_split_is_lossless(self):
        bars = _bars()
        fig = price_cvd_panels(bars)
        buy = np.nan_to_num(np.array(_trace(fig, "Buy-dominant bar").y, dtype=float))
        sell = np.nan_to_num(np.array(_trace(fig, "Sell-dominant bar").y, dtype=float))
        assert np.allclose(buy + sell, bars["delta"].to_numpy())

    def test_price_line_is_recessive_ink(self):
        fig = price_cvd_panels(_bars())
        assert _trace(fig, "Price").line.color == COLOR_PRICE

    def test_light_template(self):
        fig = price_cvd_panels(_bars())
        assert fig.layout.template.layout.plot_bgcolor == "white"

    def test_hover_is_unified(self):
        assert price_cvd_panels(_bars()).layout.hovermode == "x unified"


class TestEmptyState:
    def test_empty_bars_returns_annotated_figure(self):
        fig = price_cvd_panels(pd.DataFrame())
        assert len(fig.data) == 0
        assert any("scripts.cvd fetch" in a.text for a in fig.layout.annotations)

    def test_none_bars_handled(self):
        fig = price_cvd_panels(None)
        assert len(fig.data) == 0

    def test_no_divergences_adds_no_marker_traces(self):
        fig = price_cvd_panels(_bars(), pd.DataFrame())
        names = [t.name for t in fig.data]
        assert "Bearish divergence" not in names
        assert "Bullish divergence" not in names


class TestDivergenceMarkers:
    def test_shape_encodes_direction_not_just_colour(self):
        bars = _bars()
        fig = price_cvd_panels(bars, _divergences(bars, ["bearish", "bullish"]))
        bear = _trace(fig, "Bearish divergence")
        bull = _trace(fig, "Bullish divergence")
        assert bear.marker.symbol == "triangle-down"
        assert bull.marker.symbol == "triangle-up"
        assert bear.marker.symbol != bull.marker.symbol

    def test_markers_meet_minimum_size(self):
        bars = _bars()
        fig = price_cvd_panels(bars, _divergences(bars, ["bearish"]))
        assert _trace(fig, "Bearish divergence").marker.size >= 8

    def test_markers_land_on_the_price_panel(self):
        bars = _bars()
        fig = price_cvd_panels(bars, _divergences(bars, ["bearish"]))
        assert _trace(fig, "Bearish divergence").yaxis == "y"

    def test_bearish_sits_above_and_bullish_below_the_line(self):
        bars = _bars()
        divs = _divergences(bars, ["bearish", "bullish"])
        fig = price_cvd_panels(bars, divs)
        bear_ref = float(divs.loc[divs["kind"] == "bearish", "close"].iloc[0])
        bull_ref = float(divs.loc[divs["kind"] == "bullish", "close"].iloc[0])
        assert float(_trace(fig, "Bearish divergence").y[0]) > bear_ref
        assert float(_trace(fig, "Bullish divergence").y[0]) < bull_ref

    def test_divergence_on_unknown_timestamp_is_skipped(self):
        bars = _bars()
        stray = pd.DataFrame([{
            "timestamp": _BASE_TS - 10 * _TF_MS, "kind": "bearish",
            "price_z": 2.0, "cvd_z": -2.0, "strength": 2.0,
            "close": 100.0, "cvd": 0.0,
        }])
        fig = price_cvd_panels(bars, stray)
        assert _trace(fig, "Bearish divergence") is None

    def test_flat_price_series_does_not_divide_by_zero(self):
        n = 30
        flat = pd.DataFrame({
            "timestamp": [_BASE_TS + i * _TF_MS for i in range(n)],
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
            "buy_volume": 1.0, "sell_volume": 1.0, "volume": 2.0,
            "delta": 0.0, "delta_high": 0.0, "delta_low": 0.0,
            "cvd": 0.0, "trade_count": 1,
        })
        divs = _divergences(flat, ["bearish"])
        fig = price_cvd_panels(flat, divs)
        assert np.isfinite(float(_trace(fig, "Bearish divergence").y[0]))


class TestAbsorptionBands:
    def _band_count(self, fig) -> int:
        return sum(1 for sh in fig.layout.shapes if sh.type == "rect")

    def test_bands_rendered(self):
        bars = _bars()
        fig = price_cvd_panels(bars, None, _absorption(bars, 3))
        assert self._band_count(fig) >= 3

    def test_band_count_capped(self):
        bars = _bars(n=80)
        fig = price_cvd_panels(bars, None, _absorption(bars, 50), max_absorption_bands=5)
        assert self._band_count(fig) <= 5 * 3  # cap × panels

    def test_bands_sit_below_the_data(self):
        bars = _bars()
        fig = price_cvd_panels(bars, None, _absorption(bars, 2))
        rects = [sh for sh in fig.layout.shapes if sh.type == "rect"]
        assert rects and all(sh.layer == "below" for sh in rects)

    def test_no_absorption_adds_no_shapes(self):
        fig = price_cvd_panels(_bars(), None, pd.DataFrame())
        assert self._band_count(fig) == 0

    def test_single_bar_series_does_not_crash(self):
        one = _bars(n=1)
        fig = price_cvd_panels(one, None, _absorption(one, 1))
        assert self._band_count(fig) >= 1


class TestPalette:
    @pytest.mark.parametrize("color", [COLOR_BUY, COLOR_SELL, COLOR_PRICE])
    def test_colors_are_hex(self, color):
        assert color.startswith("#") and len(color) == 7

    def test_buy_and_sell_are_the_validated_diverging_pair(self):
        # Changing either invalidates the colour-vision separation check that
        # was run against this pair; re-run the validator if you touch them.
        assert (COLOR_BUY, COLOR_SELL) == ("#2a78d6", "#e34948")
