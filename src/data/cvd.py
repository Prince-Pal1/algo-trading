"""Cumulative Volume Delta (CVD) — aggressive buy/sell flow from trade data.

CVD measures *aggression*: who crosses the spread with market orders, as
opposed to who rests limit orders in the book. It is the running sum of
(volume traded into the ask − volume traded into the bid)::

    delta[bar] = buy_volume − sell_volume
    cvd[n]     = Σ delta[0..n]

This is the complement of a liquidity heatmap: a heatmap shows *passive*
resting size, CVD shows *aggressive* flow. Neither sees what the other sees.


AGGRESSOR CONVENTION — read before changing anything
----------------------------------------------------
``Tick.is_buyer_maker`` follows the Binance ``trade``/``aggTrade`` convention:

===================== ================================================
``is_buyer_maker``    meaning
===================== ================================================
``True``              the BUYER was the passive maker (resting bid), so
                      the SELLER crossed the spread → **SELL volume**
``False``             the BUYER lifted the ask → **BUY volume**
===================== ================================================

It reads backwards from intuition, and inverting it silently flips every
signal built on top of this module. ``_signed_quantity`` is the single
place the mapping lives — change it there or nowhere.


DATA REQUIREMENTS
-----------------
CVD is only meaningful on a feed that publishes both a trade size and an
aggressor flag:

- ``src/data/feeds/binance_ws.py`` — real ``quantity`` + real
  ``is_buyer_maker`` from the ``@trade`` stream. ✅
- ``src/data/feeds/icmarkets_feed.py`` — hardcodes ``quantity=0.0`` and
  ``is_buyer_maker=False`` because a CFD has no central tape: there is no
  trade size and no aggressor to report. ❌

Feeding CFD ticks here yields a flat, meaningless CVD, so ``CVDCalculator``
detects the degenerate case and warns once instead of silently emitting
zeros. See ARCHITECTURE.md Known Gotchas.

Usage::

    calc = CVDCalculator("BTCUSDT", "1m")
    for tick in ticks:
        bar = calc.update(tick)
        if bar is not None:
            print(bar.timestamp, bar.delta, bar.cvd)

    bars = compute_delta_bars(trades_df, "5m", symbol="BTCUSDT")
    divs = detect_divergences(bars, lookback=20)
    absorp = detect_absorption(bars)
"""

from __future__ import annotations

import msgspec
import numpy as np
import pandas as pd

from src.data.candle_builder import TF_MS
from src.utils.logger import get_logger
from src.utils.types import Tick

log = get_logger("cvd")

# Ticks observed before the degenerate-feed check fires. Low enough to catch a
# dead feed quickly, high enough that a quiet genuine feed is not flagged.
DEGENERATE_CHECK_AFTER = 50

BAR_COLUMNS = [
    "timestamp", "open", "high", "low", "close",
    "buy_volume", "sell_volume", "volume",
    "delta", "delta_high", "delta_low", "cvd", "trade_count",
]


def _signed_quantity(quantity: float, is_buyer_maker: bool) -> float:
    """Signed aggressor volume: positive = aggressive buy, negative = aggressive sell.

    THE aggressor mapping. See module docstring before editing.
    """
    return -quantity if is_buyer_maker else quantity


class DeltaBar(msgspec.Struct, frozen=True):
    """One timeframe bucket of order flow, with price for divergence work."""

    symbol: str
    timeframe: str
    timestamp: int              # Unix ms, bar open time (timeframe-aligned)
    open: float
    high: float
    low: float
    close: float
    buy_volume: float           # volume that lifted the ask
    sell_volume: float          # volume that hit the bid
    delta: float                # buy_volume − sell_volume
    delta_high: float           # max of the running intrabar delta
    delta_low: float            # min of the running intrabar delta
    cvd: float                  # cumulative delta through this bar
    trade_count: int

    @property
    def volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def delta_ratio(self) -> float:
        """Delta as a fraction of bar volume — one-sidedness in [-1, 1]."""
        vol = self.volume
        return self.delta / vol if vol > 0 else 0.0


