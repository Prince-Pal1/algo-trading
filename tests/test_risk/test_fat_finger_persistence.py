"""Regression tests for FatFingerGuard running-average state persistence.

Bug pre-fix (discovered via paper-engine investigation 2026-04-17):
    FatFingerGuard._avg_trade_size lived only in RAM. On every process
    restart it reset to 0.0, and the first small post-restart signal
    (quantity=31.20 in our case) set the running average to itself.
    Every subsequent normal-sized signal (quantity 1400-5000) tripped
    the "> 10× avg" rule and was rejected — forever, until manual fix.

Fix: mirror the running average into RiskState, which is already
persisted to the SQLite risk_state key-value table. When the risk
manager restarts, it loads the average from disk instead of starting
fresh.

These tests exercise the load/save round-trip end-to-end through a
real SQLite file and verify that a second RiskState + FatFingerGuard
instantiated against the same DB sees the same average.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.risk.config import RiskConfig
from src.risk.fat_finger import FatFingerGuard
from src.risk.state import RiskState


_RISK_STATE_SQL = """
CREATE TABLE IF NOT EXISTS risk_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


@pytest.fixture
def db_path(tmp_path) -> str:
    """A fresh SQLite file with the risk_state table seeded."""
    p = tmp_path / "trades.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_RISK_STATE_SQL)
    conn.commit()
    conn.close()
    return str(p)


class TestStatePersistence:
    def test_fat_finger_fields_start_at_zero_for_fresh_db(self, db_path):
        state = RiskState(db_path=db_path)
        state.load_from_db()
        assert state.fat_finger_avg_trade_size == 0.0
        assert state.fat_finger_trade_count == 0

    def test_update_avg_mirrors_into_state(self, db_path):
        state = RiskState(db_path=db_path)
        guard = FatFingerGuard(RiskConfig(), state)

        # Feed 5 trades, running average should be 1500
        for qty in [1000.0, 1500.0, 2000.0, 1200.0, 1800.0]:
            guard.update_avg_trade_size(qty)

        assert state.fat_finger_avg_trade_size == pytest.approx(1500.0, rel=1e-6)
        assert state.fat_finger_trade_count == 5
        assert guard._avg_trade_size == pytest.approx(1500.0, rel=1e-6)
        assert guard._trade_count == 5

    def test_round_trip_through_disk(self, db_path):
        """The critical case: simulate a restart and verify avg survives."""
        # 1) Pre-restart state: guard builds up an average
        state_before = RiskState(db_path=db_path)
        guard_before = FatFingerGuard(RiskConfig(), state_before)
        for qty in [1000.0, 1500.0, 2000.0, 1200.0, 1800.0]:
            guard_before.update_avg_trade_size(qty)
        state_before.persist()

        # 2) Simulate restart: fresh RiskState + fresh FatFingerGuard
        #    against the same on-disk DB
        state_after = RiskState(db_path=db_path)
        state_after.load_from_db()
        guard_after = FatFingerGuard(RiskConfig(), state_after)

        # 3) The new guard must see the old average, not 0.0
        assert guard_after._avg_trade_size == pytest.approx(1500.0, rel=1e-6)
        assert guard_after._trade_count == 5
        # And the state object reflects it too
        assert state_after.fat_finger_avg_trade_size == pytest.approx(1500.0, rel=1e-6)
        assert state_after.fat_finger_trade_count == 5

    def test_round_trip_doesnt_clobber_other_risk_state(self, db_path):
        """Adding fat-finger keys must not disturb the existing persisted keys
        (peak_equity, current_equity, kill_switch, etc.)."""
        state_before = RiskState(db_path=db_path)
        state_before.peak_equity = 12_345.67
        state_before.current_equity = 10_111.22
        state_before.kill_switch_active = True
        state_before.kill_switch_reason = "manual test"
        guard = FatFingerGuard(RiskConfig(), state_before)
        for qty in [500.0, 600.0, 700.0]:
            guard.update_avg_trade_size(qty)
        state_before.persist()

        state_after = RiskState(db_path=db_path)
        state_after.load_from_db()
        assert state_after.peak_equity == 12_345.67
        assert state_after.current_equity == 10_111.22
        assert state_after.kill_switch_active is True
        assert state_after.kill_switch_reason == "manual test"
        assert state_after.fat_finger_avg_trade_size == pytest.approx(600.0, rel=1e-6)
        assert state_after.fat_finger_trade_count == 3

    def test_memory_db_skips_persist_silently(self):
        """Existing tests use db_path=':memory:' as a sentinel to skip
        persistence. That path must still work (persist() is a no-op)."""
        state = RiskState(db_path=":memory:")
        guard = FatFingerGuard(RiskConfig(), state)
        guard.update_avg_trade_size(1234.0)
        # Should not raise
        state.persist()
        # And fresh in-memory state starts clean
        state2 = RiskState(db_path=":memory:")
        state2.load_from_db()  # Should not find anything, no error
        assert state2.fat_finger_avg_trade_size == 0.0


class TestRegressionScenario:
    """Replay the 2026-04-17 bug: small signal sets avg low, then a larger
    signal gets wrongly rejected. Post-fix, if the large signal is a fill
    (goes through update_avg_trade_size), the avg recovers."""

    def test_small_followed_by_large_fills_avg_recovers(self, db_path):
        state = RiskState(db_path=db_path)
        cfg = RiskConfig(fat_finger_max_qty_mult=10.0)
        guard = FatFingerGuard(cfg, state)

        # Pre-bug state: one small trade sets avg to 31.20
        guard.update_avg_trade_size(31.20)
        assert guard._avg_trade_size == pytest.approx(31.20, rel=1e-6)

        # A normal-sized fill follows (1500) → updates average
        guard.update_avg_trade_size(1500.0)
        # Running avg is now ~765.6 (average of 31.20 and 1500)
        assert guard._avg_trade_size == pytest.approx((31.20 + 1500.0) / 2, rel=1e-6)
        assert guard._trade_count == 2

        # After a few more normal-sized trades, the skew from 31.20 washes out
        for _ in range(10):
            guard.update_avg_trade_size(1500.0)
        # Should now be close to 1500
        assert guard._avg_trade_size > 1200.0

    def test_restart_after_small_first_fill_recovers_from_disk(self, db_path):
        """The actual bug: engine restarts → small first signal → avg=31.20
        → subsequent large signals rejected. Post-fix, if we have a durable
        prior average on disk, the small first signal just contributes to
        an already-populated history, not resets it."""
        # Build up a healthy average before the restart
        state_before = RiskState(db_path=db_path)
        guard_before = FatFingerGuard(RiskConfig(), state_before)
        for qty in [1500.0] * 20:
            guard_before.update_avg_trade_size(qty)
        state_before.persist()
        assert guard_before._avg_trade_size == pytest.approx(1500.0, rel=1e-6)

        # Simulate restart + the original 31.20-sized anomaly signal
        state_after = RiskState(db_path=db_path)
        state_after.load_from_db()
        guard_after = FatFingerGuard(RiskConfig(), state_after)

        # Pre-fix, avg would have been 0.0 here. Post-fix, it's 1500 from disk.
        assert guard_after._avg_trade_size == pytest.approx(1500.0, rel=1e-6)

        # If that anomalous 31.20 signal now fills (for whatever reason),
        # the running average shifts only slightly — not back to ~31
        guard_after.update_avg_trade_size(31.20)
        assert guard_after._avg_trade_size > 1000.0  # still robust
