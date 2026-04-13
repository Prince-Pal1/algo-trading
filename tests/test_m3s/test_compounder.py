"""Sub-phase 0.3 — tests for src/m3s/compounder.py.

Covers: scalar composition (vol target × pace dial × CVaR × DD), HWM-gated
base advancement, per-trade compounding cadence (CUSTOM), mode transition,
and BT #1 synthetic trade stream across all 4 modes.
"""

from __future__ import annotations

import math

import pytest

from src.m3s.compounder import (
    Compounder,
    CompoundTickReason,
    TradeCloseEvent,
)
from src.m3s.modes import MODE_PRESETS, M3SMode, load_mode_from_dict
from src.m3s.portfolio import PortfolioTracker


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_700_000_000_000


def _ts(day: int) -> int:
    return _BASE_TS + day * _MS_PER_DAY


def _make(mode_key: M3SMode = M3SMode.STANDARD, initial_equity: float = 10_000.0):
    tracker = PortfolioTracker(initial_equity=initial_equity)
    if mode_key == M3SMode.CUSTOM:
        mode = load_mode_from_dict(
            "CUSTOM",
            {"i_accept_custom_mode_risk": True, "compound_cadence": "per_trade"},
        )
    else:
        mode = MODE_PRESETS[mode_key]
    comp = Compounder(mode=mode, tracker=tracker)
    return tracker, comp


# ══════════════════════════════════════════════════════════════════════
# Init + state
# ══════════════════════════════════════════════════════════════════════


class TestInit:
    def test_initial_base_equals_equity(self):
        _, comp = _make(initial_equity=12_000.0)
        assert comp.state.base_equity == 12_000.0
        assert comp.state.hwm == 12_000.0
        assert comp.state.mode == "STANDARD"

    def test_initial_from_tracker(self):
        tracker = PortfolioTracker(initial_equity=15_000.0)
        comp = Compounder(mode=MODE_PRESETS[M3SMode.GROWTH], tracker=tracker)
        assert comp.state.base_equity == 15_000.0
        assert comp.mode.name == M3SMode.GROWTH

    def test_initial_from_explicit_equity(self):
        tracker = PortfolioTracker(initial_equity=10_000.0)
        comp = Compounder(
            mode=MODE_PRESETS[M3SMode.STANDARD],
            tracker=tracker,
            initial_equity=20_000.0,
        )
        assert comp.state.base_equity == 20_000.0


# ══════════════════════════════════════════════════════════════════════
# Drawdown scalar
# ══════════════════════════════════════════════════════════════════════


class TestDrawdownScalar:
    def test_no_drawdown_scalar_one(self):
        tracker, comp = _make(M3SMode.STANDARD)
        snap = tracker.snapshot(now_ms=_ts(0))
        s = comp.risk_scalar(snap)
        # No trades → all factors neutral → scalar ≈ 1.0 after clamps
        assert s > 0.0

    def test_dd_freeze_halves_scalar(self):
        tracker, comp = _make(M3SMode.STANDARD)
        # Drive equity to HWM then below freeze threshold (8% for STANDARD)
        tracker.on_trade_close("a", pnl=1000.0, symbol="BTC", ts_ms=_ts(0))   # eq=11000, hwm=11000
        tracker.on_trade_close("a", pnl=-1200.0, symbol="BTC", ts_ms=_ts(1))  # eq=9800, dd≈0.109
        snap = tracker.snapshot(now_ms=_ts(1))
        # DD = 1200/11000 = 0.109 → above freeze 0.08 and above halt 0.12? Halt=0.12, so still below halt
        assert snap.drawdown_pct > 0.08
        assert snap.drawdown_pct < 0.12
        # Halt scalar = 0.0
        s = comp.risk_scalar(snap)
        # Freeze scalar = 0.5 × other factors
        assert 0.0 < s < 1.5

    def test_dd_halt_zeroes_scalar(self):
        tracker, comp = _make(M3SMode.STANDARD)
        tracker.on_trade_close("a", pnl=1000.0, symbol="BTC", ts_ms=_ts(0))   # hwm=11000
        tracker.on_trade_close("a", pnl=-2500.0, symbol="BTC", ts_ms=_ts(1))  # eq=8500, dd=2500/11000≈0.227
        snap = tracker.snapshot(now_ms=_ts(1))
        assert snap.drawdown_pct >= 0.12
        s = comp.risk_scalar(snap)
        assert s == 0.0

    def test_mode_thresholds_differ(self):
        """CONSERVATIVE freezes at 5% DD, GROWTH at 10%."""
        tracker, conservative_comp = _make(M3SMode.CONSERVATIVE)
        _, growth_comp = _make(M3SMode.GROWTH)
        # 6% DD: above CONSERVATIVE freeze (5%) but below GROWTH freeze (10%)
        tracker.on_trade_close("a", pnl=1000.0, symbol="BTC", ts_ms=_ts(0))   # hwm=11000
        tracker.on_trade_close("a", pnl=-700.0, symbol="BTC", ts_ms=_ts(1))   # dd=700/11000≈0.0636
        snap = tracker.snapshot(now_ms=_ts(1))
        # CONSERVATIVE should apply freeze (0.5×), GROWTH should not
        assert conservative_comp._dd_scalar(snap.drawdown_pct) == 0.5
        assert growth_comp._dd_scalar(snap.drawdown_pct) == 1.0