class CVDCalculator:
    """Streaming tick → delta bar aggregator.

    Stateful, one instance per (symbol, timeframe). ``update`` returns a
    ``DeltaBar`` at the moment a bar closes — i.e. when a tick arrives that
    belongs to a later bucket — and ``None`` otherwise. Ticks older than the
    bar in progress are counted and dropped rather than corrupting it.
    """

    def __init__(self, symbol: str, timeframe: str, initial_cvd: float = 0.0):
        if timeframe not in TF_MS:
            raise ValueError(f"unsupported timeframe: {timeframe!r} (known: {sorted(TF_MS)})")
        self.symbol = symbol
        self.timeframe = timeframe
        self.tf_ms = TF_MS[timeframe]
        self._initial_cvd = initial_cvd
        self.reset()

    def reset(self) -> None:
        self._cvd = self._initial_cvd
        self._bar_start: int | None = None
        self._open = 0.0
        self._high = 0.0
        self._low = 0.0
        self._close = 0.0
        self._buy = 0.0
        self._sell = 0.0
        self._running_delta = 0.0
        self._delta_high = 0.0
        self._delta_low = 0.0
        self._trades = 0
        self._tick_count = 0
        self._zero_qty_ticks = 0
        self._out_of_order = 0
        self._degenerate_warned = False

    # ── ingest ─────────────────────────────────────────────────────────

    def update(self, tick: Tick) -> DeltaBar | None:
        """Feed one trade tick. Returns the previous bar if this tick closed it."""
        self._tick_count += 1
        if tick.quantity <= 0.0:
            self._zero_qty_ticks += 1
        self._check_degenerate()

        bucket = (tick.timestamp // self.tf_ms) * self.tf_ms
        closed: DeltaBar | None = None

        if self._bar_start is None:
            self._start_bar(bucket)
        elif bucket > self._bar_start:
            closed = self._close_bar()
            self._start_bar(bucket)
        elif bucket < self._bar_start:
            # Late tick belonging to an already-closed bar — drop it rather
            # than fold it into the wrong bucket.
            self._out_of_order += 1
            return None

        self._accumulate(tick)
        return closed

    def flush(self) -> DeltaBar | None:
        """Close the bar in progress. Call at end of stream/backfill."""
        if self._bar_start is None or self._trades == 0:
            return None
        bar = self._close_bar()
        self._bar_start = None
        return bar

    # ── internals ──────────────────────────────────────────────────────

    def _start_bar(self, bucket: int) -> None:
        self._bar_start = bucket
        self._open = self._high = self._low = self._close = 0.0
        self._buy = self._sell = 0.0
        self._running_delta = 0.0
        self._delta_high = 0.0
        self._delta_low = 0.0
        self._trades = 0

    def _accumulate(self, tick: Tick) -> None:
        if self._trades == 0:
            self._open = self._high = self._low = tick.price
        else:
            if tick.price > self._high:
                self._high = tick.price
            if tick.price < self._low:
                self._low = tick.price
        self._close = tick.price

        if tick.is_buyer_maker:
            self._sell += tick.quantity
        else:
            self._buy += tick.quantity

        self._running_delta += _signed_quantity(tick.quantity, tick.is_buyer_maker)
        if self._running_delta > self._delta_high:
            self._delta_high = self._running_delta
        if self._running_delta < self._delta_low:
            self._delta_low = self._running_delta

        self._trades += 1

    def _close_bar(self) -> DeltaBar:
        delta = self._buy - self._sell
        self._cvd += delta
        assert self._bar_start is not None  # guarded by callers
        return DeltaBar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            timestamp=self._bar_start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            buy_volume=self._buy,
            sell_volume=self._sell,
            delta=delta,
            delta_high=self._delta_high,
            delta_low=self._delta_low,
            cvd=self._cvd,
            trade_count=self._trades,
        )

    def _check_degenerate(self) -> None:
        """Warn once if the feed carries no usable trade size."""
        if self._degenerate_warned or self._tick_count < DEGENERATE_CHECK_AFTER:
            return
        if self._zero_qty_ticks == self._tick_count:
            log.warning(
                "cvd_degenerate_feed",
                symbol=self.symbol,
                timeframe=self.timeframe,
                ticks=self._tick_count,
                reason="all ticks have quantity=0 — quote-only feed has no tape",
            )
            self._degenerate_warned = True

    # ── state ──────────────────────────────────────────────────────────

    @property
    def current_cvd(self) -> float:
        """CVD through the last *closed* bar."""
        return self._cvd

    @property
    def pending_delta(self) -> float:
        """Delta accumulated so far in the bar still forming."""
        return self._buy - self._sell

    @property
    def is_degenerate(self) -> bool:
        """True once enough ticks have arrived and none carried a size."""
        return (
            self._tick_count >= DEGENERATE_CHECK_AFTER
            and self._zero_qty_ticks == self._tick_count
        )

    @property
    def dropped_out_of_order(self) -> int:
        return self._out_of_order


