"""Rolling market picture for the live page — candles, CVD, footprint, depth.

The zone monitor answers "what is flow doing at my level". This answers "what
does the market look like", which is the part a trader reads spatially and
cannot get from a number. Both feed the same page.

WHAT IT HOLDS
-------------
One bounded ring of columns, each covering `interval_ms`:

    candle      o / h / l / c
    buy, sell   aggressor volume in that column
    delta       buy - sell, and the running CVD across columns
    footprint   volume traded at each price bucket, split by aggressor
    book        resting size at each price bucket, sampled at column close

Everything is keyed by an ABSOLUTE price-bucket index (`round(price / bucket)`),
not by an offset into a fixed grid. Price drifts; a fixed grid would either
clip or force a rebuild. Sparse dicts also cost nothing where nothing traded,
which on a quiet book is most of the ladder.

HOT PATH
--------
`on_tick` is two dict lookups and some float arithmetic — no allocation once a
column exists, no clock call, no logging. `on_book` only does work at a column
boundary: sampling every depth update would be ~10x the work for a picture that
redraws 5 times a second.

WHY NOT REUSE CandleBuilder
---------------------------
`src/data/candle_builder.py` emits completed candles for the strategy path and
is timeframe-aligned to the wall clock. This needs the FORMING column visible at
all times (the right edge of the chart is the live candle), sub-minute intervals,
and per-price volume alongside. Different job, different shape.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from src.utils.types import OrderBookSnapshot, Tick

DEFAULT_INTERVAL_MS = 5_000
DEFAULT_COLUMNS = 180          # 15 minutes at 5s
MAX_BUCKETS_PER_COLUMN = 512   # a runaway book cannot grow a column without bound


@dataclass
class Column:
    """One time slice: a candle plus where its volume went."""

    start_ms: int
    open: float
    high: float
    low: float
    close: float
    buy: float = 0.0
    sell: float = 0.0
    # bucket index -> volume. Two dicts rather than one of tuples: the hot path
    # touches exactly one of them per trade.
    buy_at: dict[int, float] = field(default_factory=dict)
    sell_at: dict[int, float] = field(default_factory=dict)
    book_at: dict[int, float] = field(default_factory=dict)
    cvd: float = 0.0           # running, across columns

    @property
    def delta(self) -> float:
        return self.buy - self.sell

    def to_dict(self) -> dict:
        return {
            "t": self.start_ms,
            "o": self.open, "h": self.high, "l": self.low, "c": self.close,
            "b": round(self.buy, 6), "s": round(self.sell, 6),
            "cvd": round(self.cvd, 6),
            # Lists, not dicts: JSON object keys must be strings, and the client
            # wants pairs anyway. Flat [idx, vol, idx, vol, ...] halves the
            # bracket count over a list of pairs, which at 180 columns matters.
            "ba": _flatten(self.buy_at),
            "sa": _flatten(self.sell_at),
            "ka": _flatten(self.book_at),
        }


def _flatten(d: dict[int, float]) -> list:
    out: list = []
    for k, v in d.items():
        out.append(k)
        out.append(round(v, 4))
    return out


@dataclass
class FlowTape:
    """Rolling columns of candle + footprint + book, ready to publish."""

    interval_ms: int = DEFAULT_INTERVAL_MS
    max_columns: int = DEFAULT_COLUMNS
    bucket_size: float = 0.0            # price units per footprint row; 0 = unset
    columns: deque = field(default_factory=deque)
    _cvd: float = 0.0
    _seq: int = 0                       # increments when a column CLOSES
    _last_book: OrderBookSnapshot | None = None

    def __post_init__(self) -> None:
        self.columns = deque(maxlen=self.max_columns)

    # ── hot path ───────────────────────────────────────────────────────

    def on_tick(self, tick: Tick) -> None:
        price, qty, ts = tick.price, tick.quantity, tick.timestamp
        start = ts - (ts % self.interval_ms)

        col = self.columns[-1] if self.columns else None
        if col is None or start > col.start_ms:
            col = self._open_column(start, price)
        elif start < col.start_ms:
            return          # out-of-order tick from a reconnect; the past is closed

        if price > col.high:
            col.high = price
        elif price < col.low:
            col.low = price
        col.close = price

        # is_buyer_maker means the BUYER was passive, so the seller crossed the
        # spread. Same mapping as src/data/cvd.py — one rule, stated twice only
        # because the two paths never import each other.
        if tick.is_buyer_maker:
            col.sell += qty
            self._add_at(col.sell_at, price, qty)
            self._cvd -= qty
        else:
            col.buy += qty
            self._add_at(col.buy_at, price, qty)
            self._cvd += qty
        col.cvd = self._cvd

    def on_book(self, snapshot: OrderBookSnapshot) -> None:
        """Remember the book; it is sampled when the column closes."""
        self._last_book = snapshot

    # ── internals ──────────────────────────────────────────────────────

    def _open_column(self, start_ms: int, price: float) -> Column:
        if self.columns:
            self._sample_book(self.columns[-1])
            self._seq += 1
        col = Column(start_ms=start_ms, open=price, high=price, low=price,
                     close=price, cvd=self._cvd)
        self.columns.append(col)
        return col

    def _bucket(self, price: float) -> int:
        return int(round(price / self.bucket_size)) if self.bucket_size > 0 else 0

    def _add_at(self, target: dict[int, float], price: float, qty: float) -> None:
        if self.bucket_size <= 0:
            return
        idx = self._bucket(price)
        if idx in target or len(target) < MAX_BUCKETS_PER_COLUMN:
            target[idx] = target.get(idx, 0.0) + qty

    def _sample_book(self, col: Column) -> None:
        """One snapshot per column, taken as it closes.

        Bookmap samples continuously and paints intensity over time; at a 5-second
        column that is a distinction without a difference, and it keeps the depth
        stream off the part of the loop that runs per trade.
        """
        book = self._last_book
        if book is None or self.bucket_size <= 0:
            return
        at = col.book_at
        for side in (book.bids, book.asks):
            for level in side:
                idx = self._bucket(level.price)
                if idx in at or len(at) < MAX_BUCKETS_PER_COLUMN:
                    at[idx] = at.get(idx, 0.0) + level.quantity

    def set_bucket_size(self, size: float) -> bool:
        """Set the footprint row height, clearing history if it changed.

        Every column's footprint and book are keyed by `round(price / bucket)`.
        Change the divisor and the old indices mean a different price — mixing
        two scales in one picture is worse than showing fifteen fewer minutes,
        so the history goes. Returns True when it changed.
        """
        size = float(f"{size:.8g}")
        if size <= 0 or size == self.bucket_size:
            return False
        self.bucket_size = size
        self.columns.clear()
        self._cvd = 0.0
        self._seq += 1
        return True

    # ── reads ──────────────────────────────────────────────────────────

    @property
    def seq(self) -> int:
        """Increments once per CLOSED column — the client's cache key."""
        return self._seq

    @property
    def cvd(self) -> float:
        return self._cvd

    def live_column(self) -> dict | None:
        """The forming column. Pushed every frame; the rest only on `seq`."""
        return self.columns[-1].to_dict() if self.columns else None

    def to_dict(self) -> dict:
        return {
            "seq": self._seq,
            "interval_ms": self.interval_ms,
            "bucket_size": self.bucket_size,
            "has_book": self._last_book is not None,
            "columns": [c.to_dict() for c in self.columns],
        }
