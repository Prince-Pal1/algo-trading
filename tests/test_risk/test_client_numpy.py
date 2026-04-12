"""Tests for RiskClient numpy serialization — enc_hook for numpy scalars."""

from __future__ import annotations

import pytest
import numpy as np
import msgspec

from src.risk.client import _numpy_enc_hook
from src.utils.types import Signal, SignalAction


class TestNumpyEncHook:
    def test_numpy_float64(self):
        """numpy.float64(3.14) → Python float 3.14."""
        result = _numpy_enc_hook(np.float64(3.14))
        assert isinstance(result, float)
        assert result == pytest.approx(3.14)

    def test_numpy_int64(self):
        """numpy.int64(42) → Python int 42."""
        result = _numpy_enc_hook(np.int64(42))
        assert isinstance(result, int)
        assert result == 42

    def test_non_numpy_raises(self):
        """Non-numpy objects raise NotImplementedError."""
        with pytest.raises(NotImplementedError):
            _numpy_enc_hook({"not": "numpy"})


class TestSignalWithNumpyFields:
    def test_signal_with_numpy_fields_serializes(self):
        """Signal with numpy entry_price serializes without crash via enc_hook."""
        signal = Signal(
            symbol="BTCUSDT",
            action=SignalAction.LONG,
            confidence=0.8,
            strategy_name="test",
            timeframe="1h",
            entry_price=float(np.float64(50000.0)),
            stop_loss=float(np.float64(49000.0)),
            take_profit=float(np.float64(52000.0)),
        )

        encoder = msgspec.json.Encoder(enc_hook=_numpy_enc_hook)
        raw = encoder.encode(signal)
        decoded = msgspec.json.decode(raw, type=Signal)

        assert decoded.entry_price == pytest.approx(50000.0)
        assert decoded.stop_loss == pytest.approx(49000.0)

    def test_metadata_with_numpy_values(self):
        """Signal metadata containing numpy scalars serializes via enc_hook."""
        metadata = {
            "volatility": np.float64(0.0234),
            "atr": np.float64(150.5),
            "lookback": np.int64(168),
        }

        encoder = msgspec.json.Encoder(enc_hook=_numpy_enc_hook)
        raw = encoder.encode(metadata)
        decoded = msgspec.json.decode(raw)

        assert decoded["volatility"] == pytest.approx(0.0234)
        assert decoded["lookback"] == 168
