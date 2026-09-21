"""Tests for src/data/cvd.py — aggressor convention, bar aggregation, patterns.

The load-bearing test here is `TestAggressorConvention` plus the
streaming-vs-vectorized parity test: if the `is_buyer_maker` mapping ever
inverts, or the two code paths drift apart, every downstream flow signal
flips sign silently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.data.cvd import (
    DEGENERATE_CHECK_AFTER,
    CVDCalculator,
    DeltaBar,
    compute_delta_bars,
    delta_ratio,
    detect_absorption,
    detect_divergences,
)
from src.utils.types import Tick

_MIN_MS = 60_000
_BASE_TS = 1_700_000_000_000 // _MIN_MS * _MIN_MS  # minute-aligned


def _tick(price: float, qty: float, is_buyer_maker: bool, ts: int, symbol: str = "BTCUSDT") -> Tick:
    return Tick(
        symbol=symbol,
        price=price,
        quantity=qty,
        timestamp=ts,
        is_buyer_maker=is_buyer_maker,
    )


def _trades_df(rows: list[tuple[int, float, float, bool]]) -> pd.DataFrame:
    """rows: (timestamp, price, quantity, is_buyer_maker)"""
    return pd.DataFrame(
        rows, columns=["timestamp", "price", "quantity", "is_buyer_maker"]
    )


class TestAggressorConvention:
    """is_buyer_maker=True means the SELLER was aggressive. Never invert this."""

    def test_buyer_maker_true_is_sell_volume(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 5.0, True, _BASE_TS))
        bar = calc.flush()
        assert bar is not None
        assert bar.sell_volume == 5.0
        assert bar.buy_volume == 0.0
        assert bar.delta == -5.0

    def test_buyer_maker_false_is_buy_volume(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 5.0, False, _BASE_TS))
        bar = calc.flush()
        assert bar is not None
        assert bar.buy_volume == 5.0
        assert bar.sell_volume == 0.0
        assert bar.delta == 5.0

    def test_vectorized_path_uses_same_convention(self):
        bars = compute_delta_bars(
            _trades_df([(_BASE_TS, 100.0, 5.0, True)]), "1m", symbol="BTCUSDT"
        )
        assert bars["sell_volume"].iloc[0] == 5.0
        assert bars["buy_volume"].iloc[0] == 0.0
        assert bars["delta"].iloc[0] == -5.0


class TestCVDCalculator:
    def test_rejects_unknown_timeframe(self):
        with pytest.raises(ValueError, match="unsupported timeframe"):
            CVDCalculator("BTCUSDT", "3m")

    def test_bar_closes_only_on_next_bucket(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        assert calc.update(_tick(100.0, 1.0, False, _BASE_TS)) is None
        assert calc.update(_tick(101.0, 2.0, False, _BASE_TS + 30_000)) is None
        closed = calc.update(_tick(102.0, 1.0, True, _BASE_TS + _MIN_MS))
        assert closed is not None
        assert closed.timestamp == _BASE_TS
        assert closed.buy_volume == 3.0
        assert closed.trade_count == 2

    def test_ohlc_tracked_within_bar(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        for px in (100.0, 105.0, 98.0, 103.0):
            calc.update(_tick(px, 1.0, False, _BASE_TS))
        bar = calc.flush()
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 98.0, 103.0)

    def test_cvd_accumulates_across_bars(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 10.0, False, _BASE_TS))            # +10
        calc.update(_tick(100.0, 4.0, True, _BASE_TS + _MIN_MS))    # closes bar 1
        assert calc.current_cvd == 10.0
        calc.update(_tick(100.0, 1.0, True, _BASE_TS + 2 * _MIN_MS))  # closes bar 2 (-4)
        assert calc.current_cvd == 6.0

    def test_initial_cvd_is_carried(self):
        calc = CVDCalculator("BTCUSDT", "1m", initial_cvd=100.0)
        calc.update(_tick(100.0, 3.0, False, _BASE_TS))
        bar = calc.flush()
        assert bar.cvd == 103.0

    def test_intrabar_delta_extremes(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        # running delta path: +8 → +3 → -2
        calc.update(_tick(100.0, 8.0, False, _BASE_TS))
        calc.update(_tick(100.0, 5.0, True, _BASE_TS))
        calc.update(_tick(100.0, 5.0, True, _BASE_TS))
        bar = calc.flush()
        assert bar.delta_high == 8.0
        assert bar.delta_low == -2.0
        assert bar.delta == -2.0

    def test_delta_extremes_bracket_zero_for_one_sided_bar(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 4.0, False, _BASE_TS))
        bar = calc.flush()
        assert bar.delta_high == 4.0
        assert bar.delta_low == 0.0

    def test_out_of_order_tick_dropped(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 1.0, False, _BASE_TS + _MIN_MS))
        late = calc.update(_tick(100.0, 99.0, False, _BASE_TS))  # previous bucket
        assert late is None
        assert calc.dropped_out_of_order == 1
        bar = calc.flush()
        assert bar.buy_volume == 1.0  # late tick excluded

    def test_flush_returns_none_when_nothing_pending(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        assert calc.flush() is None

    def test_flush_is_idempotent(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 1.0, False, _BASE_TS))
        assert calc.flush() is not None
        assert calc.flush() is None

    def test_pending_delta_before_close(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        calc.update(_tick(100.0, 7.0, False, _BASE_TS))
        assert calc.pending_delta == 7.0
        assert calc.current_cvd == 0.0  # not yet closed

    def test_reset_clears_state(self):
        calc = CVDCalculator("BTCUSDT", "1m", initial_cvd=50.0)
        calc.update(_tick(100.0, 5.0, False, _BASE_TS))
        calc.flush()
        calc.reset()
        assert calc.current_cvd == 50.0
        assert calc.pending_delta == 0.0

    def test_delta_ratio_property(self):
        bar = DeltaBar(
            symbol="X", timeframe="1m", timestamp=0,
            open=1.0, high=1.0, low=1.0, close=1.0,
            buy_volume=7.0, sell_volume=3.0, delta=4.0,
            delta_high=7.0, delta_low=0.0, cvd=4.0, trade_count=2,
        )
        assert bar.volume == 10.0
        assert bar.delta_ratio == pytest.approx(0.4)

    def test_delta_ratio_zero_volume_bar(self):
        bar = DeltaBar(
            symbol="X", timeframe="1m", timestamp=0,
            open=1.0, high=1.0, low=1.0, close=1.0,
            buy_volume=0.0, sell_volume=0.0, delta=0.0,
            delta_high=0.0, delta_low=0.0, cvd=0.0, trade_count=0,
        )
        assert bar.delta_ratio == 0.0


class TestDegenerateFeedDetection:
    """A quote-only CFD feed (quantity=0) must be flagged, not silently zeroed."""

    def test_zero_quantity_feed_flagged(self):
        calc = CVDCalculator("XAUUSD", "1m")
        for i in range(DEGENERATE_CHECK_AFTER):
            calc.update(_tick(2000.0 + i, 0.0, False, _BASE_TS + i))
        assert calc.is_degenerate is True

    def test_real_feed_not_flagged(self):
        calc = CVDCalculator("BTCUSDT", "1m")
        for i in range(DEGENERATE_CHECK_AFTER * 2):
            calc.update(_tick(100.0, 1.0, i % 2 == 0, _BASE_TS + i))
        assert calc.is_degenerate is False

    def test_not_flagged_before_threshold(self):
        calc = CVDCalculator("XAUUSD", "1m")
        for i in range(DEGENERATE_CHECK_AFTER - 1):
            calc.update(_tick(2000.0, 0.0, False, _BASE_TS + i))
        assert calc.is_degenerate is False

    def test_single_sized_tick_clears_degeneracy(self):
        calc = CVDCalculator("XAUUSD", "1m")
        for i in range(DEGENERATE_CHECK_AFTER):
            calc.update(_tick(2000.0, 0.0, False, _BASE_TS + i))
        calc.update(_tick(2000.0, 1.0, False, _BASE_TS + DEGENERATE_CHECK_AFTER))
        assert calc.is_degenerate is False


class TestComputeDeltaBars:
    def test_empty_input_returns_empty_frame(self):
        out = compute_delta_bars(_trades_df([]), "1m")
        assert out.empty
        assert "cvd" in out.columns

    def test_rejects_unknown_timeframe(self):
        with pytest.raises(ValueError, match="unsupported timeframe"):
            compute_delta_bars(_trades_df([(_BASE_TS, 1.0, 1.0, False)]), "7m")

    def test_buckets_by_timeframe(self):
        rows = [
            (_BASE_TS, 100.0, 1.0, False),
            (_BASE_TS + 30_000, 101.0, 2.0, False),
            (_BASE_TS + _MIN_MS, 102.0, 3.0, True),
        ]
        bars = compute_delta_bars(_trades_df(rows), "1m")
        assert len(bars) == 2
        assert bars["timestamp"].tolist() == [_BASE_TS, _BASE_TS + _MIN_MS]
        assert bars["delta"].tolist() == [3.0, -3.0]

    def test_cvd_is_cumulative(self):
        rows = [
            (_BASE_TS, 100.0, 5.0, False),
            (_BASE_TS + _MIN_MS, 100.0, 2.0, True),
            (_BASE_TS + 2 * _MIN_MS, 100.0, 1.0, False),
        ]
        bars = compute_delta_bars(_trades_df(rows), "1m")
        assert bars["cvd"].tolist() == [5.0, 3.0, 4.0]

    def test_initial_cvd_offsets_series(self):
        rows = [(_BASE_TS, 100.0, 5.0, False)]
        bars = compute_delta_bars(_trades_df(rows), "1m", initial_cvd=-20.0)
        assert bars["cvd"].iloc[0] == -15.0

    def test_unsorted_input_is_sorted(self):
        rows = [
            (_BASE_TS + _MIN_MS, 102.0, 3.0, True),
            (_BASE_TS, 100.0, 1.0, False),
        ]
        bars = compute_delta_bars(_trades_df(rows), "1m")
        assert bars["timestamp"].is_monotonic_increasing

    def test_symbol_stamped(self):
        bars = compute_delta_bars(_trades_df([(_BASE_TS, 1.0, 1.0, False)]), "1m", symbol="ETHUSDT")
        assert bars["symbol"].iloc[0] == "ETHUSDT"


class TestStreamingVectorizedParity:
    """Both code paths must produce identical bars for identical input."""

    def _random_trades(self, n: int = 500, seed: int = 7) -> list[tuple[int, float, float, bool]]:
        rng = np.random.default_rng(seed)
        rows = []
        ts = _BASE_TS
        price = 100.0
        for _ in range(n):
            ts += int(rng.integers(500, 20_000))
            price += float(rng.normal(0, 0.5))
            rows.append((ts, round(price, 4), float(rng.integers(1, 50)), bool(rng.integers(0, 2))))
        return rows

    @pytest.mark.parametrize("timeframe", ["1m", "5m", "15m"])
    def test_parity(self, timeframe: str):
        rows = self._random_trades()
        calc = CVDCalculator("BTCUSDT", timeframe)
        streamed: list[DeltaBar] = []
        for ts, price, qty, maker in rows:
            bar = calc.update(_tick(price, qty, maker, ts))
            if bar is not None:
                streamed.append(bar)
        last = calc.flush()
        if last is not None:
            streamed.append(last)

        vector = compute_delta_bars(_trades_df(rows), timeframe, symbol="BTCUSDT")

        assert len(streamed) == len(vector)
        for i, bar in enumerate(streamed):
            row = vector.iloc[i]
            assert bar.timestamp == int(row["timestamp"])
            assert bar.open == pytest.approx(row["open"])
            assert bar.high == pytest.approx(row["high"])
            assert bar.low == pytest.approx(row["low"])
            assert bar.close == pytest.approx(row["close"])
            assert bar.buy_volume == pytest.approx(row["buy_volume"])
            assert bar.sell_volume == pytest.approx(row["sell_volume"])
            assert bar.delta == pytest.approx(row["delta"])
            assert bar.delta_high == pytest.approx(row["delta_high"])
            assert bar.delta_low == pytest.approx(row["delta_low"])
            assert bar.cvd == pytest.approx(row["cvd"])
            assert bar.trade_count == int(row["trade_count"])


class TestDeltaRatio:
    def test_ratio_bounds(self):
        bars = pd.DataFrame({"delta": [5.0, -5.0, 0.0], "volume": [5.0, 5.0, 5.0]})
        assert delta_ratio(bars).tolist() == [1.0, -1.0, 0.0]

    def test_zero_volume_is_zero_not_nan(self):
        bars = pd.DataFrame({"delta": [0.0], "volume": [0.0]})
        assert delta_ratio(bars).tolist() == [0.0]


def _synthetic_bars(n: int, price_drift: float, cvd_drift: float, seed: int = 3) -> pd.DataFrame:
    """Bars with controlled price/CVD drift plus noise (noise → non-zero std)."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(price_drift + rng.normal(0, 0.2, n))
    cvd = np.cumsum(cvd_drift + rng.normal(0, 0.2, n))
    return pd.DataFrame({
        "timestamp": [_BASE_TS + i * _MIN_MS for i in range(n)],
        "open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
        "buy_volume": 10.0, "sell_volume": 10.0, "volume": 20.0,
        "delta": np.diff(cvd, prepend=0.0),
        "delta_high": 0.0, "delta_low": 0.0,
        "cvd": cvd, "trade_count": 10,
    })


