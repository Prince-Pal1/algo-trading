"""Tests for src/flow/zone_state.py — state transitions and signal discipline.

Two rules here are safety-critical:
  · invalidation beats confirmation — a level that broke cannot also confirm
  · one signal per test — a zone must not fire repeatedly while price loiters
"""

from __future__ import annotations

import pytest

from src.flow.level_registry import Level, LevelRegistry, LevelSide
from src.flow.zone_state import FlowEngine, ZoneMonitor, ZoneState
from src.utils.types import Tick

TS = 1_757_000_000_000
BASELINE = 100.0


def _tick(price: float, maker: bool = True, ts: int = TS, qty: float = 1.0) -> Tick:
    return Tick(symbol="BTCUSDT", price=price, quantity=qty,
                timestamp=ts, is_buyer_maker=maker)


def _support(width: float = 100.0) -> Level:
    return Level("sup", "BTCUSDT", 98000.0, width, LevelSide.LONG, note="pdl")


def _monitor(**kw) -> ZoneMonitor:
    return ZoneMonitor(level=_support(), **kw)


def _absorb(mon: ZoneMonitor, n: int, start_ms: int, price_fn=None) -> list:
    """Feed absorbed selling: one-sided sells, price pinned."""
    price_fn = price_fn or (lambda i: 98000.0 + (i % 3))
    out = []
    for i in range(n):
        sig = mon.on_tick(_tick(price_fn(i), True, start_ms + i * 100), BASELINE)
        if sig:
            out.append(sig)
    return out


class TestTransitions:
    def test_starts_idle(self):
        assert _monitor().state is ZoneState.IDLE

    def test_far_price_stays_idle(self):
        mon = _monitor()
        mon.on_tick(_tick(99000.0), BASELINE)
        assert mon.state is ZoneState.IDLE

    def test_near_price_approaches(self):
        mon = _monitor(approach_mult=3.0)
        mon.on_tick(_tick(98250.0), BASELINE)   # 150 away, within 3x100
        assert mon.state is ZoneState.APPROACHING

    def test_entering_zone_evaluates(self):
        mon = _monitor()
        mon.on_tick(_tick(98050.0), BASELINE)
        assert mon.state is ZoneState.EVALUATING

    def test_entering_increments_test_count(self):
        mon = _monitor()
        mon.on_tick(_tick(98050.0), BASELINE)
        assert mon.test_count == 1

    def test_leaving_area_returns_to_idle(self):
        mon = _monitor()
        mon.on_tick(_tick(98050.0, ts=TS), BASELINE)
        mon.on_tick(_tick(99500.0, ts=TS + 1000), BASELINE)
        assert mon.state is ZoneState.IDLE

    def test_approaching_then_away_returns_idle(self):
        mon = _monitor()
        mon.on_tick(_tick(98250.0, ts=TS), BASELINE)
        mon.on_tick(_tick(99500.0, ts=TS + 1000), BASELINE)
        assert mon.state is ZoneState.IDLE

    def test_test_count_survives_leaving_and_returning(self):
        mon = _monitor()
        mon.on_tick(_tick(98050.0, ts=TS), BASELINE)
        mon.on_tick(_tick(99500.0, ts=TS + 1000), BASELINE)
        mon.on_tick(_tick(98050.0, ts=TS + 2000), BASELINE)
        assert mon.test_count == 2


class TestConfirmation:
    def test_absorbed_selling_confirms(self):
        mon = _monitor(min_dwell_ms=5_000)
        signals = _absorb(mon, 200, TS)
        assert signals, "expected a confirmation"
        assert mon.state is ZoneState.CONFIRMED
        assert signals[0].score >= signals[0].threshold

    def test_min_dwell_blocks_early_signal(self):
        """Strong evidence must still wait out the minimum dwell."""
        mon = _monitor(min_dwell_ms=60_000)
        signals = _absorb(mon, 100, TS)     # 100 ticks x 100ms = 10s
        assert signals == []
        assert mon.state is ZoneState.EVALUATING
        assert mon.report.confirmed is True   # evidence was there, time was not

    def test_only_one_signal_per_test(self):
        mon = _monitor(min_dwell_ms=5_000)
        signals = _absorb(mon, 400, TS)
        assert len(signals) == 1

    def test_signal_carries_reasoning(self):
        mon = _monitor(min_dwell_ms=5_000)
        sig = _absorb(mon, 200, TS)[0]
        assert sig.report.supporting
        assert sig.level_id == "sup"
        assert sig.side is LevelSide.LONG

    def test_invalidation_below_support(self):
        mon = _monitor(min_dwell_ms=5_000, invalidation_mult=1.0)
        sig = _absorb(mon, 200, TS)[0]
        assert sig.invalidation < mon.level.low

    def test_signal_dict_serializable(self):
        import orjson
        mon = _monitor(min_dwell_ms=5_000)
        sig = _absorb(mon, 200, TS)[0]
        assert orjson.loads(orjson.dumps(sig.to_dict()))["level_id"] == "sup"