# ── Vectorized path (historical backfill) ──────────────────────────────


def compute_delta_bars(
    trades: pd.DataFrame,
    timeframe: str,
    symbol: str = "",
    initial_cvd: float = 0.0,
) -> pd.DataFrame:
    """Aggregate a raw trade DataFrame into delta bars.

    Args:
        trades: columns ``timestamp`` (Unix ms), ``price``, ``quantity``,
            ``is_buyer_maker`` (bool).
        timeframe: any key of ``TF_MS``.
        symbol: stamped onto the result for provenance.
        initial_cvd: CVD carried in from a previous chunk, so day-by-day
            backfills produce one continuous series.

    Returns:
        DataFrame with ``BAR_COLUMNS``, one row per non-empty bucket,
        ascending by timestamp.
    """
    if timeframe not in TF_MS:
        raise ValueError(f"unsupported timeframe: {timeframe!r} (known: {sorted(TF_MS)})")

    if trades.empty:
        out = pd.DataFrame(columns=BAR_COLUMNS)
        out["symbol"] = pd.Series(dtype="object")
        return out

    tf_ms = TF_MS[timeframe]
    df = trades.sort_values("timestamp", kind="stable").reset_index(drop=True)

    qty = df["quantity"].astype(float)
    maker = df["is_buyer_maker"].astype(bool)
    bucket = (df["timestamp"].astype("int64") // tf_ms) * tf_ms

    signed = np.where(maker, -qty, qty)
    buy = np.where(maker, 0.0, qty)
    sell = np.where(maker, qty, 0.0)

    work = pd.DataFrame({
        "bucket": bucket,
        "price": df["price"].astype(float),
        "buy": buy,
        "sell": sell,
        "signed": signed,
    })

    # Running intrabar delta, reset each bucket — gives delta_high/delta_low.
    work["running"] = work.groupby("bucket", sort=False)["signed"].cumsum()

    grouped = work.groupby("bucket", sort=True)
    bars = grouped.agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        buy_volume=("buy", "sum"),
        sell_volume=("sell", "sum"),
        delta_high=("running", "max"),
        delta_low=("running", "min"),
        trade_count=("price", "size"),
    ).reset_index().rename(columns={"bucket": "timestamp"})

    bars["volume"] = bars["buy_volume"] + bars["sell_volume"]
    bars["delta"] = bars["buy_volume"] - bars["sell_volume"]
    bars["cvd"] = bars["delta"].cumsum() + initial_cvd
    # An all-buy bar never goes negative, and vice versa — clamp the running
    # extremes so they bracket zero the way the streaming path does.
    bars["delta_high"] = bars["delta_high"].clip(lower=0.0)
    bars["delta_low"] = bars["delta_low"].clip(upper=0.0)
    bars["timestamp"] = bars["timestamp"].astype("int64")
    bars["trade_count"] = bars["trade_count"].astype("int64")
    bars["symbol"] = symbol

    return bars[BAR_COLUMNS + ["symbol"]]


def delta_ratio(bars: pd.DataFrame) -> pd.Series:
    """Delta as a fraction of bar volume, in [-1, 1]. Zero-volume bars → 0."""
    vol = bars["volume"].astype(float)
    return (bars["delta"].astype(float) / vol.where(vol > 0)).fillna(0.0)


# ── Pattern detection ──────────────────────────────────────────────────