class TestDetectDivergences:
    def test_empty_input(self):
        out = detect_divergences(pd.DataFrame(columns=["close", "cvd", "timestamp"]))
        assert out.empty
        assert list(out.columns) == [
            "timestamp", "kind", "price_z", "cvd_z", "strength", "close", "cvd",
        ]

    def test_too_few_bars(self):
        bars = _synthetic_bars(10, 1.0, -1.0)
        assert detect_divergences(bars, lookback=20).empty

    def test_rejects_bad_lookback(self):
        with pytest.raises(ValueError, match="lookback"):
            detect_divergences(_synthetic_bars(50, 1.0, -1.0), lookback=0)

    def test_price_up_cvd_down_is_bearish(self):
        bars = _synthetic_bars(300, price_drift=0.5, cvd_drift=-0.5)
        out = detect_divergences(bars, lookback=20, z_threshold=0.5)
        assert not out.empty
        assert (out["kind"] == "bearish").all()

    def test_price_down_cvd_up_is_bullish(self):
        bars = _synthetic_bars(300, price_drift=-0.5, cvd_drift=0.5)
        out = detect_divergences(bars, lookback=20, z_threshold=0.5)
        assert not out.empty
        assert (out["kind"] == "bullish").all()

    def test_aligned_price_and_cvd_gives_no_divergence(self):
        bars = _synthetic_bars(300, price_drift=0.5, cvd_drift=0.5)
        out = detect_divergences(bars, lookback=20, z_threshold=0.5)
        assert out.empty

    def test_higher_threshold_is_stricter(self):
        bars = _synthetic_bars(300, price_drift=0.5, cvd_drift=-0.5)
        loose = detect_divergences(bars, lookback=20, z_threshold=0.5)
        tight = detect_divergences(bars, lookback=20, z_threshold=3.0)
        assert len(tight) <= len(loose)

    def test_flat_series_produces_no_infinities(self):
        n = 100
        bars = pd.DataFrame({
            "timestamp": [_BASE_TS + i * _MIN_MS for i in range(n)],
            "close": [100.0] * n,
            "cvd": [0.0] * n,
        })
        out = detect_divergences(bars, lookback=20)
        assert out.empty

    def test_strength_is_min_of_legs(self):
        bars = _synthetic_bars(300, price_drift=0.5, cvd_drift=-0.5)
        out = detect_divergences(bars, lookback=20, z_threshold=0.5)
        expected = np.minimum(out["price_z"].abs(), out["cvd_z"].abs())
        assert np.allclose(out["strength"], expected)


