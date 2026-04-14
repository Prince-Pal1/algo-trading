"""Unit tests for src/strategies/graveyard.py.

All tests use an in-memory SQLite (`db_path=":memory:"`) so nothing
touches `data/trades.db`. Each test gets a fresh DB because the
module's `_connect()` creates + installs schema on every call.
"""

from __future__ import annotations

import json

import pytest

from src.strategies.graveyard import (
    GraveyardEntry,
    append_revival_attempt,
    get_graveyard_entry,
    list_graveyard,
    record_kill,
)


def _sample_entry(
    name: str = "candle_burst_hunter",
    category: str = "NOISE",
    **overrides,
) -> GraveyardEntry:
    defaults = {
        "strategy_name": name,
        "kill_date": "2026-04-14",
        "kill_category": category,
        "kill_reason_short": "ATR filter degenerates at M1",
        "kill_commit": "be4e2d2",
        "root_cause_long": "At M1 cadence ATR_20 is ~0.5-1.5 so travel > 1.5*ATR qualifies every bar.",
        "revival_conditions": "Multi-bar velocity or tick-level.",
        "obituary_source_file": "src/strategies/aggressive/candle_burst_hunter.py",
        "backtest_metrics": {"return_pct": -100.0, "trades": 10434},
        "research_artifacts": [{"name": "tier5_m1_revival", "path": "data/tier5_m1_revival.json"}],
        "task_ids": [94, 100],
        "tags": ["aggressive", "tier5", "tick-blocked"],
    }
    defaults.update(overrides)
    return GraveyardEntry(**defaults)


class TestGraveyardEntryValidation:
    def test_valid_category_accepted(self):
        e = _sample_entry(category="NOISE")
        assert e.kill_category == "NOISE"

    def test_invalid_category_raises(self):
        with pytest.raises(ValueError, match="kill_category must be one of"):
            _sample_entry(category="FOO")

    def test_all_valid_categories_work(self):
        for cat in ("TIMING", "NOISE", "PREMISE", "INFRA", "REGIME"):
            e = _sample_entry(category=cat)
            assert e.kill_category == cat


class TestRecordKill:
    def test_record_kill_creates_row(self):
        db = ":memory:"
        e = _sample_entry()
        row_id = record_kill(e, db_path=db)
        # NOTE: in-memory DBs are per-connection, so we can't query back
        # from a fresh connection. But record_kill() returns the row id,
        # which must be > 0 on success.
        assert row_id > 0

    def test_record_kill_and_get_back(self, tmp_path):
        # Use a file-backed DB in tmp_path so multiple connections see the data
        db = str(tmp_path / "test.db")
        e = _sample_entry()
        row_id = record_kill(e, db_path=db)
        assert row_id > 0

        fetched = get_graveyard_entry("candle_burst_hunter", db_path=db)
        assert fetched is not None
        assert fetched.strategy_name == "candle_burst_hunter"
        assert fetched.kill_category == "NOISE"
        assert fetched.kill_reason_short == "ATR filter degenerates at M1"
        assert fetched.kill_commit == "be4e2d2"
        assert fetched.task_ids == [94, 100]
        assert fetched.tags == ["aggressive", "tier5", "tick-blocked"]
        assert fetched.backtest_metrics == {"return_pct": -100.0, "trades": 10434}
        assert fetched.research_artifacts == [
            {"name": "tier5_m1_revival", "path": "data/tier5_m1_revival.json"}
        ]
        assert fetched.revival_attempts == []

    def test_record_kill_upsert_is_idempotent(self, tmp_path):
        db = str(tmp_path / "test.db")
        e1 = _sample_entry()
        row_id_1 = record_kill(e1, db_path=db)

        # Same entry twice — should still be 1 row total
        e2 = _sample_entry()
        row_id_2 = record_kill(e2, db_path=db)

        assert row_id_1 == row_id_2  # same row

        entries = list_graveyard(db_path=db)
        assert len(entries) == 1

    def test_record_kill_upsert_updates_fields(self, tmp_path):
        db = str(tmp_path / "test.db")
        e1 = _sample_entry(kill_reason_short="original reason")
        record_kill(e1, db_path=db)

        # Update with new reason
        e2 = _sample_entry(kill_reason_short="updated reason")
        record_kill(e2, db_path=db)

        fetched = get_graveyard_entry("candle_burst_hunter", db_path=db)
        assert fetched is not None
        assert fetched.kill_reason_short == "updated reason"

    def test_record_kill_different_versions_coexist(self, tmp_path):
        db = str(tmp_path / "test.db")
        e1 = _sample_entry(strategy_version="v1")
        e2 = _sample_entry(strategy_version="v2", kill_reason_short="v2 kill")
        record_kill(e1, db_path=db)
        record_kill(e2, db_path=db)

        entries = list_graveyard(db_path=db)
        assert len(entries) == 2
        versions = {e.strategy_version for e in entries}
        assert versions == {"v1", "v2"}


