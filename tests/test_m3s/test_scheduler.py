"""Sub-phase 0.6 — tests for src/m3s/scheduler.py (persistence + async scheduler)."""

from __future__ import annotations

import asyncio

import pytest

from src.m3s.allocator import Allocator
from src.m3s.compounder import Compounder
from src.m3s.conviction import ConvictionScorer
from src.m3s.edge_decay import EdgeDecayFlag, EdgeDecayMonitor, EdgeDecayState
from src.m3s.hooks import M3S
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.m3s.scheduler import (
    M3SScheduler,
    load_state,
    make_scheduler,
    save_state,
)
from src.m3s.state import M3SStore
from src.m3s.types import AllocationDecision, M3SEventType


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_700_000_000_000


def _ts(day: int) -> int:
    return _BASE_TS + day * _MS_PER_DAY


def _build_m3s(mode_key: M3SMode = M3SMode.STANDARD) -> M3S:
    tracker = PortfolioTracker(initial_equity=10_000.0)
    mode = MODE_PRESETS[mode_key]
    comp = Compounder(mode=mode, tracker=tracker)
    alloc = Allocator(mode=mode, tracker=tracker)
    ed = EdgeDecayMonitor()
    return M3S(
        mode=mode,
        tracker=tracker,
        compounder=comp,
        allocator=alloc,
        edge_decay=ed,
        conviction_scorer=ConvictionScorer(),
        shadow_mode=False,
    )


@pytest.fixture
def tmp_store(tmp_path):
    db = tmp_path / "m3s_scheduler_test.sqlite"
    store = M3SStore(str(db))
    yield store
    store.close()


# ══════════════════════════════════════════════════════════════════════
# save_state / load_state roundtrip
# ══════════════════════════════════════════════════════════════════════


class TestPersistenceRoundtrip:
    def test_save_and_load_compound_state(self, tmp_store):
        m3s = _build_m3s()
        # Drive some activity
        m3s.on_trade_close("a", pnl=500.0, symbol="BTC", ts_ms=_ts(0))
        m3s.rebalance(now_ms=_ts(0))
        save_state(m3s, tmp_store)

        # Build a fresh M3S and load
        m3s2 = _build_m3s()
        ok = load_state(m3s2, tmp_store)
        assert ok
        assert m3s2._compounder.state.base_equity == pytest.approx(10_500.0)
        assert m3s2._compounder.state.hwm == pytest.approx(10_500.0)

    def test_load_from_empty_store_is_graceful(self, tmp_store):
        m3s = _build_m3s()
        ok = load_state(m3s, tmp_store)
        # Nothing loaded, but no exception
        assert ok is False
        # Fresh state
        assert m3s._compounder.state.base_equity == 10_000.0

    def test_save_allocation_decision(self, tmp_store):
        m3s = _build_m3s()
        # Populate enough history to get a mature allocation
        import numpy as np
        rng = np.random.default_rng(17)
        for day in range(90):
            m3s.on_trade_close("a", pnl=float(rng.normal(20, 40)), symbol="BTC", ts_ms=_ts(day))
            m3s.on_trade_close("b", pnl=float(rng.normal(15, 35)), symbol="ETH", ts_ms=_ts(day))
            m3s.on_trade_close("c", pnl=float(rng.normal(10, 25)), symbol="SOL", ts_ms=_ts(day))
        decision = m3s.rebalance(now_ms=_ts(89))
        save_state(m3s, tmp_store)

        # Fresh M3S → load
        m3s2 = _build_m3s()
        load_state(m3s2, tmp_store)
        assert m3s2.last_allocation() is not None
        assert isinstance(m3s2.last_allocation(), AllocationDecision)
        assert m3s2.last_allocation().method == decision.method

    def test_save_mode(self, tmp_store):
        m3s = _build_m3s(M3SMode.GROWTH)
        save_state(m3s, tmp_store)
        assert tmp_store.get("mode", "current") == "GROWTH"

    def test_save_edge_decay_flags(self, tmp_store):
        m3s = _build_m3s()
        m3s._edge_decay._states["vol_mom"] = EdgeDecayState(
            strategy="vol_mom",
            flag=EdgeDecayFlag.HALVED,
            unhealthy_since_ms=_BASE_TS,
            halved_since_ms=_BASE_TS + 14 * _MS_PER_DAY,
        )
        save_state(m3s, tmp_store)

        m3s2 = _build_m3s()
        load_state(m3s2, tmp_store)
        assert m3s2._edge_decay.flag("vol_mom") == EdgeDecayFlag.HALVED

    def test_load_partial_corrupt_data_logs_and_continues(self, tmp_store):
        """Corrupt data in one namespace shouldn't break the others."""
        # Put valid mode
        tmp_store.put("mode", "current", "STANDARD")
        # Put garbage compound state
        tmp_store.put("compound", "state", "not-a-dict")

        m3s = _build_m3s()
        # Should not raise
        load_state(m3s, tmp_store)
        # Compound state remains fresh (load failed gracefully)
        assert m3s._compounder.state.base_equity == 10_000.0


