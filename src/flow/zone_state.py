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

THE SEQUENCE — why absorption alone does not fire a signal
-----------------------------------------------------------
A trader does not enter on absorption. They wait for a sequence:

    1. aggression arrives at the level
    2. it FAILS — price does not go
    3. the aggressors are now trapped, offside, needing to cover
    4. price turns, and the entry is taken as the trapped side covers

Step 3 is the entry, not step 2. Absorption says somebody is defending; the
*turn* says the attackers gave up. The gap between them is where most losses
live — a defender can absorb for twenty minutes and then step away, which is
exactly what makes "absorption = buy" a losing rule.

So `ZonePhase` models the sequence explicitly and a signal fires only on
ABSORBING -> TURNING. Set `require_turn=False` to fall back to scoring alone,
which exists so the two can be compared on logged outcomes rather than argued
about.


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
from src.flow.features import MarketContext, ZoneAccumulator, ZoneFeatures
from src.flow.level_memory import FAILED, HELD, LevelMemory
from src.flow.level_registry import Level, LevelRegistry, LevelSide
from src.flow.tape import FlowTape
from src.utils.logger import get_logger
from src.utils.types import OrderBookSnapshot, Tick

log = get_logger("zone_state")

# How far through your zone price must travel against you, as a fraction of the
# width, before the phase reads FAILING. Below 1.0 by definition: at 1.0 the
# level is already invalidated, so a FAILING phase that only appeared then
# would never be visible.
FAILING_EXCURSION_FRAC = 0.6

# Footprint rows the chart window should span. ~48 keeps each row several
# pixels tall on a 380px pane, which is the point: a row thinner than a pixel
# draws nothing at all.
FOOTPRINT_ROWS = 48
# How much taller than the zone the chart window is. Must match the page's
# `computeGeo` (zone +/- 0.9x its height), or the rows are sized for a window
# that is not the one being drawn.
WINDOW_ZONE_MULT = 2.8


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