class TestDetectAbsorption:
    def _bars(self, deltas, ranges) -> pd.DataFrame:
        n = len(deltas)
        close = np.full(n, 100.0)
        return pd.DataFrame({
            "timestamp": [_BASE_TS + i * _MIN_MS for i in range(n)],
            "open": close,
            "high": close + np.array(ranges) / 2,
            "low": close - np.array(ranges) / 2,
            "close": close,
            "delta": deltas,
            "volume": [100.0] * n,
            "cvd": np.cumsum(deltas),
        })

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["high", "low", "delta", "volume", "timestamp"])
        out = detect_absorption(empty)
        assert out.empty
        assert "kind" in out.columns

    def test_rejects_bad_window(self):
        with pytest.raises(ValueError, match="range_window"):
            detect_absorption(self._bars([0.0] * 30, [1.0] * 30), range_window=0)

    def test_heavy_selling_tiny_range_is_sell_absorbed(self):
        deltas = [0.0] * 30 + [-60.0]
        ranges = [1.0] * 30 + [0.1]
        out = detect_absorption(self._bars(deltas, ranges))
        assert len(out) == 1
        assert out["kind"].iloc[0] == "sell_absorbed"

    def test_heavy_buying_tiny_range_is_buy_absorbed(self):
        deltas = [0.0] * 30 + [60.0]
        ranges = [1.0] * 30 + [0.1]
        out = detect_absorption(self._bars(deltas, ranges))
        assert out["kind"].iloc[0] == "buy_absorbed"

    def test_big_delta_with_big_range_is_not_absorption(self):
        deltas = [0.0] * 30 + [60.0]
        ranges = [1.0] * 30 + [5.0]
        assert detect_absorption(self._bars(deltas, ranges)).empty

    def test_small_delta_with_tiny_range_is_not_absorption(self):
        deltas = [0.0] * 30 + [1.0]
        ranges = [1.0] * 30 + [0.1]
        assert detect_absorption(self._bars(deltas, ranges)).empty
