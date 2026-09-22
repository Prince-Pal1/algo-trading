"""Tests for editing levels from the live page.

The page writes through to config/levels.toml, so these cover the two places
that can quietly lose an operator's intent:

  · write-back — a level edited in the UI must survive a restart, and its
    `first_seen_ms` must survive with it, because that timestamp is the only
    evidence the level was written before price arrived
  · partial edits — the form carries price, width, side and note. Anything it
    does NOT carry (enabled, expires) must be inherited, not defaulted
"""

from __future__ import annotations

import argparse
import tomllib

import pytest

from src.flow.level_registry import Level, LevelRegistry, LevelSide
from src.flow.live_server import LiveServer

TS = 1_757_000_000_000
DAY = 86_400_000


def _registry(tmp_path) -> LevelRegistry:
    return LevelRegistry(path=tmp_path / "levels.toml")


def _level(lid: str = "sup", price: float = 98_000.0, **kw) -> Level:
    kw.setdefault("side", LevelSide.LONG)
    return Level(lid, "BTCUSDT", price, kw.pop("width", 150.0), note=kw.pop("note", ""), **kw)


# ── write-back ─────────────────────────────────────────────────────────

class TestWriteBack:
    def test_upsert_creates_a_readable_file(self, tmp_path):
        reg = _registry(tmp_path)
        reg.upsert(_level(note="prior day low"), now_ms=TS)
        raw = tomllib.loads((tmp_path / "levels.toml").read_text())
        assert raw["level"][0]["id"] == "sup"
        assert raw["level"][0]["note"] == "prior day low"

    def test_a_note_with_quotes_survives(self, tmp_path):
        reg = _registry(tmp_path)
        reg.upsert(_level(note='the "real" low \\ 4h'), now_ms=TS)
        fresh = _registry(tmp_path)
        fresh.load(now_ms=TS, force=True)
        assert fresh.get("sup").note == 'the "real" low \\ 4h'

    def test_first_seen_survives_a_restart(self, tmp_path):
        """Without this, every restart re-stamps provenance with today's clock
        and 'written before price arrived' becomes unfalsifiable."""
        reg = _registry(tmp_path)
        reg.upsert(_level(), now_ms=TS)
        fresh = _registry(tmp_path)
        fresh.load(now_ms=TS + 30 * DAY, force=True)
        assert fresh.get("sup").first_seen_ms == pytest.approx(TS, abs=1000)

    def test_editing_keeps_the_original_first_seen(self, tmp_path):
        reg = _registry(tmp_path)
        reg.upsert(_level(), now_ms=TS)
        reg.upsert(_level(price=98_050.0), now_ms=TS + DAY)
        assert reg.get("sup").first_seen_ms == pytest.approx(TS, abs=1000)
        assert reg.get("sup").price == 98_050.0

    def test_a_new_level_is_stamped_now(self, tmp_path):
        reg = _registry(tmp_path)
        reg.upsert(_level("other"), now_ms=TS + DAY)
        assert reg.get("other").first_seen_ms == TS + DAY

    def test_remove_and_set_enabled_persist(self, tmp_path):
        reg = _registry(tmp_path)
        reg.upsert(_level(), now_ms=TS)
        reg.upsert(_level("res", price=101_500.0), now_ms=TS)
        assert reg.set_enabled("sup", False) is True
        assert reg.remove("res") is True
        fresh = _registry(tmp_path)
        fresh.load(now_ms=TS, force=True)
        assert fresh.get("res") is None
        assert fresh.get("sup").enabled is False

    def test_removing_an_unknown_level_is_false_not_an_error(self, tmp_path):
        reg = _registry(tmp_path)
        assert reg.remove("nope") is False
        assert reg.set_enabled("nope", True) is False

    def test_expiry_round_trips(self, tmp_path):
        reg = _registry(tmp_path)
        reg.upsert(_level(expires_ms=TS + 7 * DAY), now_ms=TS)
        fresh = _registry(tmp_path)
        fresh.load(now_ms=TS, force=True)
        assert fresh.get("sup").expires_ms == pytest.approx(TS + 7 * DAY, abs=1000)

    def test_save_adopts_its_own_mtime(self, tmp_path):
        """Otherwise the reload timer re-reads a file we just wrote."""
        reg = _registry(tmp_path)
        reg.upsert(_level(), now_ms=TS)
        assert reg.load(now_ms=TS) is False


