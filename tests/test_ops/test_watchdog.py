"""Tests for the watchdog DATA_BLIND check (added 2026-06-08).

The DATA_BLIND check catches the failure mode that bit gold between
2026-05-18 and 2026-06-08: engine process alive, heartbeat file mtime
fresh, but the IC Markets feed receiving zero ticks because the OAuth
access token had expired. Neither the file-mtime kick nor the
candle-age kick fired, so the engine ran data-blind for 21 days.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "watchdog_mod", ROOT / "scripts" / "watchdog.py"
)
wd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wd)


def _write_hb(path: Path, *, uptime_s: float, tick_count: int,
              last_candle_age_s=None) -> None:
    path.write_text(json.dumps({
        "timestamp": "2026-06-08T08:00:00+00:00",
        "uptime_s": uptime_s,
        "tick_count": tick_count,
        "candle_count": 0,
        "open_positions": 0,
        "equity": 10000.0,
        "last_candle_age_s": last_candle_age_s,
        "risk_server_ok": True,
        "status": "HEALTHY",
    }))


class TestReadHeartbeatMetrics:
    def test_missing_file_returns_empty(self, tmp_path):
        assert wd._read_heartbeat_metrics(tmp_path / "nope.json") == {}

    def test_malformed_json_returns_empty(self, tmp_path):
        p = tmp_path / "hb.json"
        p.write_text("{not json")
        assert wd._read_heartbeat_metrics(p) == {}

    def test_valid_heartbeat_parses(self, tmp_path):
        p = tmp_path / "hb.json"
        _write_hb(p, uptime_s=300, tick_count=42)
        m = wd._read_heartbeat_metrics(p)
        assert m == {"tick_count": 42, "uptime_s": 300.0}


class TestIsDataBlind:
    def test_empty_metrics_is_not_blind(self):
        assert wd._is_data_blind({}) is False

    def test_fresh_engine_short_uptime_not_blind(self):
        # Engine just started — not enough time to judge.
        assert wd._is_data_blind({"uptime_s": 60, "tick_count": 0}) is False

    def test_long_uptime_with_ticks_not_blind(self):
        assert wd._is_data_blind({"uptime_s": 7200, "tick_count": 5000}) is False

    def test_long_uptime_zero_ticks_is_blind(self):
        # This is the gold-engine-after-token-expiry signature.
        assert wd._is_data_blind({"uptime_s": 7200, "tick_count": 0}) is True

    def test_boundary_just_above_threshold(self):
        # Threshold is 1800s; 1801 with zero ticks should fire.
        assert wd._is_data_blind({"uptime_s": 1801, "tick_count": 0}) is True

    def test_boundary_just_below_threshold(self):
        assert wd._is_data_blind({"uptime_s": 1799, "tick_count": 0}) is False


class TestCheckEngineDataBlind:
    """End-to-end: a heartbeat with high uptime + zero ticks must
    produce status=DATA_BLIND and trigger a notification, but NOT kick
    the engine (kicking won't recover an auth failure)."""

    def test_data_blind_does_not_kick(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        _write_hb(hb, uptime_s=3600, tick_count=0)
        engine = {
            "label": "gold",
            "heartbeat_path": hb,
            "launchd_label": "com.algo-trading.engine-gold",
        }

        kicked = {"called": False}

        def fake_kick(label):
            kicked["called"] = True
            return True

        monkeypatch.setattr(wd, "_kick_engine", fake_kick)
        monkeypatch.setattr(wd, "_macos_notify", lambda *a, **k: None)
        monkeypatch.setattr(wd, "_log_alert", lambda *a, **k: None)
        # Wipe state between tests
        wd._last_kick_at.clear()
        wd._last_data_blind_notify_at.clear()

        result = wd._check_engine(engine)
        assert result["status"] == "DATA_BLIND"
        assert result["tick_count"] == 0
        assert kicked["called"] is False  # critically — do NOT kick

    def test_data_blind_notify_cooldown(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        _write_hb(hb, uptime_s=3600, tick_count=0)
        engine = {
            "label": "gold",
            "heartbeat_path": hb,
            "launchd_label": "com.algo-trading.engine-gold",
        }

        notify_calls = []
        monkeypatch.setattr(wd, "_kick_engine", lambda *a: True)
        monkeypatch.setattr(wd, "_macos_notify",
                            lambda *a, **k: notify_calls.append(a))
        monkeypatch.setattr(wd, "_log_alert", lambda *a, **k: None)
        wd._last_kick_at.clear()
        wd._last_data_blind_notify_at.clear()

        wd._check_engine(engine)
        wd._check_engine(engine)
        # Second call in the same window must NOT notify again.
        assert len(notify_calls) == 1

    def test_healthy_engine_with_ticks_not_data_blind(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        _write_hb(hb, uptime_s=3600, tick_count=50_000)
        engine = {
            "label": "crypto",
            "heartbeat_path": hb,
            "launchd_label": "com.algo-trading.engine",
        }
        monkeypatch.setattr(wd, "_kick_engine", lambda *a: True)
        monkeypatch.setattr(wd, "_macos_notify", lambda *a, **k: None)
        monkeypatch.setattr(wd, "_log_alert", lambda *a, **k: None)
        wd._last_kick_at.clear()
        wd._last_data_blind_notify_at.clear()

        result = wd._check_engine(engine)
        assert result["status"] == "HEALTHY"
