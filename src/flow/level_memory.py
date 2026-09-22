"""Level memory — how each level resolved the last times it was tested.

A level that broke last time is not the same level it was before. This records
outcomes across tests and across restarts, so a monitor started this morning
still knows that 98,000 failed twice yesterday.

OPTIONAL BY DEFAULT, AND OFF
----------------------------
This is a new, unvalidated heuristic that changes scoring, so it ships disabled.
Turn it on with `--level-memory`. Nothing in the engine depends on it, and with
it off the features report zeros and the rules never fire — the same discipline
as `--depth`.


WHAT COUNTS AS AN OUTCOME
-------------------------
- **held**    price entered the zone and left on the favourable side without
              the adverse excursion ever breaching the invalidation distance
- **failed**  adverse excursion breached it — the level gave way
- neither     anything else (still open, or price wandered off sideways) is
              recorded as nothing at all. An inconclusive test is not evidence.


THE ASYMMETRY — failing is far more informative than holding
------------------------------------------------------------
A level that broke has *demonstrated* the defender is not there. A level that
held could have held for any reason, including nobody bothering to test it
seriously. So `failed` carries roughly double the weight of `held`, and the
evidence rules are written accordingly.

This also sits in deliberate tension with the `repeated_test` rule, which
counts *against* a level on its third-plus test in a session. They measure
different things: `repeated_test` is rapid retesting consuming liquidity now;
memory is how tests **resolved**, possibly days ago. A level tested four times
in an hour and a level that held once last week are not the same situation, and
the two rules are meant to disagree about them.


DECAY
-----
Outcomes older than `max_age_days` are dropped. Market structure moves; a level
that held a month ago says little about today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import orjson

from src.utils.logger import get_logger

log = get_logger("level_memory")

DEFAULT_MEMORY_PATH = Path("data/flow_level_memory.json")
DEFAULT_MAX_AGE_DAYS = 14
MAX_OUTCOMES_PER_LEVEL = 50

HELD = "held"
FAILED = "failed"


@dataclass
class LevelRecord:
    """One level's resolved test history, newest last."""

    outcomes: list[dict] = field(default_factory=list)

    def add(self, outcome: str, ts_ms: int, price: float) -> None:
        self.outcomes.append({"outcome": outcome, "ts": ts_ms, "price": price})
        if len(self.outcomes) > MAX_OUTCOMES_PER_LEVEL:
            del self.outcomes[: len(self.outcomes) - MAX_OUTCOMES_PER_LEVEL]

    def prune(self, now_ms: int, max_age_days: int) -> None:
        cutoff = now_ms - max_age_days * 86_400_000
        self.outcomes = [o for o in self.outcomes if o["ts"] >= cutoff]

    def counts(self, now_ms: int | None = None, max_age_days: int = 0) -> tuple[int, int]:
        """(held, failed). Filters by age WITHOUT mutating — see LevelMemory.counts."""
        outcomes = self.outcomes
        if now_ms is not None and max_age_days > 0:
            cutoff = now_ms - max_age_days * 86_400_000
            outcomes = [o for o in outcomes if o["ts"] >= cutoff]
        held = sum(1 for o in outcomes if o["outcome"] == HELD)
        failed = sum(1 for o in outcomes if o["outcome"] == FAILED)
        return held, failed

    def last_outcome(self) -> str | None:
        return self.outcomes[-1]["outcome"] if self.outcomes else None


@dataclass
class LevelMemory:
    """Persisted per-level outcome history. A no-op when `enabled` is False."""

    enabled: bool = False
    path: Path = field(default_factory=lambda: DEFAULT_MEMORY_PATH)
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    records: dict[str, LevelRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.enabled:
            self.load()

    # ── persistence ────────────────────────────────────────────────────

    def load(self) -> bool:
        """Read prior outcomes. A corrupt file is logged and ignored."""
        if not self.enabled or not self.path.exists():
            return False
        try:
            raw = orjson.loads(self.path.read_bytes())
            self.records = {
                lid: LevelRecord(outcomes=list(entry.get("outcomes", [])))
                for lid, entry in raw.items()
            }
        except Exception as e:
            log.error(
                "level_memory_load_failed",
                path=str(self.path), error=str(e),
                note="starting with empty memory",
            )
            self.records = {}
            return False
        log.info("level_memory_loaded", path=str(self.path), levels=len(self.records))
        return True

    def save(self) -> None:
        """Persist. Never raises — memory is an enhancement, not a dependency."""
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {lid: {"outcomes": rec.outcomes} for lid, rec in self.records.items()}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(orjson.dumps(payload))
            tmp.replace(self.path)          # atomic — a reader never sees a partial file
        except Exception as e:
            log.error("level_memory_save_failed", path=str(self.path), error=str(e))

    # ── recording ──────────────────────────────────────────────────────

    def record(self, level_id: str, outcome: str, ts_ms: int, price: float) -> None:
        """Note how a test resolved. Ignored entirely when disabled."""
        if not self.enabled or outcome not in (HELD, FAILED):
            return
        record = self.records.setdefault(level_id, LevelRecord())
        record.add(outcome, ts_ms, price)
        record.prune(ts_ms, self.max_age_days)   # mutation belongs here, not in counts
        held, failed = record.counts()
        log.info(
            "level_outcome_recorded",
            level=level_id, outcome=outcome, held=held, failed=failed,
        )
        self.save()

    # ── reads ──────────────────────────────────────────────────────────

    def counts(self, level_id: str, now_ms: int | None = None) -> tuple[int, int]:
        """(held, failed) within the age window. (0, 0) when disabled.

        PURE — this does not prune. An earlier version did, which made every
        read destructive: a dashboard poll or a diagnostic call would silently
        empty the history it was reporting on. Pruning belongs in `record` and
        `load`, which are the only places that should mutate.

        `now_ms` should be an EVENT timestamp (a tick's), not wall clock. The
        two diverge under replay, backfill and clock skew, and filtering event
        data against wall clock would quietly drop everything.
        """
        if not self.enabled:
            return 0, 0
        record = self.records.get(level_id)
        if record is None:
            return 0, 0
        return record.counts(now_ms, self.max_age_days)

    def last_outcome(self, level_id: str) -> str | None:
        if not self.enabled:
            return None
        record = self.records.get(level_id)
        return record.last_outcome() if record else None

    def clear(self, level_id: str | None = None) -> None:
        """Forget one level, or all of them."""
        if level_id is None:
            self.records = {}
        else:
            self.records.pop(level_id, None)
        self.save()
