"""Tests for scripts/m3s_shadow_check.py — the 6h shadow validator."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from src.m3s.state import M3SStore
from src.m3s.types import M3SEventType


_MS_PER_DAY = 86_400_000


def _load_checker_module():
    """Load scripts/m3s_shadow_check.py as a module (it's not a package).

    Must register in sys.modules BEFORE exec_module so that the @dataclass
    decorator can resolve type annotations against the module's namespace.
    """
    if "m3s_shadow_check" in sys.modules:
        return sys.modules["m3s_shadow_check"]
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "m3s_shadow_check.py"
    spec = importlib.util.spec_from_file_location("m3s_shadow_check", script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["m3s_shadow_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker(monkeypatch, tmp_path):
    """Load the checker with DATA_DIR monkey-patched to a tmp path."""
    module = _load_checker_module()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(module, "DATA_DIR", data_dir)
    monkeypatch.setattr(module, "M3S_DB", data_dir / "m3s.sqlite")
    monkeypatch.setattr(module, "TRADES_DB", data_dir / "trades.db")
    monkeypatch.setattr(module, "REPORT_MD", data_dir / "m3s_shadow_report.md")
    monkeypatch.setattr(module, "STATUS_JSON", data_dir / "m3s_shadow_status.json")
    monkeypatch.setattr(module, "ALERTS_LOG", data_dir / "m3s_shadow_alerts.log")
    monkeypatch.setattr(module, "_CLOCK_FILE", data_dir / "m3s_shadow_clock.json")
    return module


# ══════════════════════════════════════════════════════════════════════
# Disabled state
# ══════════════════════════════════════════════════════════════════════


class TestDisabledState:
    def test_disabled_when_store_missing(self, checker):
        report = checker.run_checks(window_days=7)
        assert report.overall_status == "DISABLED"
        assert len(report.checks) == 1
        assert report.checks[0].status == "SKIP"

    def test_disabled_report_writes_markdown(self, checker):
        report = checker.run_checks(window_days=7)
        checker.write_markdown_report(report)
        content = checker.REPORT_MD.read_text()
        assert "DISABLED" in content
        assert "enabled = true" in content


# ══════════════════════════════════════════════════════════════════════
# Healthy state
# ══════════════════════════════════════════════════════════════════════


class TestHealthyState:
    def _populate_healthy(self, store: M3SStore):
        """Seed a store with a clean set of events + state."""
        now_ms = int(time.time() * 1000)

        # Compound state
        store.put("compound", "state", {
            "base_equity": 10_500.0,
            "hwm": 10_500.0,
            "last_updated_ts_ms": now_ms,
            "mode": "STANDARD",
        })
        store.put("mode", "current", "STANDARD")
        store.put("allocation", "latest", {
            "ts_ms": now_ms,
            "weights": {"a": 0.35, "b": 0.30, "c": 0.30},
            "method": "hrp_lite_ledoit_wolf",
            "inputs_hash": "abc123",
            "reasoning": "healthy test",
        })

        # Events — 3 allocation rebalances + 3 compound updates + 1 signal
        for i in range(3):
            ts = now_ms - (3 - i) * 86400_000  # one per day
            store.append_event(
                M3SEventType.ALLOCATION.value,
                {
                    "weights": {"a": 0.35 + i * 0.01, "b": 0.30, "c": 0.30},
                    "method": "hrp_lite_ledoit_wolf",
                    "inputs_hash": f"hash{i}",
                },
                ts_ms=ts,
            )
            store.append_event(
                M3SEventType.COMPOUND_UPDATE.value,
                {
                    "old_base": 10_000.0 + i * 200,
                    "new_base": 10_000.0 + (i + 1) * 200,
                    "new_hwm": 10_000.0 + (i + 1) * 200,
                    "reason": "advanced",
                },
                ts_ms=ts,
            )

    def test_healthy_all_checks_pass(self, checker):
        store = M3SStore(str(checker.M3S_DB))
        try:
            self._populate_healthy(store)
        finally:
            store.close()

        # Also create an empty trades.db with paper_equity for divergence check
        import sqlite3
        conn = sqlite3.connect(str(checker.TRADES_DB))
        conn.execute(
            "CREATE TABLE paper_equity (id INTEGER PRIMARY KEY, equity REAL, "
            "initial_capital REAL, trade_count INTEGER)"
        )
        conn.execute("INSERT INTO paper_equity VALUES (1, 10500.0, 10000.0, 3)")
        conn.commit()
        conn.close()

        report = checker.run_checks(window_days=7)
        assert report.overall_status in ("HEALTHY", "WARNING")
        # HWM monotonic, cluster caps, no_dd_frozen should all pass
        pass_names = {c.name for c in report.checks if c.status == "PASS"}
        assert "hwm_monotonic" in pass_names
        assert "cluster_caps" in pass_names
        assert "no_dd_frozen_compound" in pass_names


# ══════════════════════════════════════════════════════════════════════
# Invariant violations
# ══════════════════════════════════════════════════════════════════════


class TestInvariantViolations:
    def test_hwm_decrease_flagged(self, checker):
        store = M3SStore(str(checker.M3S_DB))
        try:
            now_ms = int(time.time() * 1000)
            store.put("compound", "state", {
                "base_equity": 10_000.0, "hwm": 10_500.0,
                "last_updated_ts_ms": now_ms, "mode": "STANDARD",
            })
            store.put("mode", "current", "STANDARD")
            # Two compound updates, second has LOWER HWM (bug!)
            store.append_event(M3SEventType.COMPOUND_UPDATE.value, {
                "old_base": 10_000.0, "new_base": 10_500.0,
                "new_hwm": 10_500.0, "reason": "advanced",
            }, ts_ms=now_ms - 86400_000)
            store.append_event(M3SEventType.COMPOUND_UPDATE.value, {
                "old_base": 10_500.0, "new_base": 10_200.0,
                "new_hwm": 10_200.0, "reason": "advanced",   # violation
            }, ts_ms=now_ms)
        finally:
            store.close()

        report = checker.run_checks(window_days=7)
        assert report.overall_status == "ERROR"
        hwm_check = next(c for c in report.checks if c.name == "hwm_monotonic")
        assert hwm_check.status == "FAIL"

    def test_cluster_cap_violation_flagged(self, checker):
        store = M3SStore(str(checker.M3S_DB))
        try:
            now_ms = int(time.time() * 1000)
            store.put("compound", "state", {
                "base_equity": 10_000.0, "hwm": 10_000.0,
                "last_updated_ts_ms": now_ms, "mode": "STANDARD",
            })
            store.put("mode", "current", "STANDARD")
            # Allocation with weight > STANDARD cap (0.40)
            store.append_event(M3SEventType.ALLOCATION.value, {
                "weights": {"a": 0.75, "b": 0.15, "c": 0.10},
                "method": "hrp_lite",
                "inputs_hash": "bad",
            }, ts_ms=now_ms)
        finally:
            store.close()

        report = checker.run_checks(window_days=7)
        cap_check = next(c for c in report.checks if c.name == "cluster_caps")
        assert cap_check.status == "FAIL"
        assert report.overall_status == "ERROR"

    def test_frozen_compound_advance_flagged(self, checker):
        store = M3SStore(str(checker.M3S_DB))
        try:
            now_ms = int(time.time() * 1000)
            store.put("compound", "state", {
                "base_equity": 10_500.0, "hwm": 10_500.0,
                "last_updated_ts_ms": now_ms, "mode": "STANDARD",
            })
            store.put("mode", "current", "STANDARD")
            # Frozen reason but base advanced — bug
            store.append_event(M3SEventType.COMPOUND_UPDATE.value, {
                "old_base": 10_000.0, "new_base": 10_500.0,
                "new_hwm": 10_500.0, "reason": "frozen_drawdown",  # contradiction
            }, ts_ms=now_ms)
        finally:
            store.close()

        report = checker.run_checks(window_days=7)
        fz_check = next(c for c in report.checks if c.name == "no_dd_frozen_compound")
        assert fz_check.status == "FAIL"


# ══════════════════════════════════════════════════════════════════════
# Divergence
# ══════════════════════════════════════════════════════════════════════


class TestDivergence:
    def test_large_divergence_warns(self, checker):
        store = M3SStore(str(checker.M3S_DB))
        try:
            store.put("compound", "state", {
                "base_equity": 12_000.0, "hwm": 12_000.0,
                "last_updated_ts_ms": int(time.time() * 1000),
                "mode": "STANDARD",
            })
            store.put("mode", "current", "STANDARD")
        finally:
            store.close()

        import sqlite3
        conn = sqlite3.connect(str(checker.TRADES_DB))
        conn.execute(
            "CREATE TABLE paper_equity (id INTEGER PRIMARY KEY, equity REAL, "
            "initial_capital REAL, trade_count INTEGER)"
        )
        conn.execute("INSERT INTO paper_equity VALUES (1, 10000.0, 10000.0, 0)")
        conn.commit()
        conn.close()

        report = checker.run_checks(window_days=7)
        div_check = next(c for c in report.checks if c.name == "shadow_vs_actual")
        assert div_check.status == "WARN"
        assert "divergence" in div_check.detail.lower()


# ══════════════════════════════════════════════════════════════════════
# Promotion clock
# ══════════════════════════════════════════════════════════════════════


class TestPromotionClock:
    def test_clock_increments_on_healthy(self, checker):
        # First: populate healthy store
        store = M3SStore(str(checker.M3S_DB))
        try:
            now_ms = int(time.time() * 1000)
            store.put("compound", "state", {
                "base_equity": 10_000.0, "hwm": 10_000.0,
                "last_updated_ts_ms": now_ms, "mode": "STANDARD",
            })
            store.put("mode", "current", "STANDARD")
            store.append_event(M3SEventType.ALLOCATION.value, {
                "weights": {"a": 0.33, "b": 0.33, "c": 0.33},
                "method": "hrp_lite", "inputs_hash": "ok",
            }, ts_ms=now_ms)
        finally:
            store.close()

        report = checker.run_checks(window_days=7)
        checker._update_clock(report.overall_status)
        days = checker._read_clock_days()
        assert days >= 1

    def test_clock_resets_on_error(self, checker):
        # Seed clock at 5 days
        checker._CLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        checker._CLOCK_FILE.write_text(json.dumps({
            "consecutive_clean_days": 5,
            "last_update_day": "2020-01-01",
            "last_status": "HEALTHY",
        }))
        # Now fire an ERROR
        checker._update_clock("ERROR")
        assert checker._read_clock_days() == 0
