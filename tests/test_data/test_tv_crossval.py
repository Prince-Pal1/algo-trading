"""Phase 3 — TradingView cross-validation tests.

Compares our _compute_indicators() output against TradingView's indicator values
captured from a live chart. This catches bugs in the `ta` library itself that
hand-calculated tests cannot (since both use the same formula).

Fixture: tests/fixtures/tv_reference_btcusdt_1h.json
    Captured via scripts/capture_tv_reference.py (run manually with TV Desktop open).

All tests skip gracefully if the fixture file doesn't exist.

Expected fixture format:
{
    "captured_at": "2026-04-12T...",
    "symbol": "BTCUSDT",
    "timeframe": "1H",
    "ohlcv": [
        {"open": ..., "high": ..., "low": ..., "close": ..., "volume": ..., "timestamp": ...},
        ...
    ],
    "indicators": {
        "RSI_14": 55.3,
        "EMA_9": 83200.5,
        "EMA_21": 82800.1,
        "BBU_20": 84500.0,
        "BBM_20": 83100.0,
        "BBL_20": 81700.0,
        "MACD": 150.2,
        "MACD_signal": 120.5,
        "ATR_14": 450.3,
        "ADX_14": 28.5,
        "STOCHk_14": 65.2
    }
}
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.data.feature_engine import _compute_indicators

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "tv_reference_btcusdt_1h.json"

# Skip all tests if fixture doesn't exist
pytestmark = pytest.mark.skipif(
    not FIXTURE_PATH.exists(),
    reason=f"TV reference fixture not found: {FIXTURE_PATH}. "
           "Run scripts/capture_tv_reference.py with TradingView Desktop open.",
)

# Tolerances per indicator (absolute values).
# Values are pinned to a CLOSED historical bar (reference_bar_index in the fixture),
# so tolerances can be extremely tight — 0.01 captures rounding at the 2nd decimal.
# The TV Data Window displays 2 decimals, so 0.01 is the natural precision floor.
TOLERANCES = {
    "EMA_9": 0.01,
    "RSI_14": 0.01,
    "BBU_20": 0.01,
    "BBM_20": 0.01,
    "BBL_20": 0.01,
    "MACD": 0.01,
    "MACD_signal": 0.01,
    "MACD_hist": 0.01,
    "ATR_14": 0.01,
    "ADX_14": 0.0001,   # TV shows 4 decimals for DMI
    "DI+_14": 0.0001,
    "DI-_14": 0.0001,
    "STOCHd_14": 0.01,
}


@pytest.fixture(scope="module")
def tv_data():
    """Load the TradingView reference fixture."""
    with open(FIXTURE_PATH) as f:
        data = json.load(f)

    df = pd.DataFrame(data["ohlcv"])
    indicators = data["indicators"]
    # Pinned closed-bar index — values are read at this row, not iloc[-1]
    reference_index = data.get("reference_bar_index", len(df) - 1)
    return df, indicators, reference_index


@pytest.fixture(scope="module")
def computed_indicators(tv_data):
    """Compute indicators on the TV OHLCV data using our engine."""
    df, _, _ = tv_data
    indicator_list = [
        "ema_9", "rsi_14", "bbands_20",
        "macd", "atr_14", "adx_14", "stoch_14",
    ]
    result = _compute_indicators(df.copy(), indicator_list)
    return result


class TestTVCrossValidation:
    """Compare our computed indicators against TradingView's values."""

    @pytest.mark.parametrize("indicator", [
        "EMA_9", "RSI_14",
        "BBU_20", "BBM_20", "BBL_20",
        "MACD", "MACD_signal", "MACD_hist",
        "ATR_14", "ADX_14", "DI+_14", "DI-_14",
        "STOCHd_14",
    ])
    def test_indicator_vs_tv(self, computed_indicators, tv_data, indicator):
        """Each indicator should be bit-exact match at the pinned closed reference bar.

        Values were captured manually from TradingView Data Window on a closed bar,
        so there is no live-forming drift. Tolerance is effectively rounding at 2nd decimal.
        """
        _, tv_indicators, reference_index = tv_data
        df = computed_indicators

        if indicator not in tv_indicators:
            pytest.skip(f"{indicator} not in TV fixture data")

        if indicator not in df.columns:
            pytest.fail(f"{indicator} not computed by _compute_indicators")

        our_value = df[indicator].iloc[reference_index]
        tv_value = tv_indicators[indicator]
        tolerance = TOLERANCES.get(indicator, 0.01)

        assert our_value == pytest.approx(tv_value, abs=tolerance), \
            f"{indicator} @ reference_bar[{reference_index}]: " \
            f"ours={our_value:.4f}, TV={tv_value:.4f}, " \
            f"diff={abs(our_value - tv_value):.4f}, tolerance={tolerance}"


class TestOHLCVAlignment:
    """Verify the OHLCV data from TV is well-formed."""

    def test_ohlcv_has_required_columns(self, tv_data):
        df, _, _ = tv_data
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in df.columns, f"Missing column: {col}"

    def test_ohlcv_high_gte_low(self, tv_data):
        df, _, _ = tv_data
        violations = df[df["high"] < df["low"]]
        assert len(violations) == 0, f"{len(violations)} bars have high < low"

    def test_ohlcv_no_zero_prices(self, tv_data):
        df, _, _ = tv_data
        for col in ["open", "high", "low", "close"]:
            zeros = df[df[col] <= 0]
            assert len(zeros) == 0, f"{len(zeros)} bars have {col} <= 0"
