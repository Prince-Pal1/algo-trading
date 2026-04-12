"""Tests for fat finger, position limits, and duplicate filter."""

import time

import pytest

from src.risk.config import RiskConfig
from src.risk.duplicate_filter import DuplicateFilter
from src.risk.fat_finger import FatFingerGuard
from src.risk.position_limits import PositionLimits
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction

from tests.test_risk.test_kill_switch import _make_state


def _make_signal(
    action: SignalAction = SignalAction.LONG,
    entry_price: float = 50000.0,
    stop_loss: float = 49000.0,
    risk_pct: float = 0.01,
    strategy: str = "test_strat",
    symbol: str = "BTCUSDT",
) -> Signal:
    return Signal(
        symbol=symbol, action=action, confidence=0.9,
        strategy_name=strategy, timeframe="1h",
        entry_price=entry_price, stop_loss=stop_loss, risk_pct=risk_pct,
    )


# ── Fat Finger Tests ──


class TestFatFingerGuard:
    def test_close_always_passes(self):
        cfg = RiskConfig()
        state = _make_state()
        ff = FatFingerGuard(cfg, state)
        assert ff.check(_make_signal(action=SignalAction.CLOSE)) is None

    def test_normal_order_passes(self):
        cfg = RiskConfig(fat_finger_max_value=10_000.0)
        state = _make_state()
        # 1% risk, $1000 SL distance → small notional
        ff = FatFingerGuard(cfg, state)
        assert ff.check(_make_signal(risk_pct=0.01)) is None

    def test_huge_notional_rejected(self):
        cfg = RiskConfig(fat_finger_max_value=1_000.0)
        state = _make_state()
        state.current_equity = 100_000.0
        # risk_pct=0.10, SL distance=$1000, → qty=10, notional=$500K
        ff = FatFingerGuard(cfg, state)
        result = ff.check(_make_signal(risk_pct=0.10))
        assert result is not None
        assert "FAT_FINGER_NOTIONAL" in result

    def test_price_deviation_rejected(self):
        cfg = RiskConfig()
        state = _make_state()
        ff = FatFingerGuard(cfg, state)
        ff.update_price("BTCUSDT", 50000.0)
        # 5% deviation
        result = ff.check(_make_signal(entry_price=52600.0, stop_loss=51600.0))
        assert result is not None
        assert "FAT_FINGER_PRICE" in result

    def test_price_within_threshold_passes(self):
        cfg = RiskConfig()
        state = _make_state()
        ff = FatFingerGuard(cfg, state)
        ff.update_price("BTCUSDT", 50000.0)
        # 0.5% deviation — within 2% threshold
        assert ff.check(_make_signal(entry_price=50250.0, stop_loss=49250.0)) is None

    def test_quantity_multiplier_check(self):
        cfg = RiskConfig(fat_finger_max_qty_mult=5.0, fat_finger_max_value=1_000_000)
        state = _make_state()
        state.current_equity = 100_000.0
        ff = FatFingerGuard(cfg, state)
        # Set avg trade size very small
        ff.update_avg_trade_size(0.001)
        ff.update_avg_trade_size(0.001)
        # entry=50000, SL=49000, risk_pct=0.10 → qty = (100K*0.10)/1000 = 10
        # 10 >> 5 * 0.001 = 0.005
        result = ff.check(_make_signal(risk_pct=0.10))
        assert result is not None
        assert "FAT_FINGER_QTY" in result


# ── Position Limits Tests ──


class TestPositionLimits:
    def test_normal_position_passes(self):
        cfg = RiskConfig(max_position_pct=0.20, max_portfolio_heat=0.50)
        state = _make_state()
        pl = PositionLimits(cfg, state)
        # entry=100, SL=90 → risk_per_unit=10, qty=10, notional=$1000 = 10% of $10K
        assert pl.check(_make_signal(entry_price=100.0, stop_loss=90.0, risk_pct=0.01)) is None

    def test_close_always_passes(self):
        cfg = RiskConfig()
        state = _make_state()
        pl = PositionLimits(cfg, state)
        assert pl.check(_make_signal(action=SignalAction.CLOSE)) is None

    def test_oversized_position_rejected(self):
        cfg = RiskConfig(max_position_pct=0.05)
        state = _make_state()
        state.current_equity = 10_000
        # risk_pct=0.10, SL distance=1000 → qty=1, notional=50000 = 500% of equity
        pl = PositionLimits(cfg, state)
        result = pl.check(_make_signal(risk_pct=0.10))
        assert result is not None
        assert "POSITION_LIMIT_SYMBOL" in result

    def test_portfolio_heat_rejected(self):
        cfg = RiskConfig(max_position_pct=1.0, max_portfolio_heat=0.10)
        state = _make_state()
        state.current_equity = 10_000
        # Add existing positions to push heat up
        from src.risk.state import PositionRiskInfo
        state.open_positions["ETHUSDT"] = PositionRiskInfo(
            "ETHUSDT", "strat", "LONG", 1.0, 900.0)
        pl = PositionLimits(cfg, state)
        result = pl.check(_make_signal(risk_pct=0.01))
        assert result is not None
        assert "POSITION_LIMIT_HEAT" in result

    def test_zero_equity_rejected(self):
        cfg = RiskConfig()
        state = _make_state()
        state.current_equity = 0
        pl = PositionLimits(cfg, state)
        result = pl.check(_make_signal())
        assert result is not None
        assert "zero equity" in result


# ── Duplicate Filter Tests ──


class TestDuplicateFilter:
    def test_first_signal_passes(self):
        df = DuplicateFilter(cooldown_seconds=30)
        assert df.check(_make_signal()) is None

    def test_duplicate_within_cooldown_rejected(self):
        df = DuplicateFilter(cooldown_seconds=30)
        df.check(_make_signal())  # first pass
        result = df.check(_make_signal())  # duplicate
        assert result is not None
        assert "DUPLICATE" in result

    def test_different_symbol_passes(self):
        df = DuplicateFilter(cooldown_seconds=30)
        df.check(_make_signal(symbol="BTCUSDT"))
        assert df.check(_make_signal(symbol="ETHUSDT")) is None

    def test_different_action_passes(self):
        df = DuplicateFilter(cooldown_seconds=30)
        df.check(_make_signal(action=SignalAction.LONG))
        assert df.check(_make_signal(action=SignalAction.SHORT)) is None

    def test_close_never_duplicate(self):
        df = DuplicateFilter(cooldown_seconds=30)
        df.check(_make_signal(action=SignalAction.CLOSE))
        assert df.check(_make_signal(action=SignalAction.CLOSE)) is None

    def test_different_strategy_passes(self):
        df = DuplicateFilter(cooldown_seconds=30)
        df.check(_make_signal(strategy="strat_a"))
        assert df.check(_make_signal(strategy="strat_b")) is None