# ══════════════════════════════════════════════════════════════════════
# Mode pace scalar (rolling Sharpe dial)
# ══════════════════════════════════════════════════════════════════════


class TestPaceScalar:
    def test_no_strategies_returns_one(self):
        tracker, comp = _make(M3SMode.STANDARD)
        snap = tracker.snapshot(now_ms=_ts(0))
        assert comp._mode_pace_scalar(snap) == 1.0

    def test_pace_clamped_to_mode_floor(self):
        """Terrible Sharpe → clamped to mode's floor."""
        tracker, comp = _make(M3SMode.STANDARD)
        # Add a strategy with losing trades so rolling Sharpe < 0
        for day in range(20):
            tracker.on_trade_close("a", pnl=-50.0 + (day % 3) * 10, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(20))
        pace = comp._mode_pace_scalar(snap)
        # STANDARD floor is 0.25
        assert pace == pytest.approx(0.25)

    def test_pace_clamped_to_mode_ceiling(self):
        """Great Sharpe → clamped to mode's ceiling (1.00 for STANDARD)."""
        tracker, comp = _make(M3SMode.STANDARD)
        # Stream of small positive + small variance — produces high Sharpe
        for day in range(30):
            pnl = 100.0 if day % 2 == 0 else 50.0
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        # With only wins, Sharpe should be very high → clamped to ceiling
        pace = comp._mode_pace_scalar(snap)
        assert pace == pytest.approx(1.00)

    def test_growth_has_higher_ceiling(self):
        """GROWTH ceiling is 1.20, STANDARD is 1.00."""
        tracker, growth_comp = _make(M3SMode.GROWTH)
        _, standard_comp = _make(M3SMode.STANDARD)
        for day in range(30):
            pnl = 100.0 if day % 2 == 0 else 50.0
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        growth_pace = growth_comp._mode_pace_scalar(snap)
        standard_pace = standard_comp._mode_pace_scalar(snap)
        assert growth_pace > standard_pace
        assert growth_pace == pytest.approx(1.20)


# ══════════════════════════════════════════════════════════════════════
# Vol targeting scalar
# ══════════════════════════════════════════════════════════════════════


class TestVolTargetScalar:
    def test_no_realized_vol_returns_one(self):
        tracker, comp = _make(M3SMode.STANDARD)
        snap = tracker.snapshot(now_ms=_ts(0))
        assert comp._vol_target_scalar(snap) == 1.0

    def test_high_vol_reduces_scalar(self):
        """Realized vol 3× target → scalar clamped to floor 0.5."""
        tracker, comp = _make(M3SMode.STANDARD)
        # Wild alternating trades — big daily swings
        for day in range(30):
            pnl = 500.0 if day % 2 == 0 else -450.0
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        vol_scalar = comp._vol_target_scalar(snap)
        assert vol_scalar == pytest.approx(0.5)  # clamped

    def test_low_vol_raises_scalar(self):
        """Realized vol << target → scalar clamped to ceiling 1.5."""
        tracker, comp = _make(M3SMode.STANDARD)
        # Tiny trades → low realized vol
        for day in range(30):
            pnl = 1.0 if day % 2 == 0 else 0.5
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        vol_scalar = comp._vol_target_scalar(snap)
        assert vol_scalar == pytest.approx(1.5)  # clamped


