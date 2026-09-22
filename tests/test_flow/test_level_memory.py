"""Tests for src/flow/level_memory.py and its wiring into the zone monitor.

Three things matter here:
  · OFF by default, and off means off — no file, no reads, no scoring effect
  · reads are PURE — an earlier version pruned inside counts(), which made a
    dashboard poll silently destroy the history it was reporting on
  · an outcome is recorded at most once per test, and only when the test
    actually resolved — an inconclusive test is not evidence
"""

from __future__ import annotations

import orjson
import pytest

from src.flow.evidence import score_zone
from src.flow.features import MarketContext, ZoneAccumulator
from src.flow.level_memory import (
    DEFAULT_MAX_AGE_DAYS,
    FAILED,
    HELD,
    MAX_OUTCOMES_PER_LEVEL,
    LevelMemory,
)
from src.flow.level_registry import Level, LevelSide
from src.flow.zone_state import ZoneMonitor, ZoneState
from src.utils.types import Tick

TS = 1_757_000_000_000
DAY = 86_400_000
CTX = MarketContext(baseline_range=200.0, baseline_trade_size=1.0, tick_size=0.1)


def _tick(price: float, ts: int, maker: bool = True, qty: float = 1.0) -> Tick:
    return Tick(symbol="BTCUSDT", price=price, quantity=qty,
                timestamp=ts, is_buyer_maker=maker)


def _support(width: float = 50.0) -> Level:
    return Level("sup", "BTCUSDT", 98_000.0, width, LevelSide.LONG, first_seen_ms=TS)


def _memory(tmp_path, enabled: bool = True, **kw) -> LevelMemory:
    return LevelMemory(enabled=enabled, path=tmp_path / "mem.json", **kw)


def _monitor(mem: LevelMemory | None, **kw) -> ZoneMonitor:
    kw.setdefault("require_turn", False)
    kw.setdefault("min_dwell_ms", 0)
    return ZoneMonitor(level=_support(), memory=mem, **kw)


# ── disabled is the default, and it is total ───────────────────────────

class TestDisabled:
    def test_off_by_default(self):
        assert LevelMemory().enabled is False

    def test_disabled_records_nothing_and_writes_no_file(self, tmp_path):
        mem = _memory(tmp_path, enabled=False)
        mem.record("sup", FAILED, TS, 97_900.0)
        assert mem.counts("sup", TS) == (0, 0)
        assert mem.last_outcome("sup") is None
        assert not (tmp_path / "mem.json").exists()

    def test_disabled_ignores_an_existing_file(self, tmp_path):
        on = _memory(tmp_path)
        on.record("sup", FAILED, TS, 97_900.0)
        off = _memory(tmp_path, enabled=False)
        assert off.counts("sup", TS) == (0, 0)

    def test_monitor_without_memory_reports_zero_priors(self):
        mon = _monitor(None)
        mon.on_tick(_tick(98_000.0, TS), CTX)
        assert mon.state is ZoneState.EVALUATING
        snap = mon.snapshot()
        assert (snap["prior_held"], snap["prior_failed"]) == (0, 0)
        assert snap["prior_last"] is None


# ── the store itself ───────────────────────────────────────────────────

class TestStore:
    def test_counts_and_last_outcome(self, tmp_path):
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS, 97_900.0)
        mem.record("sup", HELD, TS + 1000, 98_200.0)
        assert mem.counts("sup", TS + 1000) == (1, 1)
        assert mem.last_outcome("sup") == HELD

    def test_unknown_outcome_is_dropped(self, tmp_path):
        mem = _memory(tmp_path)
        mem.record("sup", "maybe", TS, 98_000.0)
        assert mem.counts("sup", TS) == (0, 0)

    def test_levels_are_independent(self, tmp_path):
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS, 97_900.0)
        mem.record("res", HELD, TS, 99_100.0)
        assert mem.counts("sup", TS) == (0, 1)
        assert mem.counts("res", TS) == (1, 0)

    def test_counts_is_pure(self, tmp_path):
        """Regression: counts() used to prune, so reading destroyed history."""
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS, 97_900.0)
        later = TS + 30 * DAY          # far outside the age window
        assert mem.counts("sup", later) == (0, 0)   # filtered out of the answer
        assert mem.counts("sup", TS) == (0, 1)      # but still on disk-side state
        assert len(mem.records["sup"].outcomes) == 1

    def test_age_window_filters_old_outcomes(self, tmp_path):
        mem = _memory(tmp_path, max_age_days=2)
        mem.record("sup", FAILED, TS, 97_900.0)
        assert mem.counts("sup", TS + DAY) == (0, 1)
        assert mem.counts("sup", TS + 3 * DAY) == (0, 0)

    def test_recording_prunes_stale_outcomes(self, tmp_path):
        mem = _memory(tmp_path, max_age_days=2)
        mem.record("sup", FAILED, TS, 97_900.0)
        mem.record("sup", HELD, TS + 5 * DAY, 98_200.0)
        assert mem.counts("sup") == (1, 0)
        assert len(mem.records["sup"].outcomes) == 1

    def test_history_is_bounded(self, tmp_path):
        mem = _memory(tmp_path)
        for i in range(MAX_OUTCOMES_PER_LEVEL + 20):
            mem.record("sup", HELD, TS + i, 98_200.0)
        assert len(mem.records["sup"].outcomes) == MAX_OUTCOMES_PER_LEVEL

    def test_clear_one_and_all(self, tmp_path):
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS, 97_900.0)
        mem.record("res", HELD, TS, 99_100.0)
        mem.clear("sup")
        assert mem.counts("sup", TS) == (0, 0)
        assert mem.counts("res", TS) == (1, 0)
        mem.clear()
        assert mem.records == {}


