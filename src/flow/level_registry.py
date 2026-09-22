"""Level registry — the human half of the system.

Levels come from a TOML file you edit before a session. This module loads and
validates them, and nothing here ever invents one. That division is deliberate:
level selection carries context (structure, bias, session) that the flow engine
has no access to, and pretending otherwise would hide where the judgement lives.

`first_seen_ms` matters more than it looks. A level is only meaningful if it was
written down BEFORE price reached it, and forward-logging that timestamp is the
only thing separating this from hindsight. The registry stamps it on first load
and preserves it across reloads.
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
    width: float
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
                "width is a half-width in price units, not a percentage"
            )

    @property
    def low(self) -> float:
        return self.price - self.width

    @property
    def high(self) -> float:
        return self.price + self.width

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

        # Carry first_seen forward so an edit elsewhere in the file does not
        # reset the provenance of untouched levels.
        for lid, level in parsed.items():
            prior = self.levels.get(lid)
            level.first_seen_ms = prior.first_seen_ms if prior else now_ms

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
            expires_ms=_parse_expiry(entry.get("expires")),
            enabled=bool(entry.get("enabled", True)),
            first_seen_ms=now_ms,
        )
        levels[level.id] = level
    return levels


def _parse_expiry(value) -> int | None:
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
