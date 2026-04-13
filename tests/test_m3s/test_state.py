"""Sub-phase 0.1 — tests for src/m3s/state.py SQLite store."""

from __future__ import annotations

import os

import pytest

from src.m3s.state import M3SStore
from src.m3s.types import M3SEventType


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "m3s_test.sqlite"
    s = M3SStore(str(db))
    yield s
    s.close()


class TestSchemaInit:
    def test_init_creates_parent_dir(self, tmp_path):
        # Parent doesn't exist yet — store must create it.
        nested = tmp_path / "nested" / "deeper" / "m3s.sqlite"
        assert not nested.parent.exists()
        s = M3SStore(str(nested))
        try:
            assert os.path.isfile(str(nested))
        finally:
            s.close()

    def test_init_is_idempotent(self, tmp_path):
        db = tmp_path / "m3s.sqlite"
        s1 = M3SStore(str(db))
        s1.put("compound", "hwm", 10_000.0)
        s1.close()

        # Re-open the same DB — schema must survive, data must persist
        s2 = M3SStore(str(db))
        try:
            assert s2.get("compound", "hwm") == 10_000.0
        finally:
            s2.close()


class TestKeyValueStore:
    def test_put_get_scalar(self, store):
        store.put("compound", "hwm", 12_345.67)
        assert store.get("compound", "hwm") == 12_345.67

    def test_put_get_json_dict(self, store):
        payload = {
            "weights": {"bb_rsi_mr_opt": 0.40, "donchian": 0.30, "vol_mom": 0.30},
            "method": "hrp_lite",
        }
        store.put("allocation", "latest", payload)
        out = store.get("allocation", "latest")
        assert out == payload
        assert out["weights"]["bb_rsi_mr_opt"] == 0.40

    def test_put_upsert_last_write_wins(self, store):
        store.put("mode", "current", "CONSERVATIVE")
        store.put("mode", "current", "GROWTH")
        assert store.get("mode", "current") == "GROWTH"
        # Still exactly one row per (namespace, key)
        assert store.list_keys("mode") == ["current"]

    def test_get_missing_returns_none(self, store):
        assert store.get("compound", "does_not_exist") is None
        assert store.list_keys("compound") == []


class TestEventLog:
    def test_append_event_returns_monotonic_ids(self, store):
        id1 = store.append_event(M3SEventType.ALLOCATION.value, {"w": {"a": 1.0}})
        id2 = store.append_event(M3SEventType.COMPOUND_UPDATE.value, {"hwm": 10_500.0})
        id3 = store.append_event(M3SEventType.DD_FREEZE.value, {"dd": 0.09})
        assert id1 < id2 < id3

    def test_query_events_filtered_by_type_and_time_window(self, store):
        store.append_event(
            M3SEventType.ALLOCATION.value, {"weights": {"a": 1.0}},
            ts_ms=1000,
        )
        store.append_event(
            M3SEventType.COMPOUND_UPDATE.value, {"hwm": 10_000.0},
            ts_ms=2000,
        )
        store.append_event(
            M3SEventType.ALLOCATION.value, {"weights": {"a": 0.5, "b": 0.5}},
            ts_ms=3000,
        )

        all_allocs = store.query_events(event_type=M3SEventType.ALLOCATION.value)
        assert len(all_allocs) == 2
        # Ordered chronologically
        assert all_allocs[0]["ts_ms"] == 1000
        assert all_allocs[1]["ts_ms"] == 3000
        assert all_allocs[1]["payload"]["weights"] == {"a": 0.5, "b": 0.5}

        # Time window excludes one
        mid = store.query_events(since_ms=1500, until_ms=2500)
        assert len(mid) == 1
        assert mid[0]["event_type"] == M3SEventType.COMPOUND_UPDATE.value

    def test_event_count(self, store):
        assert store.event_count() == 0
        store.append_event(M3SEventType.ALLOCATION.value, {})
        store.append_event(M3SEventType.ALLOCATION.value, {})
        store.append_event(M3SEventType.DD_HALT.value, {})
        assert store.event_count() == 3
        assert store.event_count(M3SEventType.ALLOCATION.value) == 2
        assert store.event_count(M3SEventType.DD_HALT.value) == 1
