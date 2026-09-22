"""Flow features accumulated while price sits inside a level's zone.

Everything here answers one question: *what did flow do while price was at this
level?* Nothing predicts, nothing extrapolates.

THE COUNTERINTUITIVE BIT — read before changing the sign of anything
--------------------------------------------------------------------
At a SUPPORT level, the bullish evidence is **aggressive SELLING that fails to
move price**. Sellers hammer the bid, a passive buyer soaks it up, price holds.
So a bullish read at support wants `delta < 0` with a small price excursion.

Getting this backwards produces a system that buys support only when buyers are
already lifting the ask — i.e. after the move, at the worst price. Absorption is
about who is *failing*, not who is *pushing*.


ABSORPTION RATIO
----------------
    absorption = |delta_ratio| x (1 - min(1, range_ratio))

- ``delta_ratio`` in [-1, 1]: how one-sided the aggression was
- ``range_ratio``: the zone's price excursion over a recent baseline range

One-sided aggression (|delta_ratio| near 1) that moved price far less than
normal (range_ratio near 0) scores near 1. Aggression that moved price the usual
amount scores 0 — that is just a normal move, not absorption.

The result is then scaled by a confidence ramp on trade count. Without it, a
zone holding three trades has an excursion near zero and therefore "perfect"
absorption — a number that is arithmetically true and completely meaningless.
`sufficient` already blocks signalling on thin zones, but the score is also
displayed live, and a confident-looking 0.95 built on three prints is exactly
the kind of false authority this system is supposed to avoid.


WHAT A TRADER READS THAT RAW DELTA DOES NOT CAPTURE
---------------------------------------------------
- **Tape velocity.** Trades per second, and whether it is accelerating.
  Acceleration into a level is the stop-run signature; deceleration inside one
  is exhaustion. Cumulative trade count says nothing about either.
- **Print size distribution.** Five prints of 40 and five hundred of 0.4 carry
  identical volume and opposite meaning. `large_print_share` is the fraction of
  zone volume that arrived in outsized trades.
- **Lower-volume retest.** After the first reaction, price returns to probe the
  level again. If that probe trades *less* than the first, the aggressors are
  exhausted — there is nobody left to push. This is the highest-quality
  confirmation available and it requires tracking probes, not just totals.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from src.utils.types import OrderBookSnapshot, Tick

# A score computed from a handful of trades is noise, not evidence. Zones below
# this many trades are reported but never signalled on.
MIN_TRADES_FOR_EVIDENCE = 20

# A print this many times the market's recent average size counts as large.
LARGE_PRINT_MULT = 4.0

# Probe detection: price must retrace this fraction of the zone half-width
# away from its extreme before a return counts as a separate probe. Without
# it, every tick at the low would register as a new test.
PROBE_RETRACE_FRAC = 0.35
PROBE_EPS_FRAC = 0.15


@dataclass(frozen=True)
class MarketContext:
    """Recent market norms, supplied by the engine, used to normalize.

    Both are measured OUTSIDE the zone so a quiet or frantic zone is judged
    against the market's own behaviour rather than its own.
    """

    baseline_range: float = 0.0        # typical recent price excursion
    baseline_trade_size: float = 0.0   # typical recent print size


@dataclass
class ZoneFeatures:
    """Immutable snapshot of what flow did inside a zone."""

    dwell_ms: int
    trade_count: int
    volume: float
    buy_volume: float
    sell_volume: float
    delta: float
    delta_ratio: float          # delta / volume, in [-1, 1]
    high: float
    low: float
    excursion: float            # high - low inside the zone
    range_ratio: float          # excursion / baseline range
    absorption: float           # in [0, 1] — see module docstring
    adverse_excursion: float    # how far price pushed PAST the level, wrong way
    late_delta_ratio: float     # delta ratio over the most recent window
    trades_per_sec: float       # tape velocity over the recent window
    velocity_ratio: float       # recent velocity / earlier velocity (>1 = accelerating)
    large_print_count: int      # prints above LARGE_PRINT_MULT x market average
    large_print_share: float    # fraction of zone volume from those prints, [0, 1]
    probe_count: int            # distinct probes of the zone extreme
    retest_volume_ratio: float  # latest probe volume / first probe volume
    test_count: int             # which test of this level this is (1-based)
    book_size_at_level: float   # resting size near the level (0 when no book)
    book_refills: int           # times that resting size replenished — iceberg proxy
    book_imbalance: float       # (bid - ask) / (bid + ask) near touch, in [-1, 1]
    has_book: bool

    @property
    def sufficient(self) -> bool:
        """Enough activity for the evidence to mean anything."""
        return self.trade_count >= MIN_TRADES_FOR_EVIDENCE and self.volume > 0

    @property
    def exhausted_retest(self) -> bool:
        """A repeat probe of the extreme that traded less than the first.

        Nobody left to push. The strongest single confirmation available, and
        it only exists after at least two probes.
        """
        return self.probe_count >= 2 and 0.0 < self.retest_volume_ratio < 0.7


@dataclass
class ZoneAccumulator:
    """Accumulates trades and book state while price is inside one zone.

    Args:
        level_price: centre of the zone, for adverse-excursion measurement.
        is_support: True for a long/support level, False for resistance.
        baseline_range: typical recent price range, used to normalize excursion.
            Pass a positive number; a zero or negative baseline disables the
            range comparison (range_ratio reports 1.0 = "normal").
        test_count: which test of this level this is.
        late_window_ms: window for `late_delta_ratio`.
    """

    level_price: float
    is_support: bool
    baseline_range: float
    test_count: int = 1
    late_window_ms: int = 30_000
    baseline_trade_size: float = 0.0
    zone_width: float = 0.0

    entered_ms: int = 0
    _last_ms: int = 0
    _trades: int = 0
    _buy: float = 0.0
    _sell: float = 0.0
    _high: float = float("-inf")
    _low: float = float("inf")
    # Bounded deque, not a list: the hot path must not grow unboundedly or
    # pay for periodic re-filtering. maxlen caps memory; the time cutoff in
    # _late_delta_ratio does the windowing.
    _recent: deque[tuple[int, float, bool]] = field(
        default_factory=lambda: deque(maxlen=8192)
    )
    _book_size: float = 0.0
    _book_peak: float = 0.0
    _book_refills: int = 0
    _book_imbalance: float = 0.0
    _has_book: bool = False
    _large_count: int = 0
    _large_volume: float = 0.0
    # Probe tracking — see PROBE_* constants.
    _extreme: float = 0.0
    _probe_volumes: list[float] = field(default_factory=list)
    _in_probe: bool = False
    _current_probe_volume: float = 0.0

    def on_tick(self, tick: Tick) -> None:
        if self.entered_ms == 0:
            self.entered_ms = tick.timestamp
            self._extreme = tick.price
        self._last_ms = max(self._last_ms, tick.timestamp)
        self._trades += 1

        if (
            self.baseline_trade_size > 0
            and tick.quantity >= self.baseline_trade_size * LARGE_PRINT_MULT
        ):
            self._large_count += 1
            self._large_volume += tick.quantity

        self._track_probe(tick.price, tick.quantity)

        if tick.is_buyer_maker:
            self._sell += tick.quantity
        else:
            self._buy += tick.quantity

        self._high = max(self._high, tick.price)
        self._low = min(self._low, tick.price)

        self._recent.append((tick.timestamp, tick.quantity, tick.is_buyer_maker))

    def _track_probe(self, price: float, qty: float) -> None:
        """Measure the volume traded DURING each probe of the zone extreme.

        The comparison that matters is "the first test traded 100, the retest
        traded 30" — volume *at* the extreme, not volume between visits. A
        probe opens when price reaches within PROBE_EPS_FRAC of the extreme and
        closes when it retraces PROBE_RETRACE_FRAC away; the retrace gate stops
        every tick at the low registering as a fresh test.

        A continuous push to new lows stays one probe, which is correct — that
        is one sustained attack, not several.
        """
        width = self.zone_width if self.zone_width > 0 else max(self.baseline_range, 1e-9)
        retrace = width * PROBE_RETRACE_FRAC
        eps = width * PROBE_EPS_FRAC

        if self.is_support:
            if price < self._extreme:
                self._extreme = price
            at_extreme = price <= self._extreme + eps
            retraced_away = price >= self._extreme + retrace
        else:
            if price > self._extreme:
                self._extreme = price
            at_extreme = price >= self._extreme - eps
            retraced_away = price <= self._extreme - retrace

        if at_extreme:
            if not self._in_probe:
                self._in_probe = True
                self._current_probe_volume = 0.0
            self._current_probe_volume += qty
        elif retraced_away and self._in_probe:
            self._probe_volumes.append(self._current_probe_volume)
            self._in_probe = False
            self._current_probe_volume = 0.0

    def _probes(self) -> list[float]:
        """Closed probes plus the one in progress — a retest happening right
        now is exactly what you want to see, not something to wait out."""
        probes = list(self._probe_volumes)
        if self._in_probe and self._current_probe_volume > 0:
            probes.append(self._current_probe_volume)
        return probes

    def on_book(self, snapshot: OrderBookSnapshot, band: float) -> None:
        """Fold in book state. `band` is how far from the level to aggregate."""
        if snapshot is None:
            return
        self._has_book = True
        levels = snapshot.bids if self.is_support else snapshot.asks
        size = sum(
            lv.quantity for lv in levels
            if abs(lv.price - self.level_price) <= band
        )

        # A refill is size recovering after being eaten down — the visible
        # signature of an iceberg working at the level.
        if size > self._book_peak * 0.9 and self._book_size < self._book_peak * 0.5:
            self._book_refills += 1
        self._book_peak = max(self._book_peak, size)
        self._book_size = size

        bid = sum(lv.quantity for lv in snapshot.bids[:10])
        ask = sum(lv.quantity for lv in snapshot.asks[:10])
        total = bid + ask
        self._book_imbalance = (bid - ask) / total if total > 0 else 0.0

    def features(self) -> ZoneFeatures:
        volume = self._buy + self._sell
        delta = self._buy - self._sell
        delta_ratio = delta / volume if volume > 0 else 0.0

        high = self._high if self._high != float("-inf") else self.level_price
        low = self._low if self._low != float("inf") else self.level_price
        excursion = max(0.0, high - low)

        if self.baseline_range > 0:
            range_ratio = excursion / self.baseline_range
        else:
            range_ratio = 1.0  # unknown baseline → assume normal, absorption 0

        absorption = abs(delta_ratio) * (1.0 - min(1.0, range_ratio))
        # Confidence ramp — see module docstring.
        absorption *= min(1.0, self._trades / MIN_TRADES_FOR_EVIDENCE)

        # Adverse excursion: how far price pushed in the direction that would
        # invalidate the level — below it for support, above it for resistance.
        adverse = (self.level_price - low) if self.is_support else (high - self.level_price)

        rate, velocity_ratio = self._velocity()
        probes = self._probes()
        if len(probes) >= 2 and probes[0] > 0:
            retest_ratio = probes[-1] / probes[0]
        else:
            retest_ratio = 0.0

        return ZoneFeatures(
            dwell_ms=max(0, self._last_ms - self.entered_ms),
            trade_count=self._trades,
            volume=volume,
            buy_volume=self._buy,
            sell_volume=self._sell,
            delta=delta,
            delta_ratio=delta_ratio,
            high=high,
            low=low,
            excursion=excursion,
            range_ratio=range_ratio,
            absorption=absorption,
            adverse_excursion=max(0.0, adverse),
            late_delta_ratio=self._late_delta_ratio(),
            trades_per_sec=rate,
            velocity_ratio=velocity_ratio,
            large_print_count=self._large_count,
            large_print_share=(self._large_volume / volume) if volume > 0 else 0.0,
            probe_count=len(probes),
            retest_volume_ratio=retest_ratio,
            test_count=self.test_count,
            book_size_at_level=self._book_size,
            book_refills=self._book_refills,
            book_imbalance=self._book_imbalance,
            has_book=self._has_book,
        )

    def _velocity(self) -> tuple[float, float]:
        """(trades/sec in the recent window, recent rate / earlier rate).

        A ratio above 1 means the tape is accelerating — the stop-run
        signature on arrival, and a warning sign inside a zone.
        """
        if len(self._recent) < 4:
            return 0.0, 1.0
        window = self.late_window_ms
        cutoff = self._last_ms - window
        prior_cutoff = cutoff - window
        recent = earlier = 0
        for ts, _qty, _maker in reversed(self._recent):
            if ts >= cutoff:
                recent += 1
            elif ts >= prior_cutoff:
                earlier += 1
            else:
                break
        seconds = window / 1000.0
        rate = recent / seconds if seconds > 0 else 0.0
        ratio = (recent / earlier) if earlier > 0 else 1.0
        return rate, ratio

    def _late_delta_ratio(self) -> float:
        """Delta ratio over the most recent window — is flow turning?"""
        cutoff = self._last_ms - self.late_window_ms
        buy = sell = 0.0
        # Walk backwards and stop at the cutoff — recent entries are at the end,
        # so this touches only the window, not the whole deque.
        for ts, qty, maker in reversed(self._recent):
            if ts < cutoff:
                break
            if maker:
                sell += qty
            else:
                buy += qty
        total = buy + sell
        return (buy - sell) / total if total > 0 else 0.0
