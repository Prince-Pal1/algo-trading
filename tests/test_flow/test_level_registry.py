"""Tests for src/flow/level_registry.py — the human half of the system.

`first_seen_ms` preservation is the load-bearing behaviour here. It is the only
thing that distinguishes a level written before price arrived from one written
after, and a reload that reset it would silently destroy that provenance.
"""

from __future__ import annotations

import pytest

from src.flow.level_registry import Level, LevelRegistry, LevelSide

NOW = 1_757_000_000_000


def _write(tmp_path, body: str):
    p = tmp_path / "levels.toml"
    p.write_text(body)
    return p


def _one(**over) -> str:
    fields = {
        "id": "l1", "symbol": "BTCUSDT", "price": 98000.0,
        "width": 100.0, "side": "long", "note": "test",
    }
    fields.update(over)
    lines = []
    for k, v in fields.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")   # TOML booleans are lowercase
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    return "[[level]]\n" + "\n".join(lines) + "\n"


class TestLevelValidation:
    def test_valid_level(self):
        lv = Level("l1", "BTCUSDT", 98000.0, 100.0, LevelSide.LONG)
        assert lv.low == 97900.0
        assert lv.high == 98100.0

    def test_empty_id_rejected(self):
        with pytest.raises(ValueError, match="id must be non-empty"):
            Level("", "BTCUSDT", 98000.0, 100.0, LevelSide.LONG)

    def test_empty_symbol_rejected(self):
        with pytest.raises(ValueError, match="symbol must be non-empty"):
            Level("l1", "", 98000.0, 100.0, LevelSide.LONG)

    def test_nonpositive_price_rejected(self):
        with pytest.raises(ValueError, match="price must be > 0"):
            Level("l1", "BTCUSDT", 0.0, 100.0, LevelSide.LONG)

    def test_nonpositive_width_rejected(self):
        with pytest.raises(ValueError, match="width must be > 0"):
            Level("l1", "BTCUSDT", 98000.0, 0.0, LevelSide.LONG)

    def test_width_larger_than_price_rejected(self):
        """Catches someone passing a percentage where price units are expected."""
        with pytest.raises(ValueError, match="width is a half-width"):
            Level("l1", "BTCUSDT", 100.0, 100.0, LevelSide.LONG)


class TestZoneGeometry:
    def _lv(self):
        return Level("l1", "BTCUSDT", 98000.0, 100.0, LevelSide.LONG)

    def test_contains_inside(self):
        assert self._lv().contains(98050.0) is True

    def test_contains_on_edge(self):
        lv = self._lv()
        assert lv.contains(97900.0) is True
        assert lv.contains(98100.0) is True

    def test_contains_outside(self):
        assert self._lv().contains(97800.0) is False

    def test_distance_zero_inside(self):
        assert self._lv().distance(98000.0) == 0.0

    def test_distance_below(self):
        assert self._lv().distance(97800.0) == pytest.approx(100.0)

    def test_distance_above(self):
        assert self._lv().distance(98300.0) == pytest.approx(200.0)


class TestExpiry:
    def test_not_expired_without_expiry(self):
        assert Level("l1", "B", 1.0, 0.1, LevelSide.LONG).is_expired(NOW) is False

    def test_expired_after_timestamp(self):
        lv = Level("l1", "B", 1.0, 0.1, LevelSide.LONG, expires_ms=NOW - 1)
        assert lv.is_expired(NOW) is True
        assert lv.is_active(NOW) is False

    def test_disabled_is_inactive(self):
        lv = Level("l1", "B", 1.0, 0.1, LevelSide.LONG, enabled=False)
        assert lv.is_active(NOW) is False


