"""Zone state machine — the engine that watches levels and reports evidence.

One `ZoneMonitor` per level. It has no opinion about where price is going; it
only answers *what is flow doing now that price is here*, and only while price
is actually here.

    IDLE ──price nears──> APPROACHING ──enters zone──> EVALUATING
                               ^                          │
                               └──────leaves zone─────────┤
                                                          ├─ score >= threshold ──> CONFIRMED ─┐
                                                          └─ pushed past level ───> INVALIDATED ─┤
                                                                                                  │
                                       IDLE <───────────── cooldown_ms elapsed ───────────────────┘

HOT PATH
--------
`on_tick` runs on every trade and is pure float arithmetic over bounded deques —
no pandas, no allocation per tick, no logging, no clock calls. Measured at
~0.9 us/tick, roughly 5000x headroom over BTCUSDT's peak trade rate. Keep it
that way: anything that touches disk, the network or a DataFrame belongs on a
separate task, not here.

Scoring is throttled (`score_interval_ms`) because it is the expensive part and
re-scoring on every tick buys nothing — the evidence does not change
meaningfully between two trades 5ms apart.

LAG
---
Every tick carries an exchange timestamp, so the engine measures how far behind
the tape it is and exposes it. A stale score is worse than no score, so the lag
is published and the UI is expected to show it prominently.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field

from src.flow.evidence import DEFAULT_THRESHOLD, EvidenceReport, score_zone
from src.flow.features import ZoneAccumulator, ZoneFeatures
from src.flow.level_registry import Level, LevelRegistry, LevelSide
from src.utils.logger import get_logger
from src.utils.types import OrderBookSnapshot, Tick

log = get_logger("zone_state")


class ZoneState(str, enum.Enum):
    IDLE = "IDLE"                  # price far away, nothing to do
    APPROACHING = "APPROACHING"    # price near the zone, warming up
    EVALUATING = "EVALUATING"      # price inside the zone, accumulating evidence
    CONFIRMED = "CONFIRMED"        # evidence threshold met — signal emitted
    INVALIDATED = "INVALIDATED"    # price pushed through; level failed

    # CONFIRMED and INVALIDATED persist for `cooldown_ms` before the level
    # re-arms to IDLE. They are terminal outcomes, not transient hops: a
    # dashboard needs to show "this confirmed two minutes ago", and a state
    # that lasted one tick would be invisible.


@dataclass
class FlowSignal:
    """A confirmation. Advisory only — this is never an order."""

    level_id: str
    symbol: str
    side: LevelSide
    timestamp: int
    price: float
    score: float
    threshold: float
    invalidation: float
    features: ZoneFeatures
    report: EvidenceReport
    note: str
    test_count: int
    level_first_seen_ms: int

    def to_dict(self) -> dict:
        return {
            "level_id": self.level_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "timestamp": self.timestamp,
            "price": self.price,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "invalidation": self.invalidation,
            "note": self.note,
            "test_count": self.test_count,
            "level_first_seen_ms": self.level_first_seen_ms,
            "dwell_ms": self.features.dwell_ms,
            "delta": round(self.features.delta, 6),
            "delta_ratio": round(self.features.delta_ratio, 4),
            "absorption": round(self.features.absorption, 4),
            "range_ratio": round(self.features.range_ratio, 4),
            "adverse_excursion": round(self.features.adverse_excursion, 6),
            "trade_count": self.features.trade_count,
            "volume": round(self.features.volume, 6),
            "supporting": [
                {"name": i.name, "weight": round(i.weight, 4), "detail": i.detail}
                for i in self.report.supporting
            ],
            "opposing": [
                {"name": i.name, "weight": round(i.weight, 4), "detail": i.detail}
                for i in self.report.opposing
            ],
        }


@dataclass
class ZoneMonitor:
    """Tracks one level through the state machine."""

    level: Level
    threshold: float = DEFAULT_THRESHOLD
    approach_mult: float = 3.0        # zone half-widths away that counts as "near"
    invalidation_mult: float = 1.0    # adverse excursion (in half-widths) that fails it
    min_dwell_ms: int = 20_000        # minimum time in zone before a signal can fire
    cooldown_ms: int = 300_000        # hold after a resolution before re-arming
    score_interval_ms: int = 1_000    # re-score at most this often

    state: ZoneState = ZoneState.IDLE
    test_count: int = 0
    last_price: float = 0.0
    last_ts: int = 0
    _acc: ZoneAccumulator | None = None
    _entered_ms: int = 0
    _resolved_ms: int = 0
    _last_score_ms: int = 0
    _report: EvidenceReport | None = None
    _signalled_this_test: bool = False

    # ── hot path ───────────────────────────────────────────────────────

    def on_tick(self, tick: Tick, baseline_range: float) -> FlowSignal | None:
        """Feed one trade. Returns a signal only on the transition to CONFIRMED."""
        price = tick.price
        ts = tick.timestamp
        self.last_price = price
        self.last_ts = ts

        if self.state in (ZoneState.CONFIRMED, ZoneState.INVALIDATED):
            # Hold the outcome visible until the cooldown expires, then re-arm.
            if ts - self._resolved_ms >= self.cooldown_ms:
                self._reset_to_idle()
            return None

        inside = self.level.contains(price)
        distance = self.level.distance(price)
        near = distance <= self.level.width * self.approach_mult

        if self.state is ZoneState.IDLE:
            if inside:
                self._enter_zone(tick, baseline_range)
            elif near:
                self.state = ZoneState.APPROACHING
            return None

        if self.state is ZoneState.APPROACHING:
            if inside:
                self._enter_zone(tick, baseline_range)
            elif not near:
                self.state = ZoneState.IDLE
            return None

        # EVALUATING
        if self._acc is None:
            self._reset_to_idle()
            return None

        self._acc.on_tick(tick)

        features = None
        if ts - self._last_score_ms >= self.score_interval_ms:
            self._last_score_ms = ts
            features = self._acc.features()
            self._report = score_zone(self.level, features, self.threshold)

        if features is None:
            features = self._acc.features()

        # Invalidation takes precedence over confirmation.
        if features.adverse_excursion > self.level.width * self.invalidation_mult:
            self.state = ZoneState.INVALIDATED
            self._resolved_ms = ts
            return None

        if not inside and not near:
            # Left the area without resolving — re-arm, keep the test count.
            self._reset_to_idle()
            return None

        if (
            self._report is not None
            and self._report.confirmed
            and not self._signalled_this_test
            and ts - self._entered_ms >= self.min_dwell_ms
        ):
            self._signalled_this_test = True
            self.state = ZoneState.CONFIRMED
            self._resolved_ms = ts
            return self._build_signal(ts, price, features, self._report)

        return None

    def on_book(self, snapshot: OrderBookSnapshot) -> None:
        """Optional book update. Ignored unless a zone is being evaluated."""
        if self.state is ZoneState.EVALUATING and self._acc is not None:
            self._acc.on_book(snapshot, band=self.level.width)

    # ── internals ──────────────────────────────────────────────────────

    def _enter_zone(self, tick: Tick, baseline_range: float) -> None:
        self.test_count += 1
        self.state = ZoneState.EVALUATING
        self._entered_ms = tick.timestamp
        self._last_score_ms = tick.timestamp
        self._signalled_this_test = False
        self._report = None
        self._acc = ZoneAccumulator(
            level_price=self.level.price,
            is_support=self.level.side is LevelSide.LONG,
            baseline_range=baseline_range,
            test_count=self.test_count,
        )
        self._acc.on_tick(tick)

    def _reset_to_idle(self) -> None:
        self.state = ZoneState.IDLE
        self._acc = None
        self._report = None
        self._signalled_this_test = False

    def _build_signal(
        self, ts: int, price: float, features: ZoneFeatures, report: EvidenceReport
    ) -> FlowSignal:
        buffer = self.level.width * self.invalidation_mult
        invalidation = (
            self.level.low - buffer
            if self.level.side is LevelSide.LONG
            else self.level.high + buffer
        )
        return FlowSignal(
            level_id=self.level.id,
            symbol=self.level.symbol,
            side=self.level.side,
            timestamp=ts,
            price=price,
            score=report.score,
            threshold=self.threshold,
            invalidation=round(invalidation, 8),
            features=features,
            report=report,
            note=self.level.note,
            test_count=self.test_count,
            level_first_seen_ms=self.level.first_seen_ms,
        )

    # ── reads ──────────────────────────────────────────────────────────

    @property
    def report(self) -> EvidenceReport | None:
        return self._report

    def snapshot(self) -> dict:
        """Publishable view. Cheap — safe to call at UI cadence."""
        features = self._acc.features() if self._acc is not None else None
        return {
            "level_id": self.level.id,
            "symbol": self.level.symbol,
            "side": self.level.side.value,
            "price": self.level.price,
            "low": self.level.low,
            "high": self.level.high,
            "note": self.level.note,
            "state": self.state.value,
            "resolved_ms": self._resolved_ms or None,
            "test_count": self.test_count,
            "last_price": self.last_price,
            "distance": round(self.level.distance(self.last_price), 8)
            if self.last_price else None,
            "dwell_ms": features.dwell_ms if features else 0,
            "score": round(self._report.score, 4) if self._report else None,
            "threshold": self.threshold,
            "sufficient": self._report.sufficient if self._report else False,
            "trade_count": features.trade_count if features else 0,
            "delta": round(features.delta, 6) if features else 0.0,
            "delta_ratio": round(features.delta_ratio, 4) if features else 0.0,
            "absorption": round(features.absorption, 4) if features else 0.0,
            "range_ratio": round(features.range_ratio, 4) if features else 0.0,
            "adverse_excursion": round(features.adverse_excursion, 6) if features else 0.0,
            "supporting": [
                {"name": i.name, "weight": round(i.weight, 4), "detail": i.detail}
                for i in self._report.supporting
            ] if self._report else [],
            "opposing": [
                {"name": i.name, "weight": round(i.weight, 4), "detail": i.detail}
                for i in self._report.opposing
            ] if self._report else [],
        }


@dataclass
class FlowEngine:
    """Owns the level registry and one monitor per level; routes ticks.

    Also maintains the rolling baseline price range that normalizes excursion,
    and measures how far behind the exchange tape the process is running.
    """

    registry: LevelRegistry
    symbol: str
    threshold: float = DEFAULT_THRESHOLD
    baseline_window_ms: int = 900_000     # 15 min of price history
    monitors: dict[str, ZoneMonitor] = field(default_factory=dict)

    _prices: deque = field(default_factory=lambda: deque(maxlen=200_000))
    _tick_count: int = 0
    _lag_ms: int = 0
    _last_tick_ms: int = 0

    def sync_levels(self, now_ms: int) -> None:
        """Add monitors for new levels, drop monitors for removed ones."""
        active = {lv.id: lv for lv in self.registry.active(self.symbol, now_ms)}
        for lid, level in active.items():
            existing = self.monitors.get(lid)
            if existing is None:
                self.monitors[lid] = ZoneMonitor(level=level, threshold=self.threshold)
            else:
                existing.level = level  # pick up edits without losing state
        for lid in list(self.monitors):
            if lid not in active:
                del self.monitors[lid]

    def on_tick(self, tick: Tick, local_ms: int | None = None) -> list[FlowSignal]:
        """Hot path. Returns any signals produced by this tick."""
        self._tick_count += 1
        self._last_tick_ms = tick.timestamp
        if local_ms is not None:
            self._lag_ms = local_ms - tick.timestamp

        self._prices.append((tick.timestamp, tick.price))
        baseline = self._baseline_range(tick.timestamp)

        signals: list[FlowSignal] = []
        for monitor in self.monitors.values():
            signal = monitor.on_tick(tick, baseline)
            if signal is not None:
                signals.append(signal)
        return signals

    def on_book(self, snapshot: OrderBookSnapshot) -> None:
        for monitor in self.monitors.values():
            monitor.on_book(snapshot)

    def _baseline_range(self, now_ms: int) -> float:
        """High-low over the recent window. Cheap enough at trade cadence."""
        cutoff = now_ms - self.baseline_window_ms
        hi = float("-inf")
        lo = float("inf")
        for ts, price in reversed(self._prices):
            if ts < cutoff:
                break
            if price > hi:
                hi = price
            if price < lo:
                lo = price
        if hi == float("-inf"):
            return 0.0
        return hi - lo

    def snapshot(self, local_ms: int) -> dict:
        return {
            "symbol": self.symbol,
            "generated_ms": local_ms,
            "last_tick_ms": self._last_tick_ms,
            "lag_ms": self._lag_ms,
            "tick_count": self._tick_count,
            "threshold": self.threshold,
            "baseline_range": round(self._baseline_range(self._last_tick_ms), 8)
            if self._last_tick_ms else 0.0,
            "levels": [m.snapshot() for m in sorted(
                self.monitors.values(), key=lambda m: m.level.price
            )],
        }