class TestListGraveyard:
    def test_list_by_category(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(name="a", category="NOISE"), db_path=db)
        record_kill(_sample_entry(name="b", category="NOISE"), db_path=db)
        record_kill(_sample_entry(name="c", category="PREMISE"), db_path=db)

        noise = list_graveyard(category="NOISE", db_path=db)
        premise = list_graveyard(category="PREMISE", db_path=db)
        assert len(noise) == 2
        assert len(premise) == 1
        assert {e.strategy_name for e in noise} == {"a", "b"}
        assert {e.strategy_name for e in premise} == {"c"}

    def test_list_by_tag(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(name="a", tags=["aggressive", "tick-blocked"]), db_path=db)
        record_kill(_sample_entry(name="b", tags=["aggressive"]), db_path=db)
        record_kill(_sample_entry(name="c", tags=["institutional"]), db_path=db)

        aggressive = list_graveyard(tag="aggressive", db_path=db)
        tick_blocked = list_graveyard(tag="tick-blocked", db_path=db)
        assert {e.strategy_name for e in aggressive} == {"a", "b"}
        assert {e.strategy_name for e in tick_blocked} == {"a"}

    def test_list_empty_db(self, tmp_path):
        db = str(tmp_path / "empty.db")
        entries = list_graveyard(db_path=db)
        assert entries == []

    def test_list_orders_by_kill_date_desc(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(name="old", kill_date="2026-01-01"), db_path=db)
        record_kill(_sample_entry(name="new", kill_date="2026-04-14"), db_path=db)
        record_kill(_sample_entry(name="mid", kill_date="2026-03-01"), db_path=db)

        entries = list_graveyard(db_path=db)
        assert [e.strategy_name for e in entries] == ["new", "mid", "old"]


class TestGetGraveyardEntry:
    def test_not_found_returns_none(self, tmp_path):
        db = str(tmp_path / "test.db")
        assert get_graveyard_entry("ghost", db_path=db) is None

    def test_found_returns_entry(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(name="alpha"), db_path=db)
        fetched = get_graveyard_entry("alpha", db_path=db)
        assert fetched is not None
        assert fetched.strategy_name == "alpha"

    def test_wrong_version_returns_none(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(name="alpha", strategy_version="v1"), db_path=db)
        fetched = get_graveyard_entry("alpha", strategy_version="v2", db_path=db)
        assert fetched is None


class TestAppendRevivalAttempt:
    def test_append_grows_list(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(), db_path=db)

        append_revival_attempt(
            "candle_burst_hunter",
            {"date": "2026-04-14", "method": "M1 revival", "result": "KILL"},
            db_path=db,
        )

        fetched = get_graveyard_entry("candle_burst_hunter", db_path=db)
        assert fetched is not None
        assert len(fetched.revival_attempts) == 1
        assert fetched.revival_attempts[0]["method"] == "M1 revival"

    def test_append_preserves_prior(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(), db_path=db)

        for i in range(3):
            append_revival_attempt(
                "candle_burst_hunter",
                {"date": f"2026-04-{14+i}", "method": f"attempt-{i}", "result": "KILL"},
                db_path=db,
            )

        fetched = get_graveyard_entry("candle_burst_hunter", db_path=db)
        assert fetched is not None
        assert len(fetched.revival_attempts) == 3
        assert [a["method"] for a in fetched.revival_attempts] == [
            "attempt-0", "attempt-1", "attempt-2"
        ]

    def test_append_to_missing_row_raises(self, tmp_path):
        db = str(tmp_path / "test.db")
        with pytest.raises(ValueError, match="no graveyard row"):
            append_revival_attempt(
                "ghost",
                {"date": "2026-04-14", "method": "test", "result": "KILL"},
                db_path=db,
            )

    def test_append_non_dict_raises(self, tmp_path):
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(), db_path=db)
        with pytest.raises(TypeError, match="attempt must be a dict"):
            append_revival_attempt(
                "candle_burst_hunter",
                "not a dict",  # type: ignore[arg-type]
                db_path=db,
            )


class TestSchemaIdempotence:
    def test_schema_creation_multiple_times(self, tmp_path):
        """Calling record_kill twice (which installs schema twice) is safe."""
        db = str(tmp_path / "test.db")
        record_kill(_sample_entry(name="a"), db_path=db)
        record_kill(_sample_entry(name="b"), db_path=db)
        # If CREATE TABLE IF NOT EXISTS weren't idempotent, the 2nd call
        # would blow up with "table already exists". This test just
        # exercises the path.
        entries = list_graveyard(db_path=db)
        assert len(entries) == 2
