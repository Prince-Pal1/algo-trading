"""Sub-phase 0.2 — tests for src/m3s/edge_decay.py (Tier 1 #2)."""

from __future__ import annotations

import pytest

from src.m3s.edge_decay import (
    EdgeDecayFlag,
    EdgeDecayMonitor,
)
from src.m3s.types import PortfolioSnapshot, StrategySnapshot


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_700_000_000_000


def _snap(
    *,
    name: str,
    rolling_sharpe: float,
    lifetime_sharpe: float,
    pnl_30d: float = 0.0,
    n_trades_30d: int = 10,
    lifetime_winrate: float = 0.55,
    ts_ms: int = _BASE_TS,
    equity: float = 10_000.0,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts_ms=ts_ms,
        equity=equity,
        hwm=equity,
        drawdown_pct=0.0,
        per_strategy={
            name: StrategySnapshot(
                name=name,
                n_trades_30d=n_trades_30d,
                rolling_sharpe_30d=rolling_sharpe,
                realized_vol_30d=0.1,
                pnl_30d=pnl_30d,
                lifetime_sharpe=lifetime_sharpe,
                lifetime_winrate=lifetime_winrate,
            )
        },
        signal_corr={},
    )


class TestConfigValidation:
    def test_invalid_sharpe_threshold_rejected(self):
        with pytest.raises(ValueError, match="sharpe_decay_threshold"):
            EdgeDecayMonitor(sharpe_decay_threshold=0.0)
        with pytest.raises(ValueError, match="sharpe_decay_threshold"):
            EdgeDecayMonitor(sharpe_decay_threshold=1.0)

    def test_invalid_persistence_days_rejected(self):
        with pytest.raises(ValueError, match="persistence_days"):
            EdgeDecayMonitor(persistence_days=0)


class TestHealthyStrategyNoFlags:
    def test_healthy_strategy_stays_normal(self):
        mon = EdgeDecayMonitor(sharpe_decay_threshold=0.5, persistence_days=14)
        snap = _snap(name="a", rolling_sharpe=1.8, lifetime_sharpe=2.0)
        alerts = mon.check(snap, now_ms=_BASE_TS)
        assert alerts == []
        assert mon.flag("a") == EdgeDecayFlag.NORMAL
        assert not mon.should_halve("a")
        assert not mon.should_pause("a")

    def test_cold_start_strategy_ignored(self):
        mon = EdgeDecayMonitor(min_lifetime_trades=20)
        # Few trades + no lifetime sharpe = cold start
        snap = _snap(
            name="newbie",
            rolling_sharpe=0.0,
            lifetime_sharpe=0.0,
            n_trades_30d=3,
        )
        alerts = mon.check(snap, now_ms=_BASE_TS)
        assert alerts == []
        assert mon.flag("newbie") == EdgeDecayFlag.NORMAL