# ── persistence ────────────────────────────────────────────────────────

class TestPersistence:
    def test_survives_a_restart(self, tmp_path):
        first = _memory(tmp_path)
        first.record("sup", FAILED, TS, 97_900.0)
        second = _memory(tmp_path)
        assert second.counts("sup", TS) == (0, 1)
        assert second.last_outcome("sup") == FAILED

    def test_load_drops_outcomes_past_the_window(self, tmp_path):
        first = _memory(tmp_path, max_age_days=2)
        first.record("sup", FAILED, TS, 97_900.0)
        # A restart much later should not resurrect an ancient outcome.
        second = LevelMemory(enabled=True, path=tmp_path / "mem.json",
                             max_age_days=2)
        assert second.counts("sup", TS + 30 * DAY) == (0, 0)

    def test_corrupt_file_starts_empty_instead_of_raising(self, tmp_path):
        (tmp_path / "mem.json").write_text("{not json")
        mem = _memory(tmp_path)
        assert mem.records == {}
        mem.record("sup", HELD, TS, 98_200.0)      # still usable
        assert mem.counts("sup", TS) == (1, 0)

    def test_save_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS, 97_900.0)
        assert not (tmp_path / "mem.tmp").exists()
        payload = orjson.loads((tmp_path / "mem.json").read_bytes())
        assert payload["sup"]["outcomes"][0]["outcome"] == FAILED


# ── outcome detection in the monitor ───────────────────────────────────

