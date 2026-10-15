"""Append-only signal and outcome log.

Every confirmation is written here with its full reasoning, and later annotated
with what price actually did. This file is the entire point of running the
system: without outcomes there is no calibration, no honest hit rate, and no
training data for anything that might replace the rule scorer later.

It also carries the **null-hypothesis** comparison. For every signal, the log
records what a naive "enter on every zone entry" rule would have gotten at the
same level. If the scorer cannot beat that baseline, the scorer is costing you
complexity for nothing — and that comparison is only possible if it is recorded
from day one, so it is not optional.

Writes are line-delimited JSON, appended and flushed per record, off the hot
path. A crash loses at most the record in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import orjson

from src.utils.logger import get_logger

log = get_logger("signal_log")

DEFAULT_SIGNAL_PATH = Path("data/flow_signals.jsonl")
DEFAULT_OUTCOME_PATH = Path("data/flow_outcomes.jsonl")


@dataclass
class PendingOutcome:
    """A signal awaiting resolution, tracked forward tick by tick."""

    record_id: str
    level_id: str
    side: str
    entry_price: float
    entry_ms: int
    invalidation: float
    target: float
    kind: str                       # "signal" or "baseline"
    score: float | None = None
    high: float = float("-inf")
    low: float = float("inf")

    def update(self, price: float) -> str | None:
        """Track the excursion. Returns 'target' / 'invalidation' once resolved."""
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        if self.side == "long":
            if price <= self.invalidation:
                return "invalidation"
            if price >= self.target:
                return "target"
        else:
            if price >= self.invalidation:
                return "invalidation"
            if price <= self.target:
                return "target"
        return None

    def mfe(self) -> float:
        """Maximum favourable excursion from entry."""
        if self.side == "long":
            return max(0.0, self.high - self.entry_price)
        return max(0.0, self.entry_price - self.low)

    def mae(self) -> float:
        """Maximum adverse excursion from entry."""
        if self.side == "long":
            return max(0.0, self.entry_price - self.low)
        return max(0.0, self.high - self.entry_price)


@dataclass
class SignalLog:
    """Writes signals, then resolves them against subsequent price."""

    signal_path: Path = field(default_factory=lambda: DEFAULT_SIGNAL_PATH)
    outcome_path: Path = field(default_factory=lambda: DEFAULT_OUTCOME_PATH)
    target_mult: float = 2.0        # target distance as a multiple of the risk
    pending: dict[str, PendingOutcome] = field(default_factory=dict)
    _seq: int = 0

    def __post_init__(self) -> None:
        self.signal_path.parent.mkdir(parents=True, exist_ok=True)
        self.outcome_path.parent.mkdir(parents=True, exist_ok=True)

    def record_signal(self, signal) -> str:
        """Write a confirmation and start tracking its outcome."""
        self._seq += 1
        record_id = f"sig-{signal.timestamp}-{self._seq}"
        payload = {"record_id": record_id, "kind": "signal", **signal.to_dict()}
        self._append(self.signal_path, payload)
        self.pending[record_id] = self._make_pending(
            record_id, "signal", signal.side.value, signal.price,
            signal.timestamp, signal.invalidation, signal.score,
        )
        return record_id

    def record_baseline(self, level, price: float, ts: int, invalidation: float) -> str:
        """Record the null hypothesis: entry on zone entry, no flow filter.

        Logged for EVERY zone entry, including those the scorer never confirms.
        That is what makes the comparison honest.
        """
        self._seq += 1
        record_id = f"base-{ts}-{self._seq}"
        self._append(self.signal_path, {
            "record_id": record_id,
            "kind": "baseline",
            "level_id": level.id,
            "symbol": level.symbol,
            "side": level.side.value,
            "timestamp": ts,
            "price": price,
            "invalidation": invalidation,
            "note": level.note,
        })
        self.pending[record_id] = self._make_pending(
            record_id, "baseline", level.side.value, price, ts, invalidation, None,
        )
        return record_id

    def _make_pending(
        self, record_id: str, kind: str, side: str, price: float,
        ts: int, invalidation: float, score: float | None,
    ) -> PendingOutcome:
        risk = abs(price - invalidation)
        reach = risk * self.target_mult
        target = price + reach if side == "long" else price - reach
        return PendingOutcome(
            record_id=record_id, level_id=record_id, side=side,
            entry_price=price, entry_ms=ts, invalidation=invalidation,
            target=target, kind=kind, score=score,
        )

    def update_prices(self, price: float, ts: int) -> list[dict]:
        """Advance every pending outcome. Returns records resolved by this tick."""
        if not self.pending:
            return []
        resolved: list[dict] = []
        for record_id in list(self.pending):
            pending = self.pending[record_id]
            result = pending.update(price)
            if result is None:
                continue
            del self.pending[record_id]
            record = {
                "record_id": record_id,
                "kind": pending.kind,
                "score": pending.score,
                "side": pending.side,
                "entry_price": pending.entry_price,
                "entry_ms": pending.entry_ms,
                "resolved_ms": ts,
                "held_ms": ts - pending.entry_ms,
                "outcome": result,
                "win": result == "target",
                "mfe": round(pending.mfe(), 8),
                "mae": round(pending.mae(), 8),
            }
            self._append(self.outcome_path, record)
            resolved.append(record)
        return resolved

    @staticmethod
    def _append(path: Path, payload: dict) -> None:
        try:
            with path.open("ab") as fh:
                fh.write(orjson.dumps(payload))
                fh.write(b"\n")
        except Exception as e:
            # Never let logging take down the monitor.
            log.error("signal_log_write_failed", path=str(path), error=str(e))