class TestDecayPersistenceGate:
    def test_one_day_of_decay_does_not_trigger(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
        )
        snap = _snap(
            name="a",
            rolling_sharpe=0.5,
            lifetime_sharpe=2.0,
            ts_ms=_BASE_TS,
        )  # rolling < 0.5 * 2.0 = 1.0 → unhealthy
        alerts = mon.check(snap, now_ms=_BASE_TS)
        assert alerts == []  # no transition yet — persistence gate not cleared
        assert mon.flag("a") == EdgeDecayFlag.NORMAL
        st = mon.state("a")
        assert st is not None
        assert st.unhealthy_since_ms == _BASE_TS

    def test_persistence_window_elapsed_triggers_halve(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
            auto_halve_enabled=True,
        )
        snap1 = _snap(name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0, ts_ms=_BASE_TS)
        mon.check(snap1, now_ms=_BASE_TS)

        later = _BASE_TS + 14 * _MS_PER_DAY
        snap2 = _snap(name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0, ts_ms=later)
        alerts = mon.check(snap2, now_ms=later)

        assert mon.should_halve("a")
        assert not mon.should_pause("a")
        assert len(alerts) == 1
        assert alerts[0].new_flag == EdgeDecayFlag.HALVED
        assert alerts[0].old_flag == EdgeDecayFlag.NORMAL

    def test_double_persistence_window_triggers_pause(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
            auto_halve_enabled=True,
            auto_pause_enabled=True,
        )
        # Day 0: unhealthy begins
        mon.check(
            _snap(name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0, ts_ms=_BASE_TS),
            now_ms=_BASE_TS,
        )
        # Day 14: halve fires
        mon.check(
            _snap(
                name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 14 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 14 * _MS_PER_DAY,
        )
        assert mon.should_halve("a")

        # Day 28: still unhealthy → pause fires
        alerts = mon.check(
            _snap(
                name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 28 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 28 * _MS_PER_DAY,
        )
        assert mon.should_pause("a")
        assert not mon.should_halve("a")
        assert len(alerts) == 1
        assert alerts[0].old_flag == EdgeDecayFlag.HALVED
        assert alerts[0].new_flag == EdgeDecayFlag.PAUSED


class TestRecovery:
    def test_recovery_clears_state(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
        )
        # Day 0: unhealthy
        mon.check(
            _snap(name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0, ts_ms=_BASE_TS),
            now_ms=_BASE_TS,
        )
        # Day 14: halved
        mon.check(
            _snap(
                name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 14 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 14 * _MS_PER_DAY,
        )
        assert mon.should_halve("a")

        # Day 20: recovered — rolling Sharpe back to 1.5, above the 0.5*2.0=1.0 floor
        alerts = mon.check(
            _snap(
                name="a", rolling_sharpe=1.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 20 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 20 * _MS_PER_DAY,
        )
        assert mon.flag("a") == EdgeDecayFlag.NORMAL
        assert not mon.should_halve("a")
        assert len(alerts) == 1
        assert alerts[0].new_flag == EdgeDecayFlag.NORMAL


class TestAutoActionFlags:
    def test_auto_halve_disabled_stays_normal(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
            auto_halve_enabled=False,
        )
        # Past persistence window, still unhealthy
        mon.check(
            _snap(name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0, ts_ms=_BASE_TS),
            now_ms=_BASE_TS,
        )
        mon.check(
            _snap(
                name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 30 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 30 * _MS_PER_DAY,
        )
        assert mon.flag("a") == EdgeDecayFlag.NORMAL
        assert not mon.should_halve("a")

    def test_auto_pause_disabled_stops_at_halved(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
            auto_halve_enabled=True,
            auto_pause_enabled=False,
        )
        mon.check(
            _snap(name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0, ts_ms=_BASE_TS),
            now_ms=_BASE_TS,
        )
        mon.check(
            _snap(
                name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 14 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 14 * _MS_PER_DAY,
        )
        mon.check(
            _snap(
                name="a", rolling_sharpe=0.5, lifetime_sharpe=2.0,
                ts_ms=_BASE_TS + 30 * _MS_PER_DAY,
            ),
            now_ms=_BASE_TS + 30 * _MS_PER_DAY,
        )
        assert mon.should_halve("a")
        assert not mon.should_pause("a")


class TestMultiStrategyIndependence:
    def test_decay_in_one_strategy_does_not_flag_others(self):
        mon = EdgeDecayMonitor(sharpe_decay_threshold=0.5, persistence_days=14)
        snap = PortfolioSnapshot(
            ts_ms=_BASE_TS,
            equity=10_000.0,
            hwm=10_000.0,
            drawdown_pct=0.0,
            per_strategy={
                "sick": StrategySnapshot(
                    name="sick",
                    n_trades_30d=20,
                    rolling_sharpe_30d=0.2,
                    realized_vol_30d=0.3,
                    pnl_30d=-100.0,
                    lifetime_sharpe=2.0,
                    lifetime_winrate=0.55,
                ),
                "healthy": StrategySnapshot(
                    name="healthy",
                    n_trades_30d=20,
                    rolling_sharpe_30d=1.8,
                    realized_vol_30d=0.1,
                    pnl_30d=200.0,
                    lifetime_sharpe=2.0,
                    lifetime_winrate=0.6,
                ),
            },
            signal_corr={},
        )
        # Advance to past persistence
        mon.check(snap, now_ms=_BASE_TS)
        later = _BASE_TS + 20 * _MS_PER_DAY
        snap_later = PortfolioSnapshot(
            ts_ms=later,
            equity=snap.equity,
            hwm=snap.hwm,
            drawdown_pct=snap.drawdown_pct,
            per_strategy=snap.per_strategy,
            signal_corr={},
        )
        mon.check(snap_later, now_ms=later)
        assert mon.should_halve("sick")
        assert mon.flag("healthy") == EdgeDecayFlag.NORMAL


class TestWinrateProxyTrigger:
    def test_negative_pnl_on_winning_strategy_triggers_unhealthy(self):
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            persistence_days=14,
        )
        # Rolling Sharpe above decay floor (0.5 * 2.0 = 1.0), so only the
        # pnl_30d proxy can flag this.
        snap = _snap(
            name="a",
            rolling_sharpe=1.2,
            lifetime_sharpe=2.0,
            pnl_30d=-500.0,
            n_trades_30d=10,
            lifetime_winrate=0.6,
            ts_ms=_BASE_TS,
        )
        mon.check(snap, now_ms=_BASE_TS)
        st = mon.state("a")
        assert st is not None
        assert st.unhealthy_since_ms == _BASE_TS
        assert any("pnl_30d" in r for r in st.last_triggers)