class ZonePhase(str, enum.Enum):
    """Where the attack/defence sequence has got to inside a zone."""

    WATCHING = "WATCHING"      # in the zone, nothing notable yet
    ABSORBING = "ABSORBING"    # opposing aggression failing to move price
    TURNING = "TURNING"        # absorption established AND flow flipping — the entry
    FAILING = "FAILING"        # price travelling with the aggression; level going


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
    phase: ZonePhase = ZonePhase.WATCHING

    def to_dict(self) -> dict:
        return {
            "level_id": self.level_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "timestamp": self.timestamp,
            "price": self.price,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "phase": self.phase.value,
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
            "probe_count": self.features.probe_count,
            "retest_volume_ratio": round(self.features.retest_volume_ratio, 4),
            "large_print_share": round(self.features.large_print_share, 4),
            "iceberg_ratio": round(self.features.iceberg_ratio, 3),
            "iceberg_price": self.features.iceberg_price,
            "iceberg_traded": round(self.features.iceberg_traded, 6),
            "iceberg_displayed": round(self.features.iceberg_displayed, 6),
            "prior_held": self.features.prior_held,
            "prior_failed": self.features.prior_failed,
            "velocity_ratio": round(self.features.velocity_ratio, 4),
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
    require_turn: bool = True         # fire only on ABSORBING -> TURNING
    memory: LevelMemory | None = None # optional prior-outcome history
    turn_threshold: float = 0.15      # late-delta flip that counts as a turn
    absorption_floor: float = 0.25

    state: ZoneState = ZoneState.IDLE
    phase: ZonePhase = ZonePhase.WATCHING
    test_count: int = 0
    last_price: float = 0.0
    last_ts: int = 0
    _acc: ZoneAccumulator | None = None
    _entered_ms: int = 0
    _resolved_ms: int = 0
    _last_score_ms: int = 0
    _report: EvidenceReport | None = None
    _signalled_this_test: bool = False
    _test_resolved: bool = False

    # ── hot path ───────────────────────────────────────────────────────

    def on_tick(self, tick: Tick, context: MarketContext) -> FlowSignal | None:
        """Feed one trade. Returns a signal only on the transition to CONFIRMED."""
        price = tick.price
        ts = tick.timestamp
        self.last_price = price
        self.last_ts = ts

        if self.state in (ZoneState.CONFIRMED, ZoneState.INVALIDATED):
            # A confirmation that then breaks is the outcome memory most wants
            # to hear about: the defender was there, and then was not. Catch it
            # during the cooldown, because by the time the cooldown expires
            # price may have wandered back and the exit test would miss it.
            if (
                self.memory is not None
                and self.state is ZoneState.CONFIRMED
                and not self._test_resolved
                and self._breached(price)
            ):
                self._record_outcome(FAILED, ts, price)

            # Hold the outcome visible until the cooldown expires, then re-arm.
            if ts - self._resolved_ms >= self.cooldown_ms:
                if self.state is ZoneState.CONFIRMED:
                    self._classify_exit(price, ts)
                self._reset_to_idle()
            return None

        inside = self.level.contains(price)
        distance = self.level.distance(price)
        near = distance <= self.level.width * self.approach_mult

        if self.state is ZoneState.IDLE:
            if inside:
                self._enter_zone(tick, context)
            elif near:
                self.state = ZoneState.APPROACHING
            return None

        if self.state is ZoneState.APPROACHING:
            if inside:
                self._enter_zone(tick, context)
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
        else:
            self._update_phase(features)

        # Invalidation takes precedence over confirmation.
        if features.adverse_excursion > self.level.width * self.invalidation_mult:
            self.state = ZoneState.INVALIDATED
            self._resolved_ms = ts
            self._record_outcome(FAILED, ts, price)
            return None

        if not inside and not near:
            # Left the area. Which side it left on is the outcome: away from the
            # level is a hold, and anything else is inconclusive — which is
            # recorded as nothing, because an inconclusive test is not evidence.
            self._classify_exit(price, ts)
            self._reset_to_idle()
            return None

        turn_ok = (not self.require_turn) or self.phase is ZonePhase.TURNING
        if (
            self._report is not None
            and self._report.confirmed
            and turn_ok
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

    def _enter_zone(self, tick: Tick, context: MarketContext) -> None:
        self.test_count += 1
        self._test_resolved = False
        prior_held, prior_failed = (
            self.memory.counts(self.level.id, tick.timestamp)
            if self.memory is not None
            else (0, 0)
        )
        self.state = ZoneState.EVALUATING
        self.phase = ZonePhase.WATCHING
        self._entered_ms = tick.timestamp
        self._last_score_ms = tick.timestamp
        self._signalled_this_test = False
        self._report = None
        self._acc = ZoneAccumulator(
            level_price=self.level.price,
            is_support=self.level.side is LevelSide.LONG,
            baseline_range=context.baseline_range,
            baseline_trade_size=context.baseline_trade_size,
            zone_width=self.level.width,
            tick_size=context.tick_size,
            test_count=self.test_count,
            prior_held=prior_held,
            prior_failed=prior_failed,
        )
        self._acc.on_tick(tick)

    def _reset_to_idle(self) -> None:
        self.state = ZoneState.IDLE
        self.phase = ZonePhase.WATCHING
        self._test_resolved = False
        self._acc = None
        self._report = None
        self._signalled_this_test = False

    def invalidation_price(self) -> float:
        """Where the level is considered gone — the ONE definition.

        It is measured from the level itself, matching the adverse-excursion
        test in `on_tick` exactly: `adverse > width * invalidation_mult` means
        price has travelled that far past the level, which is this price.

        It used to be computed as `level.low - width * mult`, which for a
        support is `level.price - 2 * width` — twice as far as the state
        machine's own invalidation. So a level was declared dead at one price
        while every signal published a stop at another, and the outcome log
        scored wins and losses against the wider one. Two definitions of the
        same number is one too many.
        """
        buffer = self.level.width * self.invalidation_mult
        return (
            self.level.price - buffer
            if self.level.side is LevelSide.LONG
            else self.level.price + buffer
        )

    def _invalidation_price(self) -> float:   # back-compat alias
        return self.invalidation_price()

    def _breached(self, price: float) -> bool:
        invalidation = self._invalidation_price()
        return (
            price < invalidation
            if self.level.side is LevelSide.LONG
            else price > invalidation
        )

    def _classify_exit(self, price: float, ts: int) -> None:
        """A hold is leaving on the favourable side; everything else is silence."""
        if self.level.side is LevelSide.LONG:
            held = price > self.level.high
        else:
            held = price < self.level.low
        if held:
            self._record_outcome(HELD, ts, price)

    def _record_outcome(self, outcome: str, ts: int, price: float) -> None:
        """At most one outcome per test, and only when memory is enabled."""
        if self._test_resolved or self.memory is None:
            return
        self._test_resolved = True
        self.memory.record(self.level.id, outcome, ts, price)

    def _update_phase(self, features: ZoneFeatures) -> None:
        """Advance the attack/defence sequence. See the module docstring."""
        is_support = self.level.side is LevelSide.LONG
        opposing = features.delta_ratio < 0 if is_support else features.delta_ratio > 0
        turning = (
            features.late_delta_ratio > self.turn_threshold
            if is_support
            else features.late_delta_ratio < -self.turn_threshold
        )
        # "Failing" is measured against YOUR zone, not against the market's
        # recent range. The old gate was `range_ratio > 1.2` — in-zone travel
        # exceeding 1.2x the 15-minute baseline range — which the directional
        # zone made nearly unreachable: the zone is only `width` tall, and
        # price leaving it invalidates, so the in-zone range can never exceed
        # the width. A 200-wide level against a 350-wide baseline could not
        # reach 1.2 however hard it broke.
        #
        # Travel most of the way through your own zone, against you, with
        # one-sided aggression, IS the level going — and it says so in the
        # units you set.
        breaking = (
            (features.delta_ratio < -0.30 if is_support else features.delta_ratio > 0.30)
            and features.adverse_excursion >= FAILING_EXCURSION_FRAC * self.level.width
        )

        if breaking:
            self.phase = ZonePhase.FAILING
            return

        if self.phase is ZonePhase.ABSORBING and turning:
            self.phase = ZonePhase.TURNING
            return

        if self.phase is ZonePhase.TURNING:
            # A turn that reverts to one-sided opposing pressure was not a turn.
            if opposing and not turning:
                self.phase = ZonePhase.ABSORBING
            return

        if (
            features.sufficient
            and opposing
            and features.absorption >= self.absorption_floor
        ):
            self.phase = ZonePhase.ABSORBING

    def _build_signal(
        self, ts: int, price: float, features: ZoneFeatures, report: EvidenceReport
    ) -> FlowSignal:
        return FlowSignal(
            level_id=self.level.id,
            symbol=self.level.symbol,
            side=self.level.side,
            timestamp=ts,
            price=price,
            score=report.score,
            threshold=self.threshold,
            invalidation=round(self._invalidation_price(), 8),
            features=features,
            report=report,
            note=self.level.note,
            test_count=self.test_count,
            level_first_seen_ms=self.level.first_seen_ms,
            phase=self.phase,
        )

    # ── reads ──────────────────────────────────────────────────────────

    @property
    def report(self) -> EvidenceReport | None:
        return self._report

    def snapshot(self) -> dict:
        """Publishable view. Cheap — safe to call at UI cadence."""
        from src.flow.narrative import describe   # deferred: narrative imports us
        features = self._acc.features() if self._acc is not None else None
        # Read memory directly rather than from features: features only exist
        # while a zone is being evaluated, and the prior record is worth showing
        # on an idle level too. Both are (0, 0) when memory is off.
        prior_held, prior_failed = (
            self.memory.counts(self.level.id, self.last_ts or None)
            if self.memory is not None
            else (0, 0)
        )
        return {
            "level_id": self.level.id,
            "symbol": self.level.symbol,
            "side": self.level.side.value,
            "price": self.level.price,
            "low": self.level.low,
            "high": self.level.high,
            "invalidation": round(self.invalidation_price(), 8),
            "note": self.level.note,
            "state": self.state.value,
            "phase": self.phase.value,
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
            "trades_per_sec": round(features.trades_per_sec, 2) if features else 0.0,
            "velocity_ratio": round(features.velocity_ratio, 2) if features else 1.0,
            "large_print_share": round(features.large_print_share, 3) if features else 0.0,
            "probe_count": features.probe_count if features else 0,
            "retest_volume_ratio": round(features.retest_volume_ratio, 3) if features else 0.0,
            "iceberg_ratio": round(features.iceberg_ratio, 2) if features else 0.0,
            "iceberg_price": features.iceberg_price if features else 0.0,
            "prior_held": prior_held,
            "prior_failed": prior_failed,
            "prior_last": self.memory.last_outcome(self.level.id)
            if self.memory is not None else None,
            "narrative": describe(
                self.level, self.state, self.phase, features,
                self.last_price, self.level.distance(self.last_price)
                if self.last_price else 0.0,
            ),
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
    memory: LevelMemory | None = None     # optional; None disables it entirely
    require_turn: bool = True
    monitors: dict[str, ZoneMonitor] = field(default_factory=dict)
    tape: FlowTape = field(default_factory=FlowTape)

    _prices: deque = field(default_factory=lambda: deque(maxlen=200_000))
    # Rolling print sizes, for the large-print baseline. Sampled from ALL
    # ticks, so a zone is judged against the market's norm, not its own.
    _sizes: deque = field(default_factory=lambda: deque(maxlen=5_000))
    _size_sum: float = 0.0
    _tick_size: float = 0.0
    _tick_count: int = 0
    _lag_ms: int = 0
    _last_tick_ms: int = 0

    @property
    def last_price(self) -> float:
        """Most recent trade price, or 0.0 before the first tick."""
        return self._prices[-1][1] if self._prices else 0.0

    def apply_settings(self, threshold: float | None = None,
                       require_turn: bool | None = None) -> None:
        """Change scoring settings on a running engine, including live monitors.

        Setting them on the engine alone would only affect monitors created
        afterwards, so a level already being evaluated would keep scoring
        against the old threshold — the one case where you most want the
        change to land.
        """
        if threshold is not None:
            self.threshold = threshold
            for monitor in self.monitors.values():
                monitor.threshold = threshold
        if require_turn is not None:
            self.require_turn = require_turn
            for monitor in self.monitors.values():
                monitor.require_turn = require_turn

    def sync_levels(self, now_ms: int) -> None:
        """Add monitors for new levels, drop monitors for removed ones."""
        active = {lv.id: lv for lv in self.registry.active(self.symbol, now_ms)}
        for lid, level in active.items():
            existing = self.monitors.get(lid)
            if existing is None:
                self.monitors[lid] = ZoneMonitor(
                    level=level, threshold=self.threshold,
                    memory=self.memory, require_turn=self.require_turn,
                )
            else:
                existing.level = level  # pick up edits without losing state
        for lid in list(self.monitors):
            if lid not in active:
                del self.monitors[lid]
        self._set_bucket_size()

    def on_tick(self, tick: Tick, local_ms: int | None = None) -> list[FlowSignal]:
        """Hot path. Returns any signals produced by this tick."""
        self._tick_count += 1
        self._last_tick_ms = tick.timestamp
        if local_ms is not None:
            self._lag_ms = local_ms - tick.timestamp

        self.tape.on_tick(tick)
        self._prices.append((tick.timestamp, tick.price))
        if self.tape.bucket_size <= 0 and self._tick_size > 0:
            self._set_bucket_size()
        if len(self._sizes) == self._sizes.maxlen:
            self._size_sum -= self._sizes[0]
        self._sizes.append(tick.quantity)
        self._size_sum += tick.quantity

        context = MarketContext(
            baseline_range=self._baseline_range(tick.timestamp),
            baseline_trade_size=self._baseline_trade_size(),
            tick_size=self._tick_size,
        )

        signals: list[FlowSignal] = []
        for monitor in self.monitors.values():
            signal = monitor.on_tick(tick, context)
            if signal is not None:
                signals.append(signal)
        return signals

    def on_book(self, snapshot: OrderBookSnapshot) -> None:
        self._infer_tick_size(snapshot)
        self.tape.on_book(snapshot)
        for monitor in self.monitors.values():
            monitor.on_book(snapshot)

    def _infer_tick_size(self, snapshot: OrderBookSnapshot) -> None:
        """Smallest gap between adjacent book levels — the instrument's tick.

        Taken from the book rather than from trades because book levels sit
        exactly on tick boundaries, so one snapshot gives an exact answer where
        trade prices would need statistics. Latched once: the tick does not
        change intraday, and re-deriving it per snapshot would let one odd
        book shrink it.

        The gap is SNAPPED to 8 significant digits. Differencing two binary
        floats does not give a round number — BTCUSDT's 0.01 tick comes back
        from a live book as 0.00999999999476131 — and while the bucketing error
        that causes is tiny, a tick that prints like that is wrong, reads as
        broken, and would fail any later equality check against the real tick.
        Eight digits is far more precision than any instrument's tick carries
        and far less than the noise.
        """
        if self._tick_size > 0 or snapshot is None:
            return
        smallest = 0.0
        for side in (snapshot.bids, snapshot.asks):
            for a, b in zip(side, side[1:]):
                gap = abs(a.price - b.price)
                if gap > 0 and (smallest == 0.0 or gap < smallest):
                    smallest = gap
        if smallest > 0:
            self._tick_size = float(f"{smallest:.8g}")
            log.info("tick_size_inferred", symbol=self.symbol,
                     tick_size=self._tick_size, raw=smallest)
            self._set_bucket_size()

    def _set_bucket_size(self) -> None:
        """Footprint row height, as a tick multiple.

        Sized from the NARROWEST active zone, not from the market's range: the
        chart window is anchored on the zone, so that is the span the rows have
        to resolve. Sizing it off the 15-minute baseline produced rows a
        five-hundredth of the window tall — every footprint bar clamped to one
        pixel, which is a chart that draws nothing.

        A tick multiple rather than an arbitrary division, because rows that do
        not divide the tick put two price levels in some rows and one in others
        — which renders as banding that reads like real structure (the same trap
        the depth heatmap hit, see ARCHITECTURE Known Gotchas 2026-09-21).

        Only re-sized when the implied height moves by 2x or more; a grid that
        shifts under you on every level edit is unreadable, and each change
        costs the history (see `FlowTape.set_bucket_size`).
        """
        if self._tick_size <= 0:
            return
        widths = [m.level.width for m in self.monitors.values()]
        span = min(widths) * WINDOW_ZONE_MULT if widths else 0.0
        if span <= 0:
            span = self._baseline_range(self._last_tick_ms) if self._tick_count > 500 else 0.0
        if span <= 0:
            return
        mult = max(1, round(span / (FOOTPRINT_ROWS * self._tick_size)))
        proposed = mult * self._tick_size
        current = self.tape.bucket_size
        if current > 0 and 0.5 <= proposed / current <= 2.0:
            return
        if self.tape.set_bucket_size(proposed):
            log.info("footprint_bucket_set", symbol=self.symbol,
                     bucket_size=self.tape.bucket_size, tick_size=self._tick_size)

    def _baseline_trade_size(self) -> float:
        """Rolling mean print size. O(1) — the sum is maintained incrementally."""
        return self._size_sum / len(self._sizes) if self._sizes else 0.0

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
            "baseline_trade_size": round(self._baseline_trade_size(), 8),
            "tick_size": self._tick_size,
            "level_memory": self.memory is not None and self.memory.enabled,
            "require_turn": self.require_turn,
            # The forming column every frame; the closed history only when `seq`
            # moves, which is once per interval rather than five times a second.
            "tape": {
                "seq": self.tape.seq,
                "interval_ms": self.tape.interval_ms,
                "bucket_size": self.tape.bucket_size,
                "has_book": self.tape._last_book is not None,
                "cvd": round(self.tape.cvd, 6),
                "live": self.tape.live_column(),
            },
            "levels": [m.snapshot() for m in sorted(
                self.monitors.values(), key=lambda m: m.level.price
            )],
        }
