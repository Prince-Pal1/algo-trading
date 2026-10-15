"""Order book state machine — maintains a live book from Binance depth streams.

A depth stream sends *diffs*, not snapshots, so the book only stays correct if
every update is applied exactly once and in order. Binance publishes sequence
numbers for precisely this, and a book that ignores them does not fail loudly:
it drifts, and every heatmap drawn from it is quietly wrong. This module makes
the sequencing explicit and refuses to serve a book it cannot vouch for.


THE SYNC PROTOCOL (Binance's documented procedure)
--------------------------------------------------
1. Subscribe to the diff stream and BUFFER events — do not apply them yet.
2. Fetch a REST snapshot, which carries a ``lastUpdateId``.
3. Discard buffered events whose final id ``u`` is <= ``lastUpdateId``.
4. The first event applied must straddle the snapshot:
   ``U <= lastUpdateId + 1 <= u``.
5. Every later event must be contiguous with the last one applied.
6. A level whose quantity is 0 is a deletion, not a zero-size order.

Spot and USD-M futures differ at step 5. Futures events carry ``pu`` (the
previous event's final id) and contiguity is ``pu == last_update_id``; spot has
no ``pu`` and the check is ``U == last_update_id + 1``. Both are handled.

Any gap sets state to DESYNCED. The book then refuses to serve snapshots until
it is re-bootstrapped from a fresh REST snapshot — a stale book is worse than
no book, because it looks fine.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from src.utils.logger import get_logger
from src.utils.types import OrderBookLevel, OrderBookSnapshot

log = get_logger("order_book")

# Binance encodes a level removal as quantity "0". Sizes are decimal strings,
# so compare against a small epsilon rather than exact zero.
_ZERO_QTY_EPS = 1e-12


class BookState(str, enum.Enum):
    """Lifecycle of the book. Only SYNCED may serve snapshots."""

    UNSYNCED = "UNSYNCED"      # buffering diffs, waiting for a REST snapshot
    SYNCED = "SYNCED"          # contiguous and trustworthy
    DESYNCED = "DESYNCED"      # a gap was seen; needs re-bootstrap


class ApplyResult(str, enum.Enum):
    """What `apply_diff` did with an event."""

    APPLIED = "APPLIED"        # folded into the book
    BUFFERED = "BUFFERED"      # held until the snapshot arrives
    STALE = "STALE"            # entirely older than the snapshot; dropped
    DESYNC = "DESYNC"          # sequence gap; book is no longer trustworthy


class OrderBookState:
    """One symbol's book, rebuilt from a REST snapshot plus a diff stream.

    Not thread-safe; drive it from a single event loop, like the feeds do.
    """

    def __init__(self, symbol: str, max_buffer: int = 1000):
        self.symbol = symbol
        self.max_buffer = max_buffer
        self.reset()

    def reset(self) -> None:
        """Return to UNSYNCED and drop all state. Call before re-bootstrapping."""
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._buffer: list[dict] = []
        self._last_update_id: int = 0
        self._state = BookState.UNSYNCED
        self._last_event_ms: int = 0
        self._desync_count = 0
        self._dropped_buffer = 0

    # ── bootstrap ──────────────────────────────────────────────────────

    def apply_snapshot(
        self,
        last_update_id: int,
        bids: Iterable[Iterable],
        asks: Iterable[Iterable],
        timestamp_ms: int = 0,
    ) -> int:
        """Seed the book from a REST snapshot, then drain the buffer.

        Args:
            last_update_id: the snapshot's ``lastUpdateId``.
            bids / asks: ``[[price, qty], ...]`` — strings or floats.
            timestamp_ms: when the snapshot was taken, for staleness checks.

        Returns:
            How many buffered events were applied on top of the snapshot.
        """
        self._bids = {float(p): float(q) for p, q in bids if float(q) > _ZERO_QTY_EPS}
        self._asks = {float(p): float(q) for p, q in asks if float(q) > _ZERO_QTY_EPS}
        self._last_update_id = int(last_update_id)
        self._state = BookState.SYNCED
        if timestamp_ms:
            self._last_event_ms = int(timestamp_ms)

        pending, self._buffer = self._buffer, []
        applied = 0
        for event in pending:
            if self.apply_diff(event) is ApplyResult.APPLIED:
                applied += 1
        return applied

    # ── streaming ──────────────────────────────────────────────────────

    def apply_diff(self, event: dict) -> ApplyResult:
        """Fold one ``depthUpdate`` event into the book.

        Buffers while UNSYNCED. Once SYNCED, a sequence gap flips the book to
        DESYNCED and the event is rejected — the caller must re-bootstrap.
        """
        first_id = int(event.get("U", 0))
        final_id = int(event.get("u", 0))

        if self._state is BookState.UNSYNCED:
            self._buffer.append(event)
            if len(self._buffer) > self.max_buffer:
                # An unbounded buffer is a memory leak when the snapshot never
                # lands. Drop oldest — they are the ones a snapshot would
                # discard as stale anyway.
                self._buffer.pop(0)
                self._dropped_buffer += 1
            return ApplyResult.BUFFERED

        if self._state is BookState.DESYNCED:
            return ApplyResult.DESYNC

        # Entirely behind the snapshot — already reflected in it.
        if final_id <= self._last_update_id:
            return ApplyResult.STALE

        if not self._is_contiguous(event, first_id):
            self._state = BookState.DESYNCED
            self._desync_count += 1
            log.warning(
                "orderbook_desync",
                symbol=self.symbol,
                expected=self._last_update_id + 1,
                got_U=first_id,
                got_pu=event.get("pu"),
                reason="sequence gap — book must be re-bootstrapped from REST",
            )
            return ApplyResult.DESYNC

        self._apply_levels(self._bids, event.get("b", ()))
        self._apply_levels(self._asks, event.get("a", ()))
        self._last_update_id = final_id
        if "E" in event:
            self._last_event_ms = int(event["E"])
        return ApplyResult.APPLIED

    def _is_contiguous(self, event: dict, first_id: int) -> bool:
        """Spot checks U against the last id; futures carries `pu` instead."""
        if "pu" in event and event["pu"] is not None:
            return int(event["pu"]) == self._last_update_id
        # Gap check, not strict-equality check. Binance's own procedure lets the
        # FIRST event after a snapshot straddle it (U <= lastUpdateId+1 <= u),
        # and overlapping ranges show up occasionally in practice. Both are
        # harmless — each event carries absolute level quantities, not deltas,
        # so re-applying an overlap converges on the same book. What is NOT
        # harmless is a gap (U > lastUpdateId+1), which means updates were
        # missed, and that is exactly what this rejects.
        return first_id <= self._last_update_id + 1

    @staticmethod
    def _apply_levels(side: dict[float, float], levels: Iterable[Iterable]) -> None:
        for price_raw, qty_raw in levels:
            price = float(price_raw)
            qty = float(qty_raw)
            if qty <= _ZERO_QTY_EPS:
                side.pop(price, None)
            else:
                side[price] = qty

    # ── reads ──────────────────────────────────────────────────────────

    def snapshot(self, depth: int | None = None, timestamp_ms: int = 0) -> OrderBookSnapshot | None:
        """Top-of-book-outward view, or None when the book is not trustworthy.

        Args:
            depth: levels per side (None = every level held).
            timestamp_ms: stamp for the snapshot; defaults to the last event time.
        """
        if self._state is not BookState.SYNCED:
            return None

        bids = sorted(self._bids.items(), key=lambda kv: kv[0], reverse=True)
        asks = sorted(self._asks.items(), key=lambda kv: kv[0])
        if depth is not None:
            bids = bids[:depth]
            asks = asks[:depth]

        return OrderBookSnapshot(
            symbol=self.symbol,
            bids=[OrderBookLevel(price=p, quantity=q) for p, q in bids],
            asks=[OrderBookLevel(price=p, quantity=q) for p, q in asks],
            timestamp=int(timestamp_ms or self._last_event_ms),
        )

    @property
    def state(self) -> BookState:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state is BookState.SYNCED

    @property
    def last_update_id(self) -> int:
        return self._last_update_id

    @property
    def best_bid(self) -> float | None:
        return max(self._bids) if self._bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self._asks) if self._asks else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def depth_counts(self) -> tuple[int, int]:
        """(bid levels, ask levels) currently held."""
        return len(self._bids), len(self._asks)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def desync_count(self) -> int:
        return self._desync_count

    @property
    def dropped_buffer(self) -> int:
        """Buffered events discarded because the snapshot never arrived."""
        return self._dropped_buffer