class TestRegistryLoading:
    def test_loads_levels(self, tmp_path):
        reg = LevelRegistry(path=_write(tmp_path, _one()))
        assert reg.load(now_ms=NOW) is True
        assert reg.get("l1").price == 98000.0

    def test_missing_file_returns_false(self, tmp_path):
        reg = LevelRegistry(path=tmp_path / "nope.toml")
        assert reg.load(now_ms=NOW) is False

    def test_unchanged_file_not_reloaded(self, tmp_path):
        reg = LevelRegistry(path=_write(tmp_path, _one()))
        assert reg.load(now_ms=NOW) is True
        assert reg.load(now_ms=NOW) is False

    def test_force_reloads_unchanged_file(self, tmp_path):
        reg = LevelRegistry(path=_write(tmp_path, _one()))
        reg.load(now_ms=NOW)
        assert reg.load(now_ms=NOW, force=True) is True

    def test_malformed_file_keeps_previous_levels(self, tmp_path):
        """A typo mid-session must not disarm live levels."""
        path = _write(tmp_path, _one())
        reg = LevelRegistry(path=path)
        reg.load(now_ms=NOW)
        path.write_text("this is not valid toml [[[")
        assert reg.load(now_ms=NOW, force=True) is False
        assert reg.get("l1") is not None

    def test_invalid_level_keeps_previous(self, tmp_path):
        path = _write(tmp_path, _one())
        reg = LevelRegistry(path=path)
        reg.load(now_ms=NOW)
        path.write_text(_one(width=0.0))
        assert reg.load(now_ms=NOW, force=True) is False
        assert reg.get("l1").width == 100.0

    def test_duplicate_id_rejected(self, tmp_path):
        reg = LevelRegistry(path=_write(tmp_path, _one() + _one()))
        assert reg.load(now_ms=NOW) is False

    def test_symbol_uppercased(self, tmp_path):
        reg = LevelRegistry(path=_write(tmp_path, _one(symbol="btcusdt")))
        reg.load(now_ms=NOW)
        assert reg.get("l1").symbol == "BTCUSDT"

    def test_expiry_parsed(self, tmp_path):
        body = _one() + 'expires = "2026-09-30T00:00:00Z"\n'
        reg = LevelRegistry(path=_write(tmp_path, body))
        reg.load(now_ms=NOW)
        assert reg.get("l1").expires_ms is not None


class TestFirstSeenProvenance:
    def test_first_seen_stamped_on_load(self, tmp_path):
        reg = LevelRegistry(path=_write(tmp_path, _one()))
        reg.load(now_ms=NOW)
        assert reg.get("l1").first_seen_ms == NOW

    def test_first_seen_survives_reload(self, tmp_path):
        """An edit elsewhere must not reset an untouched level's provenance."""
        path = _write(tmp_path, _one())
        reg = LevelRegistry(path=path)
        reg.load(now_ms=NOW)
        path.write_text(_one() + _one(id="l2", price=99000.0))
        reg.load(now_ms=NOW + 60_000, force=True)
        assert reg.get("l1").first_seen_ms == NOW
        assert reg.get("l2").first_seen_ms == NOW + 60_000

    def test_first_seen_survives_price_edit(self, tmp_path):
        path = _write(tmp_path, _one())
        reg = LevelRegistry(path=path)
        reg.load(now_ms=NOW)
        path.write_text(_one(price=97500.0))
        reg.load(now_ms=NOW + 60_000, force=True)
        assert reg.get("l1").first_seen_ms == NOW


class TestRegistryQueries:
    def _reg(self, tmp_path):
        body = (
            _one(id="a", price=98000.0)
            + _one(id="b", price=99000.0)
            + _one(id="c", symbol="ETHUSDT", price=3000.0, width=10.0)
            + _one(id="d", price=97000.0, enabled=False)
        )
        reg = LevelRegistry(path=_write(tmp_path, body))
        reg.load(now_ms=NOW)
        return reg

    def test_active_filters_disabled(self, tmp_path):
        ids = {lv.id for lv in self._reg(tmp_path).active(now_ms=NOW)}
        assert "d" not in ids

    def test_active_filters_by_symbol(self, tmp_path):
        ids = {lv.id for lv in self._reg(tmp_path).active("BTCUSDT", NOW)}
        assert ids == {"a", "b"}

    def test_active_sorted_by_price(self, tmp_path):
        prices = [lv.price for lv in self._reg(tmp_path).active("BTCUSDT", NOW)]
        assert prices == sorted(prices)

    def test_nearest(self, tmp_path):
        assert self._reg(tmp_path).nearest("BTCUSDT", 98100.0, NOW).id == "a"

    def test_nearest_none_when_no_levels(self, tmp_path):
        assert self._reg(tmp_path).nearest("DOGEUSDT", 1.0, NOW) is None
