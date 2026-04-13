"""Unit tests for BinanceFundingDownloader's response parser.

Doesn't hit the real Binance API — feeds mock JSON responses to the
`_parse_funding` helper and asserts the DataFrame shape.
"""

from __future__ import annotations

import pandas as pd

from src.data.downloader import _parse_funding


class TestParseFunding:
    def test_empty_response(self):
        df = _parse_funding([])
        assert len(df) == 0

    def test_single_record(self):
        raw = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00010000",
                "fundingTime": 1698220800000,
                "markPrice": "34820.5",
            }
        ]
        df = _parse_funding(raw)
        assert len(df) == 1
        assert df["symbol"].iloc[0] == "BTCUSDT"
        assert df["funding_rate"].iloc[0] == 0.0001
        assert df["timestamp"].iloc[0] == 1698220800000
        assert df["mark_price"].iloc[0] == 34820.5

    def test_multiple_records_and_dtypes(self):
        raw = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00008000",
                "fundingTime": 1698192000000,
                "markPrice": "34000.0",
            },
            {
                "symbol": "BTCUSDT",
                "fundingRate": "-0.00005000",
                "fundingTime": 1698220800000,
                "markPrice": "34100.0",
            },
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00015000",
                "fundingTime": 1698249600000,
                "markPrice": "34200.0",
            },
        ]
        df = _parse_funding(raw)
        assert len(df) == 3
        # Negative funding is preserved
        assert df["funding_rate"].iloc[1] == -0.00005
        # Types
        assert pd.api.types.is_integer_dtype(df["timestamp"])
        assert pd.api.types.is_float_dtype(df["funding_rate"])
        assert pd.api.types.is_float_dtype(df["mark_price"])

    def test_missing_mark_price_handled(self):
        raw = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0001",
                "fundingTime": 1698220800000,
                # markPrice absent
            }
        ]
        df = _parse_funding(raw)
        assert df["mark_price"].iloc[0] == 0.0
