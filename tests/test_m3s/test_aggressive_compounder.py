from __future__ import annotations

import pytest

from src.m3s.aggressive_compounder import (
    AggressiveCompounderConfig,
    AggressiveRetailCompounder,
)


_BASE_TS = 1_700_000_000_000  # some Tuesday
_MS_PER_DAY = 86_400_000
_MS_PER_WEEK = 7 * _MS_PER_DAY


def _build(equity: float = 10_000.0, **cfg_kwargs) -> AggressiveRetailCompounder:
    return AggressiveRetailCompounder(
        initial_equity=equity,
        config=AggressiveCompounderConfig(**cfg_kwargs),
    )


class TestSizing:
    def test_default_5_percent(self):
        comp = _build(10_000.0)
        assert comp.size_next_trade(open_positions=0) == pytest.approx(500.0)

    def test_override_at_high_conviction(self):
        comp = _build(10_000.0, position_size_pct=0.05, override_cap_pct=0.10)
        # conviction 1.0 + allow_size_override → bumped to min(0.10, 0.05*1.5) = 0.075
        size = comp.size_next_trade(conviction=1.0)
        assert size == pytest.approx(750.0)

    def test_override_respects_cap(self):
        comp = _build(10_000.0, position_size_pct=0.08, override_cap_pct=0.10)
        size = comp.size_next_trade(conviction=1.0)
        assert size == pytest.approx(1000.0)  # capped at 0.10

    def test_size_zero_at_max_concurrent(self):
        comp = _build(10_000.0, max_concurrent_positions=2)
        assert comp.size_next_trade(open_positions=2) == 0.0


class TestKillSwitches:
    def test_can_trade_at_start(self):
        comp = _build(10_000.0)
        ok, reason = comp.can_trade(_BASE_TS)
        assert ok is True
        assert reason == "ok"

    def test_daily_loss_halt(self):
        comp = _build(10_000.0, daily_loss_limit_pct=0.30)
        comp.update_equity(10_000.0, _BASE_TS)
        # Drop 35% within the same day
        comp.update_equity(6_500.0, _BASE_TS + 3600_000)
        ok, reason = comp.can_trade(_BASE_TS + 3600_000)
        assert ok is False
        assert reason == "daily_loss_limit"

    def test_daily_loss_resets_next_day(self):
        comp = _build(10_000.0, daily_loss_limit_pct=0.30)
        comp.update_equity(10_000.0, _BASE_TS)
        comp.update_equity(6_500.0, _BASE_TS + 3600_000)
        ok, _ = comp.can_trade(_BASE_TS + 3600_000)
        assert ok is False
        # Next day rollover
        next_day = _BASE_TS + _MS_PER_DAY + 100
        comp.update_equity(6_500.0, next_day)
        ok, reason = comp.can_trade(next_day)
        # Total DD still 35% < 50% → ok, daily just reset
        assert ok is True

    def test_total_drawdown_halt_triggers_cooldown(self):
        comp = _build(10_000.0, max_drawdown_pct=0.50, cooldown_days_after_halt=7)
        comp.update_equity(4_800.0, _BASE_TS + 3600_000)  # 52% DD
        ok, reason = comp.can_trade(_BASE_TS + 3600_000)
        assert ok is False
        assert reason == "max_drawdown_halt"
        # During cooldown still halted
        ok, reason = comp.can_trade(_BASE_TS + 3 * _MS_PER_DAY)
        assert ok is False
        assert reason == "cooldown"

    def test_zero_equity_permanent_halt(self):
        comp = _build(10_000.0)
        comp.update_equity(0.0, _BASE_TS + 3600_000)
        ok, reason = comp.can_trade(_BASE_TS + 3600_000)
        assert ok is False
        assert reason == "sub_book_zeroed"


class TestWeeklyRefund:
    def test_refund_tops_up_when_main_is_flat(self):
        comp = _build(3_000.0)
        comp.update_equity(1_500.0, _BASE_TS)  # sub-book lost half
        # Main account is flat → refund back to 3000
        refund = comp.weekly_refund(main_account_equity=3_000.0, ts_ms=_BASE_TS + _MS_PER_WEEK)
        assert refund == pytest.approx(1_500.0)
        assert comp.state.current_equity == pytest.approx(3_000.0)

    def test_no_refund_within_same_week(self):
        comp = _build(3_000.0)
        comp.update_equity(1_500.0, _BASE_TS)
        # First refund (far in the future so the weekly gate doesn't block)
        comp.weekly_refund(main_account_equity=3_000.0, ts_ms=_BASE_TS + _MS_PER_WEEK)
        # Second refund only 2 days later should be blocked
        refund = comp.weekly_refund(main_account_equity=3_000.0, ts_ms=_BASE_TS + _MS_PER_WEEK + 2 * _MS_PER_DAY)
        assert refund == 0.0

    def test_refund_skipped_when_main_is_drawn_down(self):
        comp = _build(3_000.0)
        comp.update_equity(1_500.0, _BASE_TS)
        # Main account is also drawn down (2000 < 3000 initial)
        # Target = 2000 * 1.0 = 2000; current 1500 → refund 500 allowed
        # But if main is flat/down, the refund should still top up to main × cap
        refund = comp.weekly_refund(main_account_equity=2_000.0, ts_ms=_BASE_TS + _MS_PER_WEEK)
        assert refund == pytest.approx(500.0)

    def test_refund_disabled_returns_zero(self):
        comp = _build(3_000.0, weekly_refund_enabled=False)
        comp.update_equity(1_500.0, _BASE_TS)
        refund = comp.weekly_refund(main_account_equity=3_000.0, ts_ms=_BASE_TS + _MS_PER_WEEK)
        assert refund == 0.0
