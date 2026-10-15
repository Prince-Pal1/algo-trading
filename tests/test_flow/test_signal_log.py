"""Tests for src/flow/signal_log.py — outcomes and the null-hypothesis baseline.

The baseline records are the point. Every zone ENTRY is logged, not just the
ones the scorer confirms, so that "does the scorer beat simply taking the
level?" can be answered from the log rather than argued about.
"""

from __future__ import annotations

import orjson
import pytest

from src.flow.level_registry import Level, LevelSide
from src.flow.signal_log import PendingOutcome, SignalLog

TS = 1_757_000_000_000


def _log(tmp_path, **kw) -> SignalLog:
    return SignalLog(
        signal_path=tmp_path / "sig.jsonl",
        outcome_path=tmp_path / "out.jsonl",
        **kw,
    )


def _read(path):
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line]


def _level(side=LevelSide.LONG) -> Level:
    return Level("sup", "BTCUSDT", 98000.0, 100.0, side, note="pdl")


class _Signal:
    """Minimal stand-in carrying the fields SignalLog reads."""

    def __init__(self, side=LevelSide.LONG, price=98000.0, invalidation=97800.0):
        self.side = side
        self.price = price
        self.timestamp = TS
        self.score = 0.8
        self.invalidation = invalidation
        self.level_id = "sup"

    def to_dict(self):
        return {
            "level_id": self.level_id, "symbol": "BTCUSDT", "side": self.side.value,
            "timestamp": self.timestamp, "price": self.price, "score": self.score,
            "invalidation": self.invalidation,
        }


class TestPendingOutcome:
    def _long(self) -> PendingOutcome:
        return PendingOutcome("r1", "sup", "long", 98000.0, TS, 97800.0, 98400.0, "signal")

    def test_unresolved_while_between(self):
        assert self._long().update(98100.0) is None

    def test_target_hit(self):
        assert self._long().update(98400.0) == "target"

    def test_invalidation_hit(self):
        assert self._long().update(97800.0) == "invalidation"

    def test_short_target_is_below(self):
        p = PendingOutcome("r1", "sup", "short", 98000.0, TS, 98200.0, 97600.0, "signal")
        assert p.update(97600.0) == "target"

    def test_short_invalidation_is_above(self):
        p = PendingOutcome("r1", "sup", "short", 98000.0, TS, 98200.0, 97600.0, "signal")
        assert p.update(98200.0) == "invalidation"

    def test_mfe_and_mae_long(self):
        p = self._long()
        p.update(98150.0)
        p.update(97900.0)
        assert p.mfe() == pytest.approx(150.0)
        assert p.mae() == pytest.approx(100.0)

    def test_mfe_and_mae_short(self):
        p = PendingOutcome("r1", "sup", "short", 98000.0, TS, 98200.0, 97600.0, "signal")
        p.update(97850.0)
        p.update(98100.0)
        assert p.mfe() == pytest.approx(150.0)
        assert p.mae() == pytest.approx(100.0)

    def test_excursions_never_negative(self):
        p = self._long()
        p.update(98000.0)
        assert p.mfe() >= 0 and p.mae() >= 0


class TestSignalRecording:
    def test_signal_written(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_signal(_Signal())
        rows = _read(slog.signal_path)
        assert len(rows) == 1
        assert rows[0]["kind"] == "signal"

    def test_signal_becomes_pending(self, tmp_path):
        slog = _log(tmp_path)
        assert slog.record_signal(_Signal()) in slog.pending

    def test_target_derived_from_risk(self, tmp_path):
        slog = _log(tmp_path, target_mult=2.0)
        rid = slog.record_signal(_Signal(price=98000.0, invalidation=97800.0))
        assert slog.pending[rid].target == pytest.approx(98400.0)

    def test_short_target_direction(self, tmp_path):
        slog = _log(tmp_path, target_mult=2.0)
        rid = slog.record_signal(
            _Signal(side=LevelSide.SHORT, price=98000.0, invalidation=98200.0)
        )
        assert slog.pending[rid].target == pytest.approx(97600.0)

    def test_record_ids_unique(self, tmp_path):
        slog = _log(tmp_path)
        assert slog.record_signal(_Signal()) != slog.record_signal(_Signal())


class TestBaselineRecording:
    def test_baseline_written(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_baseline(_level(), 98000.0, TS, 97800.0)
        assert _read(slog.signal_path)[0]["kind"] == "baseline"

    def test_baseline_has_no_score(self, tmp_path):
        slog = _log(tmp_path)
        rid = slog.record_baseline(_level(), 98000.0, TS, 97800.0)
        assert slog.pending[rid].score is None

    def test_baseline_tracked_like_a_signal(self, tmp_path):
        slog = _log(tmp_path)
        rid = slog.record_baseline(_level(), 98000.0, TS, 97800.0)
        assert slog.pending[rid].kind == "baseline"

    def test_signals_and_baselines_coexist(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_baseline(_level(), 98000.0, TS, 97800.0)
        slog.record_signal(_Signal())
        assert {r["kind"] for r in _read(slog.signal_path)} == {"baseline", "signal"}


class TestOutcomeResolution:
    def test_resolves_on_target(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_signal(_Signal())
        resolved = slog.update_prices(98400.0, TS + 60_000)
        assert resolved[0]["outcome"] == "target"
        assert resolved[0]["win"] is True

    def test_resolves_on_invalidation(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_signal(_Signal())
        resolved = slog.update_prices(97800.0, TS + 60_000)
        assert resolved[0]["win"] is False

    def test_outcome_written_to_file(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_signal(_Signal())
        slog.update_prices(98400.0, TS + 60_000)
        assert len(_read(slog.outcome_path)) == 1

    def test_resolved_removed_from_pending(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_signal(_Signal())
        slog.update_prices(98400.0, TS + 1000)
        assert slog.pending == {}

    def test_held_ms_recorded(self, tmp_path):
        slog = _log(tmp_path)
        slog.record_signal(_Signal())
        assert slog.update_prices(98400.0, TS + 90_000)[0]["held_ms"] == 90_000

    def test_kind_preserved_through_resolution(self, tmp_path):
        """Outcome rows must stay attributable to signal vs baseline."""
        slog = _log(tmp_path)
        slog.record_baseline(_level(), 98000.0, TS, 97800.0)
        assert slog.update_prices(98400.0, TS + 1000)[0]["kind"] == "baseline"

    def test_no_pending_is_noop(self, tmp_path):
        assert _log(tmp_path).update_prices(98000.0, TS) == []

    def test_both_kinds_resolve_independently(self, tmp_path):
        slog = _log(tmp_path, target_mult=2.0)
        slog.record_signal(_Signal(price=98000.0, invalidation=97800.0))
        slog.record_baseline(_level(), 97950.0, TS, 97800.0)
        resolved = slog.update_prices(98400.0, TS + 1000)
        assert {r["kind"] for r in resolved} == {"signal", "baseline"}


class TestRobustness:
    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        """Logging must never take down the monitor."""
        slog = _log(tmp_path)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("pathlib.Path.open", boom)
        slog.record_signal(_Signal())

    def test_creates_parent_directories(self, tmp_path):
        slog = SignalLog(
            signal_path=tmp_path / "deep" / "nested" / "sig.jsonl",
            outcome_path=tmp_path / "deep" / "nested" / "out.jsonl",
        )
        assert slog.signal_path.parent.exists()