class TestInvalidation:
    def test_push_through_invalidates(self):
        mon = _monitor(invalidation_mult=1.0)
        mon.on_tick(_tick(98050.0, ts=TS), BASELINE)
        mon.on_tick(_tick(97850.0, ts=TS + 1000), BASELINE)   # 150 below level
        assert mon.state is ZoneState.INVALIDATED

    def test_invalidation_beats_confirmation(self):
        """Evidence may look great; a broken level is still broken."""
        mon = _monitor(min_dwell_ms=0, invalidation_mult=1.0)
        _absorb(mon, 200, TS)
        mon._signalled_this_test = False
        mon.state = ZoneState.EVALUATING
        sig = mon.on_tick(_tick(97800.0, True, TS + 50_000), BASELINE)
        assert sig is None
        assert mon.state is ZoneState.INVALIDATED

    def test_resistance_invalidates_upward(self):
        level = Level("res", "BTCUSDT", 98000.0, 100.0, LevelSide.SHORT)
        mon = ZoneMonitor(level=level, invalidation_mult=1.0)
        mon.on_tick(_tick(97950.0, ts=TS), BASELINE)
        mon.on_tick(_tick(98250.0, ts=TS + 1000), BASELINE)
        assert mon.state is ZoneState.INVALIDATED


class TestCooldown:
    def test_outcome_persists_through_cooldown(self):
        """A dashboard must be able to show 'confirmed N minutes ago'."""
        mon = _monitor(min_dwell_ms=5_000, cooldown_ms=600_000)
        _absorb(mon, 200, TS)
        mon.on_tick(_tick(98050.0, ts=TS + 30_000), BASELINE)
        assert mon.state is ZoneState.CONFIRMED

    def test_cooldown_blocks_new_signals(self):
        mon = _monitor(min_dwell_ms=5_000, cooldown_ms=600_000)
        _absorb(mon, 200, TS)
        mon.on_tick(_tick(98050.0, ts=TS + 30_000), BASELINE)
        assert _absorb(mon, 200, TS + 40_000) == []

    def test_cooldown_expires_to_idle(self):
        mon = _monitor(min_dwell_ms=5_000, cooldown_ms=10_000)
        _absorb(mon, 200, TS)
        mon.on_tick(_tick(99000.0, ts=TS + 100_000), BASELINE)
        assert mon.state is ZoneState.IDLE


class TestSnapshot:
    def test_snapshot_shape_when_idle(self):
        snap = _monitor().snapshot()
        assert snap["state"] == "IDLE"
        assert snap["score"] is None

    def test_snapshot_carries_live_evidence(self):
        mon = _monitor(min_dwell_ms=600_000)
        _absorb(mon, 200, TS)
        snap = mon.snapshot()
        assert snap["state"] == "EVALUATING"
        assert snap["score"] > 0.5
        assert snap["supporting"]

    def test_snapshot_serializable(self):
        import orjson
        mon = _monitor(min_dwell_ms=600_000)
        _absorb(mon, 200, TS)
        assert orjson.loads(orjson.dumps(mon.snapshot()))["level_id"] == "sup"


class TestFlowEngine:
    def _engine(self, *levels) -> FlowEngine:
        reg = LevelRegistry()
        reg.levels = {lv.id: lv for lv in (levels or (_support(),))}
        eng = FlowEngine(registry=reg, symbol="BTCUSDT")
        eng.sync_levels(now_ms=TS)
        return eng

    def test_sync_creates_monitors(self):
        assert set(self._engine().monitors) == {"sup"}

    def test_sync_drops_removed_levels(self):
        eng = self._engine()
        eng.registry.levels = {}
        eng.sync_levels(now_ms=TS)
        assert eng.monitors == {}

    def test_sync_preserves_state_on_edit(self):
        eng = self._engine()
        eng.on_tick(_tick(98050.0), TS)
        assert eng.monitors["sup"].state is ZoneState.EVALUATING
        eng.registry.levels["sup"].note = "edited"
        eng.sync_levels(now_ms=TS)
        assert eng.monitors["sup"].state is ZoneState.EVALUATING

    def test_routes_to_all_monitors(self):
        second = Level("sup2", "BTCUSDT", 97000.0, 100.0, LevelSide.LONG)
        eng = self._engine(_support(), second)
        eng.on_tick(_tick(98050.0), TS)
        assert eng.monitors["sup"].state is ZoneState.EVALUATING
        assert eng.monitors["sup2"].state is ZoneState.IDLE

    def test_lag_measured(self):
        eng = self._engine()
        eng.on_tick(_tick(98050.0, ts=TS), local_ms=TS + 250)
        assert eng._lag_ms == 250

    def test_baseline_range_from_history(self):
        eng = self._engine()
        for i in range(100):
            eng.on_tick(_tick(98500.0 + i, ts=TS + i * 1000), TS)
        assert eng._baseline_range(TS + 99_000) == pytest.approx(99.0)

    def test_baseline_ignores_stale_prices(self):
        eng = self._engine()
        eng.on_tick(_tick(90000.0, ts=TS), TS)
        eng.on_tick(_tick(98000.0, ts=TS + 3_600_000), TS)
        assert eng._baseline_range(TS + 3_600_000) == pytest.approx(0.0)

    def test_engine_snapshot_serializable(self):
        import orjson
        eng = self._engine()
        eng.on_tick(_tick(98050.0, ts=TS), local_ms=TS + 10)
        snap = orjson.loads(orjson.dumps(eng.snapshot(local_ms=TS + 10)))
        assert snap["symbol"] == "BTCUSDT"
        assert snap["lag_ms"] == 10
        assert len(snap["levels"]) == 1