def detect_divergences(
    bars: pd.DataFrame,
    lookback: int = 20,
    z_threshold: float = 1.0,
    norm_window: int | None = None,
) -> pd.DataFrame:
    """Flag price/CVD divergences — the quantified form of "price up, CVD not".

    Over a rolling ``lookback``, compare the change in price against the change
    in CVD. Each is z-scored against its own recent variability so the test is
    scale-free and comparable across symbols and regimes. A divergence is a bar
    where the two moved in *opposite* directions and both moves were large
    enough to matter.

    ``bearish`` = price up, CVD down: the rally is not backed by aggressive
    buying (passive sellers are absorbing it).
    ``bullish`` = price down, CVD up: the selloff is not backed by aggressive
    selling.

    This is a screen, not a signal. Divergence against a strong trend is a
    classic losing fade — pair it with context before acting on it.

    Args:
        bars: output of ``compute_delta_bars`` (needs ``close``, ``cvd``).
        lookback: bars over which the change is measured.
        z_threshold: minimum |z| on BOTH legs.
        norm_window: window for the z-score std (default ``lookback * 5``).

    Returns:
        One row per divergence: timestamp, kind, price_z, cvd_z, strength,
        close, cvd. Empty (with those columns) when none found.
    """
    cols = ["timestamp", "kind", "price_z", "cvd_z", "strength", "close", "cvd"]
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    if bars.empty or len(bars) <= lookback:
        return pd.DataFrame(columns=cols)

    window = norm_window if norm_window is not None else lookback * 5
    min_periods = max(2, lookback // 2)

    price_chg = bars["close"].astype(float).diff(lookback)
    cvd_chg = bars["cvd"].astype(float).diff(lookback)

    price_z = _zscore(price_chg, window, min_periods)
    cvd_z = _zscore(cvd_chg, window, min_periods)

    opposite = np.sign(price_z) * np.sign(cvd_z) < 0
    strong = (price_z.abs() >= z_threshold) & (cvd_z.abs() >= z_threshold)
    hit = opposite & strong

    if not hit.any():
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame({
        "timestamp": bars.loc[hit, "timestamp"].to_numpy(),
        "kind": np.where(price_z[hit] > 0, "bearish", "bullish"),
        "price_z": price_z[hit].to_numpy(),
        "cvd_z": cvd_z[hit].to_numpy(),
        "strength": np.minimum(price_z[hit].abs(), cvd_z[hit].abs()).to_numpy(),
        "close": bars.loc[hit, "close"].to_numpy(),
        "cvd": bars.loc[hit, "cvd"].to_numpy(),
    })
    return out.reset_index(drop=True)


def detect_absorption(
    bars: pd.DataFrame,
    delta_ratio_threshold: float = 0.35,
    range_ratio_threshold: float = 0.6,
    range_window: int = 20,
) -> pd.DataFrame:
    """Flag absorption — heavy one-sided aggression that failed to move price.

    A bar qualifies when aggression was lopsided (``|delta/volume|`` above
    ``delta_ratio_threshold``) *and* the bar's range was unusually small
    relative to recent bars (``range_ratio_threshold``). That combination is
    the signature of a large passive participant soaking up market orders.

    ``sell_absorbed`` = aggressive selling absorbed by a passive buyer
    (potentially bullish); ``buy_absorbed`` = the mirror case.

    Returns:
        One row per absorption bar: timestamp, kind, delta_ratio, range_ratio,
        close, delta, volume. Empty (with those columns) when none found.
    """
    cols = ["timestamp", "kind", "delta_ratio", "range_ratio", "close", "delta", "volume"]
    if range_window < 1:
        raise ValueError(f"range_window must be >= 1, got {range_window}")
    if bars.empty:
        return pd.DataFrame(columns=cols)

    ratio = delta_ratio(bars)
    bar_range = (bars["high"].astype(float) - bars["low"].astype(float))
    avg_range = bar_range.rolling(range_window, min_periods=max(2, range_window // 2)).mean()
    range_ratio = (bar_range / avg_range.where(avg_range > 0)).fillna(np.inf)

    hit = (ratio.abs() >= delta_ratio_threshold) & (range_ratio <= range_ratio_threshold)
    if not hit.any():
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame({
        "timestamp": bars.loc[hit, "timestamp"].to_numpy(),
        "kind": np.where(ratio[hit] > 0, "buy_absorbed", "sell_absorbed"),
        "delta_ratio": ratio[hit].to_numpy(),
        "range_ratio": range_ratio[hit].to_numpy(),
        "close": bars.loc[hit, "close"].to_numpy(),
        "delta": bars.loc[hit, "delta"].to_numpy(),
        "volume": bars.loc[hit, "volume"].to_numpy(),
    })
    return out.reset_index(drop=True)


def _zscore(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Rolling z-score. Flat windows (std 0 or NaN) score 0, never inf."""
    std = series.rolling(window, min_periods=min_periods).std()
    z = series / std.where(std > 0)
    return z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
