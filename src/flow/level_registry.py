"""Level registry — the human half of the system.

Levels come from a TOML file you edit before a session. This module loads and
validates them, and nothing here ever invents one. That division is deliberate:
level selection carries context (structure, bias, session) that the flow engine
has no access to, and pretending otherwise would hide where the judgement lives.

`first_seen_ms` matters more than it looks. A level is only meaningful if it was
written down BEFORE price reached it, and forward-logging that timestamp is the
only thing separating this from hindsight. The registry stamps it on first load,
preserves it across reloads, and **writes it back to the file** so a monitor
restart does not silently re-stamp every level with today's clock.

Levels can also be edited from the live page, which calls `upsert`/`remove` and
then `save`. The TOML file stays the single source of truth either way: the UI
writes through to it rather than keeping a second store, because two places to
look is how a level ends up armed in one and parked in the other.
"""

from __future__ import annotations

import enum
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.utils.logger import get_logger

log = get_logger("level_registry")

DEFAULT_LEVELS_PATH = Path("config/levels.toml")


class LevelSide(str, enum.Enum):
    """Which way you expect the level to resolve if it holds."""

    LONG = "long"     # support — a hold means buy
    SHORT = "short"   # resistance — a hold means sell


@dataclass
class Level:
    """One price zone to watch, as written by the trader."""

    id: str
    symbol: str
    price: float
    width: float          # how far PAST the level the zone runs — see low/high
    side: LevelSide
    note: str = ""
    expires_ms: int | None = None
    enabled: bool = True
    first_seen_ms: int = 0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("level id must be non-empty")
        if not self.symbol:
            raise ValueError(f"level {self.id}: symbol must be non-empty")
        if self.price <= 0:
            raise ValueError(f"level {self.id}: price must be > 0, got {self.price}")
        if self.width <= 0:
            raise ValueError(f"level {self.id}: width must be > 0, got {self.width}")
        if self.width >= self.price:
            raise ValueError(
                f"level {self.id}: width {self.width} >= price {self.price} — "
                "width is a distance in price units, not a percentage"
            )

    # The zone extends from the level INTO the side price penetrates, not
    # symmetrically around it. A resistance at 4000 with width 100 is watched
    # from 4000 to 4100 — price rallies into it and pokes above; a support at
    # 4000 is watched from 3900 to 4000. The level itself is the edge you are
    # trading off, and the width is how far through it you will tolerate.
    #
    # This also makes invalidation fall out cleanly: leaving the zone on the
    # far side IS the level failing, so `width` is both the zone and the risk.

    @property
    def low(self) -> float:
        return self.price if self.side is LevelSide.SHORT else self.price - self.width

    @property
    def high(self) -> float:
        return self.price + self.width if self.side is LevelSide.SHORT else self.price

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def distance(self, price: float) -> float:
        """Absolute distance from the zone edge; 0 when inside."""
        if self.contains(price):
            return 0.0
        return self.low - price if price < self.low else price - self.high

    def is_expired(self, now_ms: int) -> bool:
        return self.expires_ms is not None and now_ms >= self.expires_ms

    def is_active(self, now_ms: int) -> bool:
        return self.enabled and not self.is_expired(now_ms)


