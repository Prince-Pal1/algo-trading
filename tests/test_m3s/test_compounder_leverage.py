"""Tests for the G.0c.3 Compounder.target_leverage helper."""

from __future__ import annotations

import pytest

from src.m3s.compounder import Compounder
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.m3s.types import PortfolioSnapshot


def _snapshot(drawdown_pct: float, equity: float = 10_000.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts_ms=1_700_000_000_000,
        equity=equity,
        hwm=equity / (1.0 - drawdown_pct) if drawdown_pct > 0 else equity,
        drawdown_pct=drawdown_pct,
        per_strategy={},
        signal_corr={},
    )


def _compounder(mode: M3SMode) -> Compounder:
    tracker = PortfolioTracker(initial_equity=10_000.0)
    return Compounder(mode=MODE_PRESETS[mode], tracker=tracker)


class TestDefaultCryptoRange:
    def test_no_leverage_returns_one(self):
        """Range (1, 1) — classic crypto default — always returns 1.0."""
        c = _compounder(M3SMode.STANDARD)
        assert c.target_leverage((1.0, 1.0), _snapshot(0.0)) == 1.0
        assert c.target_leverage((1.0, 1.0), _snapshot(0.05)) == 1.0
        assert c.target_leverage((1.0, 1.0), _snapshot(0.15)) == 1.0


class TestConservativeMode:
    def test_always_returns_min(self):
        c = _compounder(M3SMode.CONSERVATIVE)
        assert c.target_leverage((10.0, 200.0), _snapshot(0.0)) == 10.0
        assert c.target_leverage((10.0, 200.0), _snapshot(0.10)) == 10.0
        assert c.target_leverage((5.0, 100.0), _snapshot(0.03)) == 5.0


class TestStandardMode:
    def test_low_dd_returns_middle(self):
        c = _compounder(M3SMode.STANDARD)
        result = c.target_leverage((10.0, 200.0), _snapshot(0.01))
        # (10 + 200) / 2 = 105
        assert result == pytest.approx(105.0)

    def test_high_dd_returns_min(self):
        c = _compounder(M3SMode.STANDARD)
        result = c.target_leverage((10.0, 200.0), _snapshot(0.05))
        # drawdown > 2% → min
        assert result == 10.0


class TestGrowthMode:
    def test_calm_returns_max(self):
        c = _compounder(M3SMode.GROWTH)
        result = c.target_leverage((10.0, 200.0), _snapshot(0.02))
        # drawdown < 5% → max
        assert result == 200.0

    def test_moderate_dd_returns_middle(self):
        c = _compounder(M3SMode.GROWTH)
        result = c.target_leverage((10.0, 200.0), _snapshot(0.07))
        # 5% < dd < 10% → middle
        assert result == pytest.approx(105.0)

    def test_deep_dd_returns_min(self):
        c = _compounder(M3SMode.GROWTH)
        result = c.target_leverage((10.0, 200.0), _snapshot(0.15))
        # dd >= 10% → min
        assert result == 10.0


class TestRangeSanitization:
    def test_min_clamped_to_one(self):
        c = _compounder(M3SMode.CONSERVATIVE)
        # Passing (0.5, 10) — invalid min — should clamp to 1
        result = c.target_leverage((0.5, 10.0), _snapshot(0.0))
        assert result >= 1.0

    def test_inverted_range_clamped(self):
        c = _compounder(M3SMode.STANDARD)
        # Passing (100, 10) — inverted — should clamp hi to lo and return lo
        result = c.target_leverage((100.0, 10.0), _snapshot(0.0))
        assert result == 100.0

    def test_never_exceeds_max(self):
        for mode in [M3SMode.CONSERVATIVE, M3SMode.STANDARD, M3SMode.GROWTH]:
            c = _compounder(mode)
            result = c.target_leverage((10.0, 50.0), _snapshot(0.01))
            assert result <= 50.0, f"mode {mode} returned {result} > 50"

    def test_never_below_min(self):
        for mode in [M3SMode.CONSERVATIVE, M3SMode.STANDARD, M3SMode.GROWTH]:
            c = _compounder(mode)
            result = c.target_leverage((25.0, 100.0), _snapshot(0.20))
            assert result >= 25.0, f"mode {mode} returned {result} < 25"