# ── the command handler ────────────────────────────────────────────────

def _monitor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from scripts.flow_monitor import FlowMonitor
    args = argparse.Namespace(
        symbol="BTCUSDT", levels=tmp_path / "levels.toml", threshold=0.62,
        target_mult=2.0, depth=False, level_memory=False, no_turn=False,
        port=8799, hz=5.0,
    )
    return FlowMonitor(args)


class TestCommands:
    def test_add_then_park_then_edit_keeps_it_parked(self, tmp_path, monkeypatch):
        """The form has no `enabled` box. Defaulting it to True means editing a
        note silently re-arms a level you deliberately parked."""
        mon = _monitor(tmp_path, monkeypatch)
        added = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long", "note": "pdl"}})
        assert added["ok"] is True
        lid = added["id"]
        assert mon._on_command({"cmd": "level.enable", "id": lid, "enabled": False})["ok"]
        mon._on_command({"cmd": "level.upsert", "level": {
            "id": lid, "price": 98_000.0, "width": 150.0, "side": "long",
            "note": "edited while parked"}})
        assert mon.registry.get(lid).enabled is False
        assert mon.registry.get(lid).note == "edited while parked"

    def test_editing_does_not_clear_an_expiry(self, tmp_path, monkeypatch):
        mon = _monitor(tmp_path, monkeypatch)
        mon.registry.upsert(_level(expires_ms=TS + 7 * DAY), now_ms=TS)
        mon._on_command({"cmd": "level.upsert", "level": {
            "id": "sup", "price": 98_000.0, "width": 150.0, "side": "long",
            "note": "still here"}})
        assert mon.registry.get("sup").expires_ms == TS + 7 * DAY

    def test_parking_removes_it_from_the_engine(self, tmp_path, monkeypatch):
        mon = _monitor(tmp_path, monkeypatch)
        lid = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})["id"]
        assert lid in mon.engine.monitors
        mon._on_command({"cmd": "level.enable", "id": lid, "enabled": False})
        assert lid not in mon.engine.monitors

    def test_generated_ids_do_not_collide(self, tmp_path, monkeypatch):
        """Outcomes are keyed by id — a collision merges two levels' histories."""
        mon = _monitor(tmp_path, monkeypatch)
        first = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})["id"]
        second = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 40.0, "side": "long"}})["id"]
        assert first != second
        assert len(mon.registry.levels) == 2

    @pytest.mark.parametrize("bad, message", [
        ({"price": 0, "width": 10}, "price"),
        ({"price": 98_000.0, "width": 0}, "width"),
        ({"price": "abc", "width": 10}, "numbers"),
        ({"price": 98_000.0, "width": 10, "side": "sideways"}, "long or short"),
    ])
    def test_bad_input_is_refused_with_a_readable_message(
            self, tmp_path, monkeypatch, bad, message):
        mon = _monitor(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match=message):
            mon._on_command({"cmd": "level.upsert", "level": bad})
        assert mon.registry.levels == {}

    def test_unknown_command_is_refused(self, tmp_path, monkeypatch):
        mon = _monitor(tmp_path, monkeypatch)
        assert mon._on_command({"cmd": "level.drop_table"})["ok"] is False

    def test_removing_an_unknown_level_answers_rather_than_raising(
            self, tmp_path, monkeypatch):
        mon = _monitor(tmp_path, monkeypatch)
        result = mon._on_command({"cmd": "level.remove", "id": "ghost"})
        assert result["ok"] is False and "ghost" in result["error"]

    def test_threshold_reaches_live_monitors(self, tmp_path, monkeypatch):
        """Setting it on the engine alone would leave a zone already being
        evaluated scoring against the old threshold."""
        mon = _monitor(tmp_path, monkeypatch)
        lid = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})["id"]
        assert mon._on_command({"cmd": "config.set", "threshold": 0.8})["ok"]
        assert mon.engine.threshold == 0.8
        assert mon.engine.monitors[lid].threshold == 0.8

    def test_require_turn_reaches_live_monitors(self, tmp_path, monkeypatch):
        mon = _monitor(tmp_path, monkeypatch)
        lid = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})["id"]
        mon._on_command({"cmd": "config.set", "require_turn": False})
        assert mon.engine.monitors[lid].require_turn is False

    def test_out_of_range_threshold_is_refused(self, tmp_path, monkeypatch):
        mon = _monitor(tmp_path, monkeypatch)
        assert mon._on_command({"cmd": "config.set", "threshold": 1.5})["ok"] is False
        assert mon.engine.threshold == 0.62

    def test_enabling_memory_mid_session_loads_the_file(self, tmp_path, monkeypatch):
        """It was constructed disabled and skipped its load, so without a read
        the first hour of 'memory on' would report no history at all."""
        from src.flow.level_memory import FAILED, LevelMemory
        mon = _monitor(tmp_path, monkeypatch)
        seed = LevelMemory(enabled=True, path=mon.memory.path)
        seed.record("sup", FAILED, TS, 97_900.0)

        assert mon.memory.counts("sup", TS) == (0, 0)
        assert mon._on_command({"cmd": "config.set", "level_memory": True})["ok"]
        assert mon.memory.counts("sup", TS) == (0, 1)

    def test_adding_a_level_price_is_standing_in_warns(self, tmp_path, monkeypatch):
        """Allowed — sometimes that is the trade — but never silent, because it
        is the one move that breaks the forward-test guarantee."""
        from src.utils.types import Tick
        mon = _monitor(tmp_path, monkeypatch)
        mon.engine.on_tick(Tick("BTCUSDT", 98_010.0, 1.0, TS, False))
        result = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})
        assert result["ok"] is True
        assert "forward-clean" in result["warning"]

    def test_no_warning_when_price_is_elsewhere(self, tmp_path, monkeypatch):
        from src.utils.types import Tick
        mon = _monitor(tmp_path, monkeypatch)
        mon.engine.on_tick(Tick("BTCUSDT", 99_500.0, 1.0, TS, False))
        result = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})
        assert "warning" not in result

    def test_snapshot_lists_parked_levels_too(self, tmp_path, monkeypatch):
        """The engine only holds ACTIVE monitors, so a parked level would
        vanish from the page and could never be re-armed from there."""
        mon = _monitor(tmp_path, monkeypatch)
        lid = mon._on_command({"cmd": "level.upsert", "level": {
            "price": 98_000.0, "width": 150.0, "side": "long"}})["id"]
        mon._on_command({"cmd": "level.enable", "id": lid, "enabled": False})
        snap = mon._snapshot()
        assert [lv["id"] for lv in snap["registry"]] == [lid]
        assert snap["levels"] == []


# ── the socket is now a write path ─────────────────────────────────────

class TestOrigin:
    """WebSockets are exempt from the same-origin policy, so binding to
    loopback stops the network but not a page the operator has open."""

    def _server(self) -> LiveServer:
        return LiveServer(get_snapshot=dict, port=8760)

    @pytest.mark.parametrize("origin", [
        "http://127.0.0.1:8760", "http://localhost:8760", None, "",
    ])
    def test_local_and_absent_origins_are_allowed(self, origin):
        assert self._server()._allowed_origin(origin) is True

    @pytest.mark.parametrize("origin", [
        "http://evil.example.com",
        "https://evil.example.com",
        "http://127.0.0.1:9999",
        "http://127.0.0.1.evil.com:8760",
    ])
    def test_foreign_origins_are_refused(self, origin):
        assert self._server()._allowed_origin(origin) is False

    def test_a_read_only_server_refuses_commands(self):
        assert self._server().on_command is None