# ══════════════════════════════════════════════════════════════════════
# Scheduler async loop
# ══════════════════════════════════════════════════════════════════════


class TestSchedulerBasic:
    def test_invalid_cadence_rejected(self):
        m3s = _build_m3s()
        store = M3SStore(":memory:")
        with pytest.raises(ValueError, match="cadence_seconds"):
            M3SScheduler(m3s, store, cadence_seconds=0)
        store.close()

    @pytest.mark.asyncio
    async def test_tick_runs_rebalance_and_persists(self, tmp_store):
        m3s = _build_m3s()
        m3s.on_trade_close("a", pnl=300.0, symbol="BTC", ts_ms=_ts(0))

        scheduler = M3SScheduler(m3s, tmp_store, cadence_seconds=60.0)
        await scheduler.tick()

        assert scheduler.tick_count == 1
        assert scheduler.error_count == 0
        # Compounder state was persisted
        saved = tmp_store.get("compound", "state")
        assert saved is not None
        assert saved["base_equity"] == pytest.approx(10_300.0)
        # Allocation event was appended
        events = tmp_store.query_events(event_type=M3SEventType.ALLOCATION.value)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_tick_catches_errors(self, tmp_store):
        """A broken rebalance shouldn't crash the scheduler."""
        m3s = _build_m3s()

        # Monkey-patch rebalance to raise
        def _boom(*a, **kw):
            raise RuntimeError("synthetic failure")

        m3s.rebalance = _boom  # type: ignore[method-assign]
        scheduler = M3SScheduler(m3s, tmp_store, cadence_seconds=60.0)
        await scheduler.tick()  # should not raise

        assert scheduler.tick_count == 1
        assert scheduler.error_count == 1

    @pytest.mark.asyncio
    async def test_run_loop_stops_on_stop_call(self, tmp_store):
        m3s = _build_m3s()
        scheduler = M3SScheduler(m3s, tmp_store, cadence_seconds=0.05)

        async def stop_after_two_ticks():
            await asyncio.sleep(0.12)  # enough for ~2 ticks
            scheduler.stop()

        await asyncio.gather(scheduler.run(), stop_after_two_ticks())
        assert scheduler.tick_count >= 1


# ══════════════════════════════════════════════════════════════════════
# make_scheduler factory
# ══════════════════════════════════════════════════════════════════════


class TestMakeScheduler:
    def test_factory_restores_state(self, tmp_path):
        db_path = str(tmp_path / "m3s_factory.sqlite")
        # Pre-populate the store
        store = M3SStore(db_path)
        store.put("compound", "state", {
            "base_equity": 12_345.67,
            "hwm": 12_345.67,
            "last_updated_ts_ms": 0,
            "mode": "STANDARD",
        })
        store.close()

        m3s = _build_m3s()
        scheduler, store2 = make_scheduler(m3s, db_path=db_path, cadence_seconds=60.0)
        assert m3s._compounder.state.base_equity == pytest.approx(12_345.67)
        store2.close()

    def test_factory_no_restore(self, tmp_path):
        db_path = str(tmp_path / "m3s_factory_no_restore.sqlite")
        store = M3SStore(db_path)
        store.put("compound", "state", {
            "base_equity": 99_999.0,
            "hwm": 99_999.0,
            "last_updated_ts_ms": 0,
            "mode": "STANDARD",
        })
        store.close()

        m3s = _build_m3s()
        scheduler, store2 = make_scheduler(
            m3s, db_path=db_path, cadence_seconds=60.0, restore=False,
        )
        # Did NOT load from disk
        assert m3s._compounder.state.base_equity == 10_000.0
        store2.close()
