"""Tests for Kelly sizer and drawdown scaler."""

import pytest

from src.risk.config import RiskConfig
from src.risk.drawdown_scaler import DrawdownScaler
from src.risk.kelly_sizer import KellySizer, StrategyStats
from src.utils.types import Signal, SignalAction


def _make_signal(risk_pct: float = 0.01) -> Signal:
    return Signal(
        symbol="BTCUSDT", action=SignalAction.LONG, confidence=0.9,
        strategy_name="test", timeframe="1h", entry_price=50000.0,
        stop_loss=49000.0, risk_pct=risk_pct,
    )


# ── Kelly Sizer Tests ──


class TestKellySizer:
    def test_no_stats_uses_signal_risk_pct(self):
        cfg = RiskConfig(max_risk_per_trade=0.02)
        ks = KellySizer(cfg)
        result = ks.compute(_make_signal(risk_pct=0.01), stats=None)
        assert result == 0.01

    def test_insufficient_trades_fallback(self):
        cfg = RiskConfig(kelly_min_trades=30, max_risk_per_trade=0.02)
        ks = KellySizer(cfg)
        stats = StrategyStats(win_rate=0.6, avg_win=2.0, avg_loss=1.0,
                              total_trades=20)
        result = ks.compute(_make_signal(risk_pct=0.01), stats)
        assert result == 0.01  # Falls back to signal risk_pct

    def test_low_win_rate_fallback(self):
        cfg = RiskConfig(kelly_min_win_rate=0.45, max_risk_per_trade=0.02)
        ks = KellySizer(cfg)
        stats = StrategyStats(win_rate=0.3, avg_win=2.0, avg_loss=1.0,
                              total_trades=100)
        result = ks.compute(_make_signal(risk_pct=0.01), stats)
        assert result == 0.01

    def test_kelly_fraction_correct(self):
        # Kelly: f = 0.6 - 0.4/2.0 = 0.6 - 0.2 = 0.4
        # Quarter Kelly: 0.4 * 0.25 = 0.10
        cfg = RiskConfig(kelly_default_fraction=0.25, kelly_max_fraction=0.50,
                         kelly_min_trades=10, kelly_min_win_rate=0.40,
                         max_risk_per_trade=0.02)
        ks = KellySizer(cfg)
        stats = StrategyStats(win_rate=0.6, avg_win=2.0, avg_loss=1.0,
                              total_trades=50)
        result = ks.compute(_make_signal(), stats)
        # 0.4 * 0.25 = 0.10, capped at max_risk_per_trade=0.02
        assert result == pytest.approx(0.02)

    def test_kelly_with_vol_scaling(self):
        cfg = RiskConfig(kelly_default_fraction=0.25, kelly_max_fraction=0.50,
                         kelly_min_trades=10, kelly_min_win_rate=0.40,
                         max_risk_per_trade=0.05, vol_target=0.15)
        ks = KellySizer(cfg)
        stats = StrategyStats(win_rate=0.6, avg_win=2.0, avg_loss=1.0,
                              total_trades=50, realized_vol=0.30)
        result = ks.compute(_make_signal(), stats)
        # vol_scalar = 0.15/0.30 = 0.5 (min clamp)
        # Kelly = 0.4 * 0.25 = 0.10, * 0.5 = 0.05, capped at 0.05
        assert result == pytest.approx(0.05)

    def test_negative_kelly_uses_minimum(self):
        cfg = RiskConfig(kelly_min_trades=5, kelly_min_win_rate=0.10,
                         max_risk_per_trade=0.02)
        ks = KellySizer(cfg)
        # Very low win rate with bad ratio → negative Kelly
        stats = StrategyStats(win_rate=0.3, avg_win=1.0, avg_loss=2.0,
                              total_trades=50)
        result = ks.compute(_make_signal(risk_pct=0.005), stats)
        assert result <= 0.005

    def test_hard_cap_respected(self):
        cfg = RiskConfig(max_risk_per_trade=0.02)
        ks = KellySizer(cfg)
        result = ks.compute(_make_signal(risk_pct=0.10), stats=None)
        assert result == 0.02

    def test_kelly_fraction_static_method(self):
        # f = p - q/b = 0.6 - 0.4/2.0 = 0.4
        assert KellySizer._kelly_fraction(0.6, 2.0, 1.0) == pytest.approx(0.4)
        # f = 0.5 - 0.5/1.0 = 0.0 (break even)
        assert KellySizer._kelly_fraction(0.5, 1.0, 1.0) == pytest.approx(0.0)
        # Edge: zero values
        assert KellySizer._kelly_fraction(0.5, 0.0, 1.0) == 0.0
        assert KellySizer._kelly_fraction(0.5, 1.0, 0.0) == 0.0

    def test_compute_stats_from_trades(self):
        trades = [
            {"pnl": 100}, {"pnl": 200}, {"pnl": -50}, {"pnl": 150}, {"pnl": -100},
        ]
        stats = KellySizer.compute_stats_from_trades(trades)
        assert stats.total_trades == 5
        assert stats.win_rate == pytest.approx(0.6)
        assert stats.avg_win == pytest.approx(150.0)
        assert stats.avg_loss == pytest.approx(75.0)

    def test_compute_stats_empty(self):
        stats = KellySizer.compute_stats_from_trades([])
        assert stats.total_trades == 0
        assert stats.win_rate == 0.0


# ── Drawdown Scaler Tests ──


class TestDrawdownScaler:
    def test_no_drawdown_full_size(self):
        cfg = RiskConfig(max_drawdown=0.15)
        ds = DrawdownScaler(cfg)
        assert ds.adjust(0.01, 0.0) == 0.01

    def test_half_drawdown_half_size(self):
        cfg = RiskConfig(max_drawdown=0.15)
        ds = DrawdownScaler(cfg)
        result = ds.adjust(0.01, 0.075)  # 50% of max DD
        assert result == pytest.approx(0.005)

    def test_max_drawdown_zero_size(self):
        cfg = RiskConfig(max_drawdown=0.15)
        ds = DrawdownScaler(cfg)
        assert ds.adjust(0.01, 0.15) == pytest.approx(0.0)

    def test_over_max_drawdown_zero_size(self):
        cfg = RiskConfig(max_drawdown=0.15)
        ds = DrawdownScaler(cfg)
        assert ds.adjust(0.01, 0.20) == 0.0

    def test_negative_drawdown_full_size(self):
        cfg = RiskConfig(max_drawdown=0.15)
        ds = DrawdownScaler(cfg)
        assert ds.adjust(0.01, -0.05) == 0.01

    def test_zero_max_drawdown_full_size(self):
        cfg = RiskConfig(max_drawdown=0.0)
        ds = DrawdownScaler(cfg)
        assert ds.adjust(0.01, 0.05) == 0.01

    def test_linear_scaling(self):
        cfg = RiskConfig(max_drawdown=0.20)
        ds = DrawdownScaler(cfg)
        # 25% of max DD → 75% size
        assert ds.adjust(0.04, 0.05) == pytest.approx(0.03)
        # 75% of max DD → 25% size
        assert ds.adjust(0.04, 0.15) == pytest.approx(0.01)
