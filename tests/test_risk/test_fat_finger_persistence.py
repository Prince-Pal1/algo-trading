"""Regression tests for FatFingerGuard running-average state persistence.

History:
    2026-04-17: original bug — running average lived only in RAM, lost on
        restart. Fixed in commit e36e075 by mirroring into RiskState.
    2026-05-09: deeper bug surfaced — a single global running average was
        poisoned by small-qty fills from one strategy/symbol pair (e.g.
        funding_carry/BTCUSDT-CARRY at qty 5.679), locking out every
        vol_momentum signal (qty 300+) for 11 days. Fixed by switching
        to per-(strategy, symbol) averages with a warmup count threshold.

These tests exercise the load/save round-trip end-to-end through a
real SQLite file and verify cross-pair isolation.
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
    def test_pairs_dict_starts_empty_for_fresh_db(self, db_path):
        state = RiskState(db_path=db_path)
        state.load_from_db()
        assert state.fat_finger_avg_pairs == {}

    def test_update_avg_mirrors_into_state_per_pair(self, db_path):
        state = RiskState(db_path=db_path)
        guard = FatFingerGuard(RiskConfig(), state)
        for qty in [1000.0, 1500.0, 2000.0, 1200.0, 1800.0]:
            guard.update_avg_trade_size("vol_momentum", "DOTUSDT", qty)
        avg, count = state.fat_finger_avg_pairs["vol_momentum/DOTUSDT"]
        assert avg == pytest.approx(1500.0, rel=1e-6)
        assert count == 5

    def test_round_trip_through_disk(self, db_path):
        """Critical case: simulate a restart and verify per-pair averages survive."""
        state_before = RiskState(db_path=db_path)
        guard_before = FatFingerGuard(RiskConfig(), state_before)
        for qty in [1000.0, 1500.0, 2000.0, 1200.0, 1800.0]:
            guard_before.update_avg_trade_size("vol_momentum", "DOTUSDT", qty)
        for qty in [10.0, 20.0]:
            guard_before.update_avg_trade_size("funding_carry", "BTCUSDT-CARRY", qty)
        state_before.persist()

        state_after = RiskState(db_path=db_path)
        state_after.load_from_db()
        # Both pairs survive
        avg_v, count_v = state_after.fat_finger_avg_pairs["vol_momentum/DOTUSDT"]
        avg_f, count_f = state_after.fat_finger_avg_pairs["funding_carry/BTCUSDT-CARRY"]
        assert avg_v == pytest.approx(1500.0, rel=1e-6)
        assert count_v == 5
        assert avg_f == pytest.approx(15.0, rel=1e-6)
        assert count_f == 2

    def test_round_trip_doesnt_clobber_other_risk_state(self, db_path):
        state_before = RiskState(db_path=db_path)
        state_before.peak_equity = 12_345.67
        state_before.current_equity = 10_111.22
        state_before.kill_switch_active = True
        state_before.kill_switch_reason = "manual test"
        guard = FatFingerGuard(RiskConfig(), state_before)
        for qty in [500.0, 600.0, 700.0]:
            guard.update_avg_trade_size("test", "BTCUSDT", qty)
        state_before.persist()

        state_after = RiskState(db_path=db_path)
        state_after.load_from_db()
        assert state_after.peak_equity == 12_345.67
        assert state_after.current_equity == 10_111.22
        assert state_after.kill_switch_active is True
        assert state_after.kill_switch_reason == "manual test"
        avg, count = state_after.fat_finger_avg_pairs["test/BTCUSDT"]
        assert avg == pytest.approx(600.0, rel=1e-6)
        assert count == 3

    def test_legacy_scalar_keys_loaded_then_dropped(self, db_path):
        """Pre-2026-05-09 risk_state rows had fat_finger_avg_trade_size and
        fat_finger_trade_count as separate scalars. New code must:
        (a) not crash when loading them, (b) ignore their values (they
        carried the bug we're fixing), (c) delete them on next persist.
        """
        # Simulate an old-format DB
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("fat_finger_avg_trade_size", "14.235", "2026-04-28T00:00:37"),
        )
        conn.execute(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("fat_finger_trade_count", "2", "2026-04-28T00:00:37"),
        )
        conn.commit()
        conn.close()

        state = RiskState(db_path=db_path)
        state.load_from_db()
        # New format starts empty regardless of legacy scalars
        assert state.fat_finger_avg_pairs == {}

        # Persist: legacy keys should be deleted
        state.persist()
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT key FROM risk_state WHERE key IN "
            "('fat_finger_avg_trade_size', 'fat_finger_trade_count')"
        ).fetchall()
        conn.close()
        assert rows == []

    def test_memory_db_skips_persist_silently(self):
        state = RiskState(db_path=":memory:")
        guard = FatFingerGuard(RiskConfig(), state)
        guard.update_avg_trade_size("test", "BTCUSDT", 1234.0)
        state.persist()  # no-op
        state2 = RiskState(db_path=":memory:")
        state2.load_from_db()
        assert state2.fat_finger_avg_pairs == {}


class TestRegressionScenario:
    """Replay the 2026-05-09 cross-strategy poisoning bug."""

    def test_cross_strategy_small_fill_does_not_lock_other_pair(self, db_path):
        """The 2026-05-09 bug in canonical form: tiny BTCUSDT-CARRY fills
        from funding_carry must NOT cause vol_momentum/DOTUSDT signals
        to be rejected.
        """
        state = RiskState(db_path=db_path)
        cfg = RiskConfig(fat_finger_max_qty_mult=10.0, fat_finger_max_value=1_000_000_000)
        guard = FatFingerGuard(cfg, state)

        # Saturate funding_carry/BTCUSDT-CARRY with tiny fills past warmup.
        for _ in range(20):
            guard.update_avg_trade_size("funding_carry", "BTCUSDT-CARRY", 5.679)

        # State persists across a simulated restart.
        state.persist()
        state2 = RiskState(db_path=db_path)
        state2.load_from_db()
        guard2 = FatFingerGuard(cfg, state2)

        # vol_momentum/DOTUSDT pair has zero history → falls within warmup,
        # qty check is skipped, signal passes.
        from src.utils.types import Signal, SignalAction
        signal = Signal(
            symbol="DOTUSDT", action=SignalAction.LONG, confidence=0.9,
            strategy_name="vol_momentum", timeframe="1h",
            entry_price=1.25, stop_loss=1.20, risk_pct=0.01,
        )
        # Equity 100k, risk 1%, SL distance 0.05 → est qty = 1000/0.05 = 20_000
        state2.current_equity = 100_000.0
        assert guard2.check(signal) is None  # would have FAT_FINGER'd pre-fix

    def test_warmup_blocks_single_fill_lockup_on_same_pair(self, db_path):
        """Even on the SAME (strategy, symbol), a single small fill must
        not poison the guard. The warmup count keeps the qty check
        disabled until enough history accumulates.
        """
        state = RiskState(db_path=db_path)
        cfg = RiskConfig(fat_finger_max_qty_mult=10.0, fat_finger_max_value=1_000_000_000)
        guard = FatFingerGuard(cfg, state)

        # One tiny fill on vol_momentum/DOTUSDT.
        guard.update_avg_trade_size("vol_momentum", "DOTUSDT", 5.0)

        from src.utils.types import Signal, SignalAction
        signal = Signal(
            symbol="DOTUSDT", action=SignalAction.LONG, confidence=0.9,
            strategy_name="vol_momentum", timeframe="1h",
            entry_price=1.25, stop_loss=1.20, risk_pct=0.01,
        )
        state.current_equity = 100_000.0
        # Pre-fix this would have rejected. Post-fix: warmup skips the check.
        assert guard.check(signal) is None

    def test_qty_check_engages_after_warmup(self, db_path):
        """After the warmup count is reached, the qty check fires normally."""
        state = RiskState(db_path=db_path)
        cfg = RiskConfig(fat_finger_max_qty_mult=10.0, fat_finger_max_value=1_000_000_000)
        guard = FatFingerGuard(cfg, state)
        for _ in range(10):
            guard.update_avg_trade_size("vol_momentum", "DOTUSDT", 5.0)

        from src.utils.types import Signal, SignalAction
        signal = Signal(
            symbol="DOTUSDT", action=SignalAction.LONG, confidence=0.9,
            strategy_name="vol_momentum", timeframe="1h",
            entry_price=1.25, stop_loss=1.20, risk_pct=0.01,
        )
        state.current_equity = 100_000.0
        # est qty = 1000/0.05 = 20_000, avg=5, 10x=50 → reject
        result = guard.check(signal)
        assert result is not None
        assert "FAT_FINGER_QTY" in result