class TestOutcomeDetection:
    def _break_it(self, mon: ZoneMonitor, start: int) -> int:
        """Sell through the level until adverse excursion invalidates it."""
        ts = start
        for price in (98_000.0, 97_990.0, 97_960.0, 97_900.0, 97_800.0):
            ts += 1000
            mon.on_tick(_tick(price, ts, maker=False), CTX)
        return ts

    def _hold_it(self, mon: ZoneMonitor, start: int) -> int:
        """Enter the zone, then leave upward past the approach band."""
        ts = start
        for price in (98_020.0, 98_030.0, 98_120.0, 98_260.0, 98_400.0):
            ts += 1000
            mon.on_tick(_tick(price, ts), CTX)
        return ts

    def test_break_records_a_failure(self, tmp_path):
        mem = _memory(tmp_path)
        mon = _monitor(mem)
        ts = self._break_it(mon, TS)
        assert mon.state is ZoneState.INVALIDATED
        assert mem.counts("sup", ts) == (0, 1)

    def test_leaving_on_the_favourable_side_records_a_hold(self, tmp_path):
        mem = _memory(tmp_path)
        mon = _monitor(mem)
        ts = self._hold_it(mon, TS)
        assert mon.state is ZoneState.IDLE
        assert mem.counts("sup", ts) == (1, 0)

    def test_short_level_hold_is_the_other_direction(self, tmp_path):
        mem = _memory(tmp_path)
        level = Level("res", "BTCUSDT", 98_000.0, 50.0, LevelSide.SHORT,
                      first_seen_ms=TS)
        mon = ZoneMonitor(level=level, memory=mem, require_turn=False,
                          min_dwell_ms=0)
        ts = TS
        for price in (97_980.0, 97_970.0, 97_880.0, 97_740.0, 97_600.0):
            ts += 1000
            mon.on_tick(_tick(price, ts), CTX)
        assert mem.counts("res", ts) == (1, 0)

    def test_sideways_exit_records_nothing(self, tmp_path):
        """Leaving on the wrong side of a support without breaking it is
        inconclusive, and inconclusive is not evidence."""
        mem = _memory(tmp_path)
        mon = _monitor(mem, invalidation_mult=10.0)   # never invalidates here
        ts = TS
        for price in (98_020.0, 98_010.0, 97_960.0, 97_880.0, 97_700.0):
            ts += 1000
            mon.on_tick(_tick(price, ts), CTX)
        assert mon.state is ZoneState.IDLE
        assert mem.counts("sup", ts) == (0, 0)

    def test_one_outcome_per_test(self, tmp_path):
        mem = _memory(tmp_path)
        mon = _monitor(mem)
        ts = self._break_it(mon, TS)
        for price in (97_700.0, 97_600.0, 97_500.0):
            ts += 1000
            mon.on_tick(_tick(price, ts, maker=False), CTX)
        assert mem.counts("sup", ts) == (0, 1)

    def _confirm(self, mon: ZoneMonitor, start: int) -> tuple:
        """Absorbed selling until the zone confirms. Returns (signal, ts)."""
        for i in range(200):
            ts = start + i * 100
            signal = mon.on_tick(_tick(98_000.0 + (i % 3), ts), CTX)
            if signal is not None:
                return signal, ts
        raise AssertionError("expected a confirmation")

    def test_a_confirmation_that_then_breaks_is_recorded_as_a_failure(self, tmp_path):
        """The defender was there, then was not — the most informative case,
        and it happens during the cooldown where the exit test cannot see it."""
        mem = _memory(tmp_path)
        mon = _monitor(mem, threshold=0.0)
        signal, ts = self._confirm(mon, TS)
        assert mon.state is ZoneState.CONFIRMED
        ts += 1000
        mon.on_tick(_tick(signal.invalidation - 10.0, ts, maker=False), CTX)
        assert mem.counts("sup", ts) == (0, 1)

    def test_a_confirmation_that_holds_is_recorded_as_a_hold(self, tmp_path):
        mem = _memory(tmp_path)
        mon = _monitor(mem, threshold=0.0, cooldown_ms=10_000)
        _, ts = self._confirm(mon, TS)
        ts += 20_000                                 # past the cooldown, higher
        mon.on_tick(_tick(98_300.0, ts), CTX)
        assert mon.state is ZoneState.IDLE
        assert mem.counts("sup", ts) == (1, 0)

    def test_a_confirmation_only_fails_once(self, tmp_path):
        mem = _memory(tmp_path)
        mon = _monitor(mem, threshold=0.0)
        signal, ts = self._confirm(mon, TS)
        for drop in (10.0, 50.0, 200.0):
            ts += 1000
            mon.on_tick(_tick(signal.invalidation - drop, ts, maker=False), CTX)
        assert mem.counts("sup", ts) == (0, 1)

    def test_priors_are_snapshotted_at_entry(self, tmp_path):
        """A read that shifted mid-test would make the score depend on when it
        was taken rather than on the evidence."""
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS - DAY, 97_900.0)
        mon = _monitor(mem)
        ts = TS + 1000
        mon.on_tick(_tick(98_000.0, ts), CTX)
        assert mon._acc is not None and mon._acc.prior_failed == 1
        mem.record("sup", FAILED, ts, 97_900.0)      # arrives mid-test
        assert mon._acc.prior_failed == 1

    def test_snapshot_reports_memory_on_an_idle_level(self, tmp_path):
        mem = _memory(tmp_path)
        mem.record("sup", FAILED, TS, 97_900.0)
        mon = _monitor(mem)
        mon.on_tick(_tick(99_500.0, TS + 1000), CTX)   # far away, stays idle
        snap = mon.snapshot()
        assert mon.state is ZoneState.IDLE
        assert (snap["prior_held"], snap["prior_failed"]) == (0, 1)
        assert snap["prior_last"] == FAILED


# ── the evidence rules ─────────────────────────────────────────────────

class TestEvidenceRules:
    def _features(self, **kw):
        """Neutral zone evidence, so only the memory rules vary."""
        acc = ZoneAccumulator(
            level_price=98_000.0, is_support=True, baseline_range=200.0,
            baseline_trade_size=1.0, zone_width=50.0, **kw,
        )
        for i in range(40):
            acc.on_tick(_tick(98_000.0, TS + i * 100, maker=i % 2 == 0))
        return acc.features()

    def test_prior_failure_opposes(self):
        report = score_zone(_support(), self._features(prior_failed=2), 0.62)
        names = [i.name for i in report.opposing]
        assert "prior_failure" in names
        assert "prior_hold" not in [i.name for i in report.supporting]

    def test_prior_hold_supports(self):
        report = score_zone(_support(), self._features(prior_held=2), 0.62)
        assert "prior_hold" in [i.name for i in report.supporting]

    def test_a_single_failure_cancels_the_holds(self):
        """A level that has ever broken does not get credit for holding."""
        report = score_zone(_support(),
                            self._features(prior_held=3, prior_failed=1), 0.62)
        assert "prior_hold" not in [i.name for i in report.supporting]
        assert "prior_failure" in [i.name for i in report.opposing]

    def test_failure_outweighs_a_hold(self):
        fail = score_zone(_support(), self._features(prior_failed=2), 0.62)
        hold = score_zone(_support(), self._features(prior_held=2), 0.62)
        fail_w = next(i.weight for i in fail.opposing if i.name == "prior_failure")
        hold_w = next(i.weight for i in hold.supporting if i.name == "prior_hold")
        assert fail_w == pytest.approx(hold_w * 2.0)

    def test_no_memory_means_no_rule(self):
        report = score_zone(_support(), self._features(), 0.62)
        names = {i.name for i in report.supporting} | {i.name for i in report.opposing}
        assert "prior_failure" not in names
        assert "prior_hold" not in names