@dataclass
class LevelRegistry:
    """Loads levels from TOML and preserves first-seen timestamps across reloads."""

    path: Path = field(default_factory=lambda: DEFAULT_LEVELS_PATH)
    levels: dict[str, Level] = field(default_factory=dict)
    _mtime: float = 0.0

    def load(self, now_ms: int | None = None, force: bool = False) -> bool:
        """Read the file if it changed. Returns True when levels were reloaded.

        A malformed file is logged and ignored rather than raised — a typo mid
        session should not take the monitor down with live levels armed.
        """
        now_ms = now_ms if now_ms is not None else _now_ms()
        if not self.path.exists():
            if self.levels:
                log.warning("levels_file_missing", path=str(self.path))
            return False

        mtime = self.path.stat().st_mtime
        if not force and mtime == self._mtime:
            return False

        try:
            raw = tomllib.loads(self.path.read_text())
            parsed = _parse_levels(raw, now_ms)
        except Exception as e:
            log.error(
                "levels_parse_failed",
                path=str(self.path), error=str(e), type=type(e).__name__,
                note="keeping previously loaded levels",
            )
            return False

        # Provenance, in priority order: what we already had in memory, then
        # what the file recorded, then now. The middle case is what makes a
        # restart non-destructive — without it every level would be re-stamped
        # with today's clock and "written before price arrived" would be
        # unfalsifiable after the first crash.
        for lid, level in parsed.items():
            prior = self.levels.get(lid)
            if prior is not None:
                level.first_seen_ms = prior.first_seen_ms
            elif not level.first_seen_ms:
                level.first_seen_ms = now_ms

        added = set(parsed) - set(self.levels)
        removed = set(self.levels) - set(parsed)
        self.levels = parsed
        self._mtime = mtime
        log.info(
            "levels_loaded",
            path=str(self.path), count=len(parsed),
            added=sorted(added), removed=sorted(removed),
        )
        return True

    # ── mutation (the live page writes through these) ──────────────────

    def upsert(self, level: Level, now_ms: int | None = None) -> Level:
        """Add or replace a level and persist. Returns the stored level.

        An existing level keeps its original `first_seen_ms` through an edit:
        moving a level you marked an hour ago is still a level you marked an
        hour ago. Only a genuinely new id gets today's stamp — otherwise
        "nudge the price by a dollar" would launder provenance.
        """
        now_ms = now_ms if now_ms is not None else _now_ms()
        prior = self.levels.get(level.id)
        level.first_seen_ms = prior.first_seen_ms if prior else (level.first_seen_ms or now_ms)
        self.levels[level.id] = level
        self.save()
        log.info(
            "level_upserted",
            level=level.id, price=level.price, width=level.width,
            side=level.side.value, enabled=level.enabled, new=prior is None,
        )
        return level

    def remove(self, level_id: str) -> bool:
        """Delete a level. Its recorded outcomes are NOT deleted — re-adding
        the same id later inherits them, which is usually what you want and
        occasionally a surprise."""
        if self.levels.pop(level_id, None) is None:
            return False
        self.save()
        log.info("level_removed", level=level_id)
        return True

    def set_enabled(self, level_id: str, enabled: bool) -> bool:
        """Park or re-arm a level without losing it."""
        level = self.levels.get(level_id)
        if level is None:
            return False
        level.enabled = enabled
        self.save()
        log.info("level_enabled_set", level=level_id, enabled=enabled)
        return True

    def save(self) -> None:
        """Write the file atomically, then adopt its mtime.

        Adopting the mtime stops the reload timer re-reading a file we just
        wrote. Comments in a hand-written file do not survive — each level's
        `note` does, which is where the reasoning belongs anyway.
        """
        text = _render_toml(self.levels)
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    def active(self, symbol: str | None = None, now_ms: int | None = None) -> list[Level]:
        """Enabled, unexpired levels, optionally filtered to one symbol."""
        now_ms = now_ms if now_ms is not None else _now_ms()
        out = [lv for lv in self.levels.values() if lv.is_active(now_ms)]
        if symbol is not None:
            out = [lv for lv in out if lv.symbol.upper() == symbol.upper()]
        return sorted(out, key=lambda lv: lv.price)

    def get(self, level_id: str) -> Level | None:
        return self.levels.get(level_id)

    def nearest(self, symbol: str, price: float, now_ms: int | None = None) -> Level | None:
        candidates = self.active(symbol, now_ms)
        if not candidates:
            return None
        return min(candidates, key=lambda lv: lv.distance(price))


_FILE_HEADER = """\
# Flow monitor levels — written by you and by the live page.
#
# A level is only honest if it was written BEFORE price reached it, which is
# what `first_seen` records. It is preserved across edits and restarts; do not
# hand-edit it unless you mean to change what the outcome log will believe.
#
#   price    the level itself — the edge you are trading off
#   width    how far PAST the level the zone runs. A short at 4000 width 100 is
#            watched 4000-4100; a long at 4000 width 100 is watched 3900-4000.
#            It is also the risk: leaving the zone on the far side fails the level.
#   side     long = support (zone below), short = resistance (zone above)
#   note     why you marked it — recorded with every signal
#   expires  optional RFC3339; the level disarms itself after this
#   enabled  false parks a level without deleting it

"""


def _toml_str(value: str) -> str:
    """Basic TOML string. Escapes what the spec requires and nothing else."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _render_toml(levels: dict[str, Level]) -> str:
    parts = [_FILE_HEADER]
    for level in sorted(levels.values(), key=lambda lv: (lv.symbol, lv.price)):
        rows = [
            "[[level]]",
            f"id          = {_toml_str(level.id)}",
            f"symbol      = {_toml_str(level.symbol)}",
            f"price       = {level.price!r}",
            f"width       = {level.width!r}",
            f"side        = {_toml_str(level.side.value)}",
            f"note        = {_toml_str(level.note)}",
        ]
        if level.expires_ms:
            rows.append(f"expires     = {_toml_str(_iso(level.expires_ms))}")
        rows.append(f"enabled     = {'true' if level.enabled else 'false'}")
        if level.first_seen_ms:
            rows.append(f"first_seen  = {_toml_str(_iso(level.first_seen_ms))}")
        parts.append("\n".join(rows) + "\n")
    return "\n".join(parts)


def _parse_levels(raw: dict, now_ms: int) -> dict[str, Level]:
    entries = raw.get("level", [])
    if not isinstance(entries, list):
        raise ValueError("expected an array of [[level]] tables")

    levels: dict[str, Level] = {}
    for entry in entries:
        lid = str(entry.get("id", "")).strip()
        if lid in levels:
            raise ValueError(f"duplicate level id: {lid!r}")
        level = Level(
            id=lid,
            symbol=str(entry.get("symbol", "")).strip().upper(),
            price=float(entry.get("price", 0.0)),
            width=float(entry.get("width", 0.0)),
            side=LevelSide(str(entry.get("side", "long")).strip().lower()),
            note=str(entry.get("note", "")),
            expires_ms=_parse_time(entry.get("expires")),
            enabled=bool(entry.get("enabled", True)),
            # 0 means "not recorded in the file" — load() stamps it.
            first_seen_ms=_parse_time(entry.get("first_seen")) or 0,
        )
        levels[level.id] = level
    return levels


def _parse_time(value) -> int | None:
    """RFC3339 / TOML datetime -> epoch ms. None passes through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