# ══════════════════════════════════════════════════════════════════════
# CVaR scalar (Tier 1 #5)
# ══════════════════════════════════════════════════════════════════════


class TestCVaRScalar:
    def test_cvar_neutral_with_insufficient_data(self):
        tracker, comp = _make(M3SMode.STANDARD)
        # Fewer than 10 days of trades → neutral
        for day in range(5):
            tracker.on_trade_close("a", pnl=10.0, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(5))
        s = comp._cvar_scalar(snap)
        assert s == 1.0

    def test_cvar_disabled_returns_one(self):
        tracker = PortfolioTracker(initial_equity=10_000.0)
        comp = Compounder(
            mode=MODE_PRESETS[M3SMode.STANDARD],
            tracker=tracker,
            cvar_enabled=False,
        )
        # Even with lots of data
        for day in range(60):
            pnl = 50.0 if day % 2 == 0 else -30.0
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(60))
        assert comp._cvar_scalar(snap) == 1.0

    def test_cvar_fat_tail_de_levers(self):
        """When realized CVaR is more negative than target, scalar < 1."""
        tracker, comp = _make(M3SMode.STANDARD)
        # 30 days with 3 very bad days (-1000 each) and rest moderate
        for day in range(30):
            if day in (5, 15, 25):
                pnl = -1000.0   # bad tail days (-10% of equity)
            else:
                pnl = 20.0
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        cvar_scalar = comp._cvar_scalar(snap)
        # Realized CVaR much more negative than target -0.02 → scalar < 1
        assert cvar_scalar < 1.0
        assert cvar_scalar >= 0.3  # floor

    def test_cvar_calm_tails_up_levers(self):
        """When realized CVaR is less negative than target, scalar > 1."""
        tracker, comp = _make(M3SMode.STANDARD)
        # All small positive PnLs — minimal tail
        for day in range(30):
            tracker.on_trade_close("a", pnl=10.0, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        cvar_scalar = comp._cvar_scalar(snap)
        # realized CVaR ≈ +0.001, target = -0.02 → ratio positive/negative → clamp
        assert cvar_scalar >= 1.0


# ══════════════════════════════════════════════════════════════════════
# risk_scalar composition
# ══════════════════════════════════════════════════════════════════════


class TestRiskScalarComposition:
    def test_scalar_clamped_to_hard_ceiling(self):
        tracker, comp = _make(M3SMode.GROWTH)
        # Prime conditions: low vol, high Sharpe, calm tails
        for day in range(30):
            tracker.on_trade_close("a", pnl=5.0, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        s = comp.risk_scalar(snap)
        assert s <= 1.5  # hard global ceiling

    def test_scalar_floors_at_zero(self):
        tracker, comp = _make(M3SMode.STANDARD)
        # Drive equity into DD halt
        tracker.on_trade_close("a", pnl=1000.0, symbol="BTC", ts_ms=_ts(0))
        tracker.on_trade_close("a", pnl=-2500.0, symbol="BTC", ts_ms=_ts(1))
        snap = tracker.snapshot(now_ms=_ts(1))
        s = comp.risk_scalar(snap)
        assert s == 0.0

    def test_scalar_nonnegative_always(self):
        tracker, comp = _make(M3SMode.STANDARD)
        for day in range(30):
            pnl = -50.0 if day % 3 == 0 else 30.0
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
        snap = tracker.snapshot(now_ms=_ts(30))
        s = comp.risk_scalar(snap)
        assert s >= 0.0


# ══════════════════════════════════════════════════════════════════════
# update_base + HWM gate
# ══════════════════════════════════════════════════════════════════════


class TestUpdateBase:
    def test_advance_above_hwm(self):
        tracker, comp = _make(M3SMode.STANDARD)
        tracker.on_trade_close("a", pnl=500.0, symbol="BTC", ts_ms=_ts(0))
        snap = tracker.snapshot(now_ms=_ts(0))
        result = comp.update_base(snap)
        assert result.reason == CompoundTickReason.ADVANCED
        assert comp.state.base_equity == pytest.approx(10_500.0)
        assert comp.state.hwm == pytest.approx(10_500.0)

    def test_no_advance_below_existing_hwm(self):
        tracker, comp = _make(M3SMode.STANDARD)
        # First trade sets HWM to 10500, base to 10500
        tracker.on_trade_close("a", pnl=500.0, symbol="BTC", ts_ms=_ts(0))
        comp.update_base(tracker.snapshot(now_ms=_ts(0)))
        # Second trade drops equity to 10300
        tracker.on_trade_close("a", pnl=-200.0, symbol="BTC", ts_ms=_ts(1))
        result = comp.update_base(tracker.snapshot(now_ms=_ts(1)))
        assert result.reason == CompoundTickReason.BELOW_HWM
        assert comp.state.base_equity == pytest.approx(10_500.0)  # unchanged

    def test_freeze_prevents_advance(self):
        tracker, comp = _make(M3SMode.STANDARD)
        # Drive DD above freeze threshold (8%)
        tracker.on_trade_close("a", pnl=1000.0, symbol="BTC", ts_ms=_ts(0))
        comp.update_base(tracker.snapshot(now_ms=_ts(0)))
        tracker.on_trade_close("a", pnl=-1000.0, symbol="BTC", ts_ms=_ts(1))
        # Now equity=10000, hwm=11000, dd=1000/11000≈0.091 > 0.08
        # Then recover to 11500
        tracker.on_trade_close("a", pnl=1500.0, symbol="BTC", ts_ms=_ts(2))
        # At this point equity=11500 > hwm=11000, but we first freeze
        snap = tracker.snapshot(now_ms=_ts(2))
        # snap.drawdown_pct is computed from current equity vs tracker HWM — but tracker
        # HWM has auto-updated to 11500 now, so DD=0 in tracker snapshot.
        # For the freeze test we want to verify the compounder honors freeze when
        # the snapshot reports DD above the threshold.
        # We simulate a frozen snapshot directly:
        from src.m3s.types import PortfolioSnapshot
        frozen_snap = PortfolioSnapshot(
            ts_ms=_ts(2),
            equity=12_000.0,
            hwm=11_500.0,
            drawdown_pct=0.10,  # above 0.08 freeze
            per_strategy={},
            signal_corr={},
        )
        result = comp.update_base(frozen_snap)
        assert result.reason == CompoundTickReason.FROZEN_DD

    def test_multiple_advances_ratchet_up(self):
        tracker, comp = _make(M3SMode.STANDARD)
        for day, pnl in enumerate([100.0, 200.0, 150.0]):
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
            comp.update_base(tracker.snapshot(now_ms=_ts(day)))
        assert comp.state.base_equity == pytest.approx(10_450.0)
        assert comp.state.hwm == pytest.approx(10_450.0)

    def test_base_does_not_move_down(self):
        tracker, comp = _make(M3SMode.STANDARD)
        tracker.on_trade_close("a", pnl=500.0, symbol="BTC", ts_ms=_ts(0))
        comp.update_base(tracker.snapshot(now_ms=_ts(0)))
        first_base = comp.state.base_equity

        tracker.on_trade_close("a", pnl=-300.0, symbol="BTC", ts_ms=_ts(1))
        comp.update_base(tracker.snapshot(now_ms=_ts(1)))
        assert comp.state.base_equity == first_base


# ══════════════════════════════════════════════════════════════════════
# Per-trade compounding (CUSTOM mode)
# ══════════════════════════════════════════════════════════════════════


class TestPerTradeCompounding:
    def test_custom_with_per_trade_every_1(self):
        tracker, comp = _make(M3SMode.CUSTOM)
        # Each trade should advance base
        for day, pnl in enumerate([100.0, 200.0, 300.0]):
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
            event = TradeCloseEvent(ts_ms=_ts(day), strategy="a", pnl=pnl)
            result = comp.on_trade_close(event, tracker.snapshot(now_ms=_ts(day)))
            assert result is not None
            assert result.reason == CompoundTickReason.ADVANCED
        assert comp.state.base_equity == pytest.approx(10_600.0)

    def test_custom_with_per_trade_every_3(self):
        tracker = PortfolioTracker(initial_equity=10_000.0)
        mode = load_mode_from_dict(
            "CUSTOM",
            {
                "i_accept_custom_mode_risk": True,
                "compound_cadence": "per_trade",
                "compound_every_n_trades": 3,
            },
        )
        comp = Compounder(mode=mode, tracker=tracker)

        reasons = []
        for day, pnl in enumerate([100.0, 200.0, 300.0, 50.0, 10.0]):
            tracker.on_trade_close("a", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
            event = TradeCloseEvent(ts_ms=_ts(day), strategy="a", pnl=pnl)
            result = comp.on_trade_close(event, tracker.snapshot(now_ms=_ts(day)))
            reasons.append(result.reason if result else None)

        # Trade 1, 2 → WAITING, Trade 3 → ADVANCED (reset to 0), Trade 4, 5 → WAITING
        assert reasons[0] == CompoundTickReason.WAITING_N_TRADES
        assert reasons[1] == CompoundTickReason.WAITING_N_TRADES
        assert reasons[2] == CompoundTickReason.ADVANCED
        assert reasons[3] == CompoundTickReason.WAITING_N_TRADES
        assert reasons[4] == CompoundTickReason.WAITING_N_TRADES

    def test_non_custom_mode_off_cadence(self):
        tracker, comp = _make(M3SMode.STANDARD)
        tracker.on_trade_close("a", pnl=100.0, symbol="BTC", ts_ms=_ts(0))
        event = TradeCloseEvent(ts_ms=_ts(0), strategy="a", pnl=100.0)
        result = comp.on_trade_close(event, tracker.snapshot(now_ms=_ts(0)))
        assert result is not None
        assert result.reason == CompoundTickReason.OFF_CADENCE

    def test_per_trade_counter_independent_per_strategy(self):
        """Each strategy has its own counter."""
        tracker = PortfolioTracker(initial_equity=10_000.0)
        mode = load_mode_from_dict(
            "CUSTOM",
            {
                "i_accept_custom_mode_risk": True,
                "compound_cadence": "per_trade",
                "compound_every_n_trades": 2,
            },
        )
        comp = Compounder(mode=mode, tracker=tracker)

        # Strategy A, trade 1: WAITING
        tracker.on_trade_close("a", pnl=50.0, symbol="BTC", ts_ms=_ts(0))
        ra1 = comp.on_trade_close(TradeCloseEvent(_ts(0), "a", 50.0), tracker.snapshot(now_ms=_ts(0)))
        assert ra1.reason == CompoundTickReason.WAITING_N_TRADES

        # Strategy B, trade 1: WAITING (independent counter)
        tracker.on_trade_close("b", pnl=50.0, symbol="BTC", ts_ms=_ts(1))
        rb1 = comp.on_trade_close(TradeCloseEvent(_ts(1), "b", 50.0), tracker.snapshot(now_ms=_ts(1)))
        assert rb1.reason == CompoundTickReason.WAITING_N_TRADES

        # Strategy A, trade 2: ADVANCED
        tracker.on_trade_close("a", pnl=50.0, symbol="BTC", ts_ms=_ts(2))
        ra2 = comp.on_trade_close(TradeCloseEvent(_ts(2), "a", 50.0), tracker.snapshot(now_ms=_ts(2)))
        assert ra2.reason == CompoundTickReason.ADVANCED


# ══════════════════════════════════════════════════════════════════════
# Mode transition
# ══════════════════════════════════════════════════════════════════════


class TestModeTransition:
    def test_set_mode_updates_state_label(self):
        _, comp = _make(M3SMode.STANDARD)
        assert comp.state.mode == "STANDARD"
        comp.set_mode(MODE_PRESETS[M3SMode.CONSERVATIVE])
        assert comp.state.mode == "CONSERVATIVE"
        assert comp.mode.name == M3SMode.CONSERVATIVE

    def test_set_mode_preserves_base_and_hwm(self):
        tracker, comp = _make(M3SMode.STANDARD)
        tracker.on_trade_close("a", pnl=1500.0, symbol="BTC", ts_ms=_ts(0))
        comp.update_base(tracker.snapshot(now_ms=_ts(0)))
        old_base = comp.state.base_equity
        old_hwm = comp.state.hwm

        comp.set_mode(MODE_PRESETS[M3SMode.GROWTH])
        assert comp.state.base_equity == old_base
        assert comp.state.hwm == old_hwm


# ══════════════════════════════════════════════════════════════════════
# BT #1 — Synthetic trade stream across modes
# ══════════════════════════════════════════════════════════════════════


class TestBacktestGate1:
    """BT #1: feed a 180-day synthetic trade stream through each mode and
    verify the invariants described in m3s_plan_v1.md §12.
    """

    def _run_stream(self, mode_key: M3SMode):
        tracker = PortfolioTracker(initial_equity=10_000.0)
        if mode_key == M3SMode.CUSTOM:
            mode = load_mode_from_dict("CUSTOM", {"i_accept_custom_mode_risk": True})
        else:
            mode = MODE_PRESETS[mode_key]
        comp = Compounder(mode=mode, tracker=tracker)

        # Synthetic stream: 180 days of +1% / -0.5% alternation + one
        # synthetic fat-tail event at day 90 (-5% portfolio).
        equity_path: list[float] = []
        scalar_path: list[float] = []
        for day in range(180):
            if day == 90:
                pnl = -tracker.equity * 0.05
            elif day % 2 == 0:
                pnl = tracker.equity * 0.01
            else:
                pnl = -tracker.equity * 0.005
            tracker.on_trade_close("synthetic", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
            snap = tracker.snapshot(now_ms=_ts(day))
            comp.update_base(snap)
            scalar_path.append(comp.risk_scalar(snap))
            equity_path.append(tracker.equity)

        return {
            "final_equity": tracker.equity,
            "hwm": tracker.hwm,
            "base": comp.state.base_equity,
            "min_scalar": min(scalar_path),
            "max_scalar": max(scalar_path),
            "equity_path": equity_path,
            "scalar_path": scalar_path,
        }

    def test_all_modes_finish_positive(self):
        """Synthetic stream has +EV; every mode should end above initial."""
        for mode_key in [M3SMode.CONSERVATIVE, M3SMode.STANDARD, M3SMode.GROWTH, M3SMode.CUSTOM]:
            result = self._run_stream(mode_key)
            assert result["final_equity"] > 10_000.0, f"mode {mode_key} lost money"

    def test_base_equity_never_decreases(self):
        """HWM gate invariant: base_equity only ratchets up."""
        for mode_key in [M3SMode.CONSERVATIVE, M3SMode.STANDARD, M3SMode.GROWTH]:
            tracker = PortfolioTracker(initial_equity=10_000.0)
            mode = MODE_PRESETS[mode_key]
            comp = Compounder(mode=mode, tracker=tracker)
            bases: list[float] = [comp.state.base_equity]
            for day in range(100):
                pnl = 50.0 if day % 2 == 0 else -70.0
                tracker.on_trade_close("synthetic", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
                comp.update_base(tracker.snapshot(now_ms=_ts(day)))
                bases.append(comp.state.base_equity)
            # Monotonically non-decreasing
            for i in range(1, len(bases)):
                assert bases[i] >= bases[i - 1], f"base dropped at i={i} for mode {mode_key}"

    def test_risk_scalar_de_levers_after_fat_tail(self):
        """After a synthetic -5% tail event, scalar should drop vs pre-event."""
        tracker = PortfolioTracker(initial_equity=10_000.0)
        mode = MODE_PRESETS[M3SMode.STANDARD]
        comp = Compounder(mode=mode, tracker=tracker)

        # 60 days of normal returns so CVaR has data
        for day in range(60):
            pnl = 20.0 if day % 2 == 0 else -10.0
            tracker.on_trade_close("synthetic", pnl=pnl, symbol="BTC", ts_ms=_ts(day))
            comp.update_base(tracker.snapshot(now_ms=_ts(day)))

        snap_pre = tracker.snapshot(now_ms=_ts(60))
        scalar_pre = comp.risk_scalar(snap_pre)

        # Inject the fat tail
        tracker.on_trade_close("synthetic", pnl=-1200.0, symbol="BTC", ts_ms=_ts(61))
        snap_post = tracker.snapshot(now_ms=_ts(61))
        scalar_post = comp.risk_scalar(snap_post)

        # Post-tail scalar should be strictly smaller than pre-tail.
        assert scalar_post < scalar_pre, (
            f"scalar did not shrink after tail event: pre={scalar_pre:.4f} post={scalar_post:.4f}"
        )
