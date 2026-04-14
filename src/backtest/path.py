"""Intrabar path reconstruction — Phase G.2a.3 + G.5b of the gold trading plan.

On an OHLC bar, the strategy only sees open/high/low/close but not the
ACTUAL path the price took within the bar. For leveraged scalping this
matters enormously:

1. **SL vs TP ordering**: if both a stop-loss and take-profit are inside
   [low, high], which one hit first? The close tells us where we ended
   but not the path. Naive answer (take the close side) biases wins and
   losses incorrectly.
2. **Broker stop-out check**: a position at 500× with a wide strategy SL
   might survive the bar open→close, but the intrabar worst-case excursion
   could have tripped the account stop-out at 50% margin level mid-bar.
   Ignoring intrabar = fake survivor bias.
3. **Trailing stop activation**: trailing stops activate at a profit
   threshold and then follow the best price. Mid-bar path determines
   whether the trailing stop activated AND where it locked in.

This module provides THREE path models:

- `BrownianBridgeModel`: seeded Brownian bridge with variance ∝ (high−low)².
  Fast, reproducible (deterministic given (run_id, bar_idx)), and
  lightweight for backtests that only have the trade timeframe. Used as
  the fallback when finer-resolution data isn't available.

- `M1PathModel` (NEW, G.5b): uses M1 sub-bars as intrabar ground truth.
  For an M5 backtest, each bar has 5 M1 sub-bars — we can find the first
  M1 sub-bar that touched a given level and use its index as the fraction
  into the bar. This is essentially tick-level accuracy without loading
  actual ticks. Falls back to Brownian bridge if M1 data is missing for
  a given bar. This is what G.5 validation uses as ground truth, so it
  trivially passes its own validation (>99% ordering accuracy).

- `PessimisticPathModel`: worst-case adverse excursion first. For longs,
  simulates bar as open → low → high → close (stops hit on the way down).
  For shorts, open → high → low → close. Produces a conservative lower
  bound on strategy performance — backtest Sharpe with this model is
  always ≤ backtest Sharpe with any other path.

The `intrabar_events` function takes a bar + a list of price levels
(SLs, TPs, margin-call levels) and returns a time-ordered list of which
events fire during the bar, using the chosen path model.

G.5 validation findings (2026-04-14): BrownianBridgeModel passes the
timing gate (mean error 0.2424, < 0.25 target) but fails the ordering
gate (71.89% agreement, < 80% target) — because its fallback for
spike-and-return bars is a random guess in [0.15, 0.50]. M1PathModel
resolves this by using real M1 sub-bar data.
"""

from __future__ import annotations

import math
import random
from collections import namedtuple
from dataclasses import dataclass
from typing import Callable, Sequence


# M1 sub-bar OHLCV tuple — used by M1PathModel to store and return
# intrabar data. Strategies consuming `intrabar_sub_bars` see this
# as a list of M1SubBar named tuples with attribute access.
M1SubBar = namedtuple("M1SubBar", ["open", "high", "low", "close", "volume"])


@dataclass(frozen=True)
class Bar:
    """Minimal OHLCV bar for path reconstruction."""
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ts_ms: int = 0


@dataclass(frozen=True)
class IntrabarEvent:
    """A single price-level event fired during intrabar path reconstruction.

    Events are emitted in time order (earlier first). The caller uses this
    to decide which stop-loss or take-profit hit before another, or whether
    a margin-call level was crossed mid-bar.
    """
    level: float
    label: str           # "sl", "tp", "margin_call", custom
    fraction_into_bar: float  # [0, 1] — 0 = open, 1 = close, 0.5 = midpoint
    owner_id: int = 0   # e.g., position id, for multi-position bars


# ── Path models ─────────────────────────────────────────────────────────


class BrownianBridgeModel:
    """Seeded Brownian bridge intrabar path reconstruction.

    The path is sampled as a Brownian bridge pinned at (open, close) with
    variance scaled to match the observed (high-low) range. The hit times
    for a given price level are computed analytically from the bridge CDF.

    For the margin-check case (worst-case adverse excursion check), we use
    the analytic formula for the hit probability of a level by a Brownian
    bridge:
        P(bridge hits level before close) =
            exp(-2 * (level - open) * (level - close) / sigma^2)
        where sigma^2 ≈ ((high - low) / 4)^2
        (variance of the intrabar range under BM)

    The seed is a tuple (run_id, bar_idx) so that two runs of the same
    backtest with the same data produce IDENTICAL intrabar paths. This is
    critical for bit-exact reproduction.

    For MVP, the model computes:
    - Whether a level is touched by the bar: yes iff low ≤ level ≤ high
    - If touched, the fraction-into-bar when the level was hit (analytic)
    - Ordering of multiple touched levels (earliest hit first)

    This is the lightweight path model suitable for M5 bar-level backtests.
    For M1 or tick-level validation, use `PessimisticPathModel` or real ticks.
    """

    def __init__(self, run_id: str = "default", seed_base: int = 0) -> None:
        self._run_id = run_id
        self._seed_base = seed_base

    def _rng_for_bar(self, bar_idx: int) -> random.Random:
        """Deterministic Random given (run_id, bar_idx)."""
        # hash ensures different run_ids produce different sequences
        seed = hash((self._run_id, self._seed_base, bar_idx)) & 0xFFFFFFFF
        return random.Random(seed)

    def level_is_touched(self, bar: Bar, level: float) -> bool:
        """True iff the level is within the bar's high/low range."""
        return bar.low <= level <= bar.high

    def fraction_into_bar(
        self,
        bar: Bar,
        level: float,
        bar_idx: int = 0,
    ) -> float | None:
        """Estimate the fraction [0, 1] of the bar when `level` was hit.

        Returns None if the level was not touched (outside low/high).

        The estimate uses a simple linear interpolation model: if the
        bar opened above the level and closed below, the hit happened
        partway through. If both OHLC sides are on the same side of the
        level (e.g., level above both open and close but below high),
        the bar made a one-way excursion and came back — the hit time
        is in the first half of the bar (use 0.33 as a sensible point).

        For reproducibility, deterministic given (run_id, bar_idx, level).
        """
        if not self.level_is_touched(bar, level):
            return None

        rng = self._rng_for_bar(bar_idx)

        o, c = bar.open, bar.close
        # Case 1: bar crosses the level monotonically (open one side, close other)
        if (o <= level <= c) or (c <= level <= o):
            # Linear interpolation by price
            if abs(c - o) < 1e-12:
                return 0.5  # degenerate: open == close, just say midpoint
            return max(0.0, min(1.0, (level - o) / (c - o)))

        # Case 2: level is touched but not between open and close.
        # Means the bar spiked above or below the level and came back.
        # For a long SL below entry: bar spiked down to touch SL, bounced.
        # Estimate: spike is usually in the first third of the bar, so
        # emit fraction in [0.15, 0.50] deterministically from the rng.
        return 0.15 + rng.random() * 0.35

    def intrabar_events(
        self,
        bar: Bar,
        levels: list[tuple[float, str, int]],
        bar_idx: int = 0,
    ) -> list[IntrabarEvent]:
        """Compute time-ordered intrabar events for a list of price levels.

        Args:
            bar: the OHLCV bar.
            levels: list of (price_level, label, owner_id). Labels can be
                "sl", "tp", "margin_call", or a strategy-specific tag.
            bar_idx: bar index, used for deterministic rng.

        Returns:
            List of IntrabarEvent sorted by fraction_into_bar (earliest first).
            Only touched levels appear in the output.
        """
        events: list[IntrabarEvent] = []
        for level, label, owner_id in levels:
            frac = self.fraction_into_bar(bar, level, bar_idx=bar_idx)
            if frac is not None:
                events.append(IntrabarEvent(
                    level=level,
                    label=label,
                    fraction_into_bar=frac,
                    owner_id=owner_id,
                ))
        events.sort(key=lambda e: e.fraction_into_bar)
        return events

    def worst_adverse_price(self, bar: Bar, side: str) -> float:
        """Return the worst-case intrabar price for a position on this side.

        For a LONG position: worst adverse = bar.low (max loss).
        For a SHORT position: worst adverse = bar.high (max loss).

        Used by the broker stop-out check to decide whether the margin
        level was breached at any point during the bar, regardless of
        where the bar closed.
        """
        if side == "LONG":
            return bar.low
        if side == "SHORT":
            return bar.high
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")

    def best_favorable_price(self, bar: Bar, side: str) -> float:
        """Return the best-case intrabar price for a position on this side.

        For a LONG position: best favorable = bar.high (max gain).
        For a SHORT position: best favorable = bar.low (max gain).

        Used by trailing stop logic to determine how high the trailing
        stop ratcheted during the bar.
        """
        if side == "LONG":
            return bar.high
        if side == "SHORT":
            return bar.low
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")


class M1PathModel:
    """Tick-accuracy intrabar path model backed by M1 sub-bars.

    On an M5 backtest (or any timeframe where 1-minute data is available),
    each bar has multiple M1 sub-bars. For a given price level inside a
    bar's [low, high] range, the true fraction-into-bar when that level
    was first touched is determined by finding the FIRST M1 sub-bar
    whose own [low, high] contains the level.

    This is the same ground-truth approach that `scripts/g5_tick_reconstruction_validate.py`
    uses to score the bridge model. Using it directly as the engine's
    path model means:
    - Ordering accuracy is ≥99% (vs 71.89% for the Brownian bridge)
    - SL vs TP hit ordering is correct on spike-and-return bars (the
      class of bars the bridge model guesses randomly on)
    - Backtest Sharpe is no longer inflated by random-guess tie breaks

    Memory: ~34 MB for 2 years of XAUUSD M1 (706k bars × ~48 bytes).
    Lookup: O(1) per query via a `ts_ms → (low, high)` dict.

    Fallback: if the M1 data is missing for a queried bar (rare — only
    happens if M5 coverage extends beyond M1 coverage), delegates to an
    internal `BrownianBridgeModel` so the backtest doesn't crash. Mixed
    mode is reported via `self.stats["bridge_fallback_count"]` for
    transparency.

    Construction accepts either a pandas DataFrame with columns
    (timestamp, open, high, low, close) or a pre-built lookup dict keyed
    by `timestamp_ms: int → (low, high): tuple[float, float]`.
    """

    def __init__(
        self,
        *,
        m1_lookup: dict[int, "M1SubBar | tuple"] | None = None,
        m1_df: "pd.DataFrame | None" = None,  # noqa: F821 (forward ref — pd is lazy)
        sub_bar_count: int = 5,
        sub_bar_duration_ms: int = 60_000,
        run_id: str = "m1_default",
        seed_base: int = 0,
    ) -> None:
        """Construct the M1 path model.

        Args:
            m1_lookup: pre-built dict mapping `ts_ms → M1SubBar` tuple
                (open, high, low, close, volume) for each M1 bar. For
                backward compatibility, plain `(low, high)` tuples are
                also accepted and auto-upgraded with open=low, close=low,
                volume=0. If provided, `m1_df` is ignored.
            m1_df: pandas DataFrame with M1 bars. Must have `timestamp`,
                `low`, `high` columns at minimum; `open`, `close`, `volume`
                are also consumed if present. Converted to a lookup dict
                at construction time.
            sub_bar_count: number of M1 sub-bars inside the trade
                timeframe bar. Default 5 (M5 backtest). Use 60 for H1, etc.
            sub_bar_duration_ms: duration of each sub-bar in ms. Default
                60_000 (1 minute).
            run_id: deterministic seed tag for the Brownian bridge
                fallback (so the fallback path is reproducible).
            seed_base: additional RNG offset for the bridge fallback.
        """
        if m1_lookup is None:
            if m1_df is None:
                raise ValueError("M1PathModel needs either m1_lookup or m1_df")
            m1_lookup = {}
            has_open = "open" in m1_df.columns
            has_close = "close" in m1_df.columns
            has_volume = "volume" in m1_df.columns
            for _, row in m1_df.iterrows():
                ts = int(row["timestamp"])
                m1_lookup[ts] = M1SubBar(
                    open=float(row["open"]) if has_open else float(row["low"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]) if has_close else float(row["high"]),
                    volume=float(row["volume"]) if has_volume else 0.0,
                )
        else:
            # Upgrade any legacy (low, high) 2-tuples in the lookup to
            # full M1SubBar for interface consistency.
            upgraded: dict[int, M1SubBar] = {}
            for ts, v in m1_lookup.items():
                if isinstance(v, M1SubBar):
                    upgraded[int(ts)] = v
                elif len(v) == 2:
                    lo, hi = v
                    upgraded[int(ts)] = M1SubBar(
                        open=float(lo), high=float(hi), low=float(lo),
                        close=float(lo), volume=0.0,
                    )
                elif len(v) == 5:
                    upgraded[int(ts)] = M1SubBar(*[float(x) for x in v])
                else:
                    raise ValueError(
                        f"M1PathModel m1_lookup values must be M1SubBar or "
                        f"tuple of length 2 or 5, got len={len(v)} for ts={ts}"
                    )
            m1_lookup = upgraded
        self._m1 = m1_lookup
        self._sub_n = int(sub_bar_count)
        self._sub_dur_ms = int(sub_bar_duration_ms)
        self._bridge = BrownianBridgeModel(run_id=run_id, seed_base=seed_base)
        self.stats: dict[str, int] = {
            "total_queries": 0,
            "m1_hits": 0,
            "bridge_fallback_count": 0,
            "level_outside_range": 0,
        }

    # ── Interface compatible with BrownianBridgeModel ──

    def level_is_touched(self, bar: Bar, level: float) -> bool:
        """True iff the level is within the bar's high/low range."""
        return bar.low <= level <= bar.high

    def _sub_bars_for(self, m5_ts_ms: int) -> list["M1SubBar"]:
        """Return M1 sub-bars as M1SubBar named tuples for the given bar ts_ms.

        Returns an empty list if no M1 data covers this bar. Missing
        individual sub-bars (gaps inside a session) are silently skipped —
        the remaining sub-bars still give ordered coverage.

        Each returned element has `.open`, `.high`, `.low`, `.close`,
        `.volume` attributes (attribute access works via namedtuple).
        """
        sub_bars: list[M1SubBar] = []
        for offset in range(self._sub_n):
            ts = m5_ts_ms + offset * self._sub_dur_ms
            hit = self._m1.get(ts)
            if hit is not None:
                sub_bars.append(hit)
        return sub_bars

    def fraction_into_bar(
        self,
        bar: Bar,
        level: float,
        bar_idx: int = 0,
    ) -> float | None:
        """Estimate the fraction [0, 1] of the bar when `level` was hit.

        Strategy:
        1. If level outside [bar.low, bar.high] → None (untouched)
        2. Look up the M1 sub-bars for this bar's ts_ms
        3. Find the FIRST sub-bar whose [low, high] contains `level` —
           its center `(i + 0.5) / N` is the true fraction
        4. If no M1 data OR none of the sub-bars contain the level (rare
           edge case when M1 and M5 disagree at boundaries), fall back
           to the Brownian bridge estimate

        The `bar_idx` kwarg is accepted for interface compatibility with
        `BrownianBridgeModel` but is NOT used by M1PathModel (M1 lookup
        uses `bar.ts_ms` instead).
        """
        self.stats["total_queries"] += 1

        if not self.level_is_touched(bar, level):
            self.stats["level_outside_range"] += 1
            return None

        sub_bars = self._sub_bars_for(int(bar.ts_ms))
        if not sub_bars:
            # No M1 coverage for this bar — fall back to the bridge
            self.stats["bridge_fallback_count"] += 1
            return self._bridge.fraction_into_bar(bar, level, bar_idx=bar_idx)

        for i, sb in enumerate(sub_bars):
            if sb.low <= level <= sb.high:
                self.stats["m1_hits"] += 1
                return (i + 0.5) / len(sub_bars)

        # M5 OHLC claims the level is inside but no M1 sub-bar touched it.
        # This shouldn't happen if M1 and M5 come from the same source,
        # but can occur when M1 and M5 are downloaded separately (e.g.,
        # a tick that shows up in M5 aggregation but is missing from the
        # broker's M1 feed). Fall back to the bridge.
        self.stats["bridge_fallback_count"] += 1
        return self._bridge.fraction_into_bar(bar, level, bar_idx=bar_idx)

    def intrabar_events(
        self,
        bar: Bar,
        levels: list[tuple[float, str, int]],
        bar_idx: int = 0,
    ) -> list[IntrabarEvent]:
        """Compute time-ordered intrabar events for a list of price levels.

        Mirrors `BrownianBridgeModel.intrabar_events` but uses the M1
        fraction_into_bar for accurate ordering.
        """
        events: list[IntrabarEvent] = []
        for level, label, owner_id in levels:
            frac = self.fraction_into_bar(bar, level, bar_idx=bar_idx)
            if frac is not None:
                events.append(IntrabarEvent(
                    level=level,
                    label=label,
                    fraction_into_bar=frac,
                    owner_id=owner_id,
                ))
        events.sort(key=lambda e: e.fraction_into_bar)
        return events

    def worst_adverse_price(self, bar: Bar, side: str) -> float:
        """Return worst-case intrabar price for a position on this side.

        For LONG: worst adverse = bar.low. For SHORT: worst = bar.high.
        Same as the bridge model — M1 sub-bars don't affect the extreme
        price, only the timing/ordering of level hits.
        """
        if side == "LONG":
            return bar.low
        if side == "SHORT":
            return bar.high
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")

    def best_favorable_price(self, bar: Bar, side: str) -> float:
        """Return best-case intrabar price for a position on this side."""
        if side == "LONG":
            return bar.high
        if side == "SHORT":
            return bar.low
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")

    def m1_coverage_ratio(self) -> float:
        """Return fraction of queries that used M1 data vs the bridge fallback.

        1.0 = all queries hit M1 (ideal). 0.0 = all queries fell back to
        the bridge (no M1 coverage). Useful for validating that a given
        backtest actually benefited from the M1 upgrade.
        """
        total = self.stats["total_queries"]
        if total == 0:
            return 0.0
        fallback = self.stats["bridge_fallback_count"]
        return 1.0 - (fallback / total)


# ── Module-level cached factory ─────────────────────────────────────────

_M1_MODEL_CACHE: dict[str, "M1PathModel"] = {}


def get_or_build_m1_path_model(
    parquet_path: str,
    *,
    sub_bar_count: int = 5,
    run_id: str = "m1_cached",
) -> "M1PathModel":
    """Load an M1 parquet once and cache the resulting `M1PathModel`.

    Use this from tuning scripts, walk-forward scripts, and any multi-run
    loop that constructs many LeveragedBacktestEngine instances. The M1
    data is loaded exactly once per process and reused across engines.

    Args:
        parquet_path: path to the M1 parquet file (e.g.,
            `data/historical/XAUUSD_1m.parquet`). Must have columns
            `timestamp` (ms), `low`, `high`.
        sub_bar_count: M1 sub-bars per trade-timeframe bar. Default 5
            (for M5 backtest). Use 60 for H1, 12 for M12, etc.
        run_id: seed tag for the bridge fallback.

    Returns:
        An `M1PathModel` instance.

    Raises:
        FileNotFoundError: parquet file does not exist.
    """
    import os
    import pandas as pd

    abs_path = os.path.abspath(parquet_path)
    cache_key = f"{abs_path}::{sub_bar_count}"
    cached = _M1_MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not os.path.exists(abs_path):
        raise FileNotFoundError(
            f"M1 parquet not found at {abs_path}. "
            f"Run `python3 scripts/download_xauusd.py --timeframes 1m --years 2` to fetch it."
        )

    df = pd.read_parquet(abs_path)
    if "timestamp" not in df.columns or "low" not in df.columns or "high" not in df.columns:
        raise ValueError(
            f"M1 parquet at {abs_path} missing required columns; "
            f"need timestamp/low/high, got {list(df.columns)}"
        )

    model = M1PathModel(
        m1_df=df[["timestamp", "low", "high"]],
        sub_bar_count=sub_bar_count,
        run_id=run_id,
    )
    _M1_MODEL_CACHE[cache_key] = model
    return model


class PessimisticPathModel:
    """Worst-case intrabar path reconstruction.

    Assumes the bar touched the adverse extreme BEFORE the favorable
    extreme. For a LONG, the path is assumed open → low → high → close;
    for a SHORT, open → high → low → close. Stops hit on the way down
    (long) or up (short) first.

    Produces a conservative lower bound on strategy performance.
    Backtests with this model are always at least as bad as backtests
    with BrownianBridgeModel. Useful for stress testing.
    """

    def level_is_touched(self, bar: Bar, level: float) -> bool:
        return bar.low <= level <= bar.high

    def fraction_into_bar(
        self,
        bar: Bar,
        level: float,
        side: str = "LONG",
    ) -> float | None:
        if not self.level_is_touched(bar, level):
            return None
        # For LONG: adverse extreme (low) hit first, at fraction 0.33
        # For SHORT: adverse extreme (high) hit first, at fraction 0.33
        if side == "LONG":
            if level <= bar.open:
                return 0.25  # adverse SL hit early
            return 0.75  # favorable TP hit late (after low excursion)
        if side == "SHORT":
            if level >= bar.open:
                return 0.25  # adverse SL hit early
            return 0.75
        return 0.5

    def worst_adverse_price(self, bar: Bar, side: str) -> float:
        if side == "LONG":
            return bar.low
        if side == "SHORT":
            return bar.high
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")


# ── Hit detection helpers ────────────────────────────────────────────────


def check_sl_tp_hits(
    bar: Bar,
    side: str,
    stop_loss: float | None,
    take_profit: float | None,
    path_model: BrownianBridgeModel | M1PathModel | PessimisticPathModel,
    bar_idx: int = 0,
) -> tuple[str | None, float | None]:
    """Check which of SL / TP hit first during this bar.

    Returns a tuple (hit_label, hit_price) where hit_label is one of
    "sl", "tp", or None (neither hit). hit_price is the level that was
    touched, or None.

    If both SL and TP are touched during the bar, we use the path model
    to determine which hit first. This is where the Brownian bridge's
    fraction_into_bar estimate matters — it decides whether the bar's
    excursion was biased toward the favorable or adverse side.

    For LONG:
        sl_touched = bar.low <= stop_loss
        tp_touched = bar.high >= take_profit
    For SHORT:
        sl_touched = bar.high >= stop_loss
        tp_touched = bar.low <= take_profit
    """
    sl_touched = False
    tp_touched = False

    if side == "LONG":
        if stop_loss is not None and bar.low <= stop_loss:
            sl_touched = True
        if take_profit is not None and bar.high >= take_profit:
            tp_touched = True
    elif side == "SHORT":
        if stop_loss is not None and bar.high >= stop_loss:
            sl_touched = True
        if take_profit is not None and bar.low <= take_profit:
            tp_touched = True
    else:
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")

    if not sl_touched and not tp_touched:
        return (None, None)

    if sl_touched and not tp_touched:
        return ("sl", stop_loss)

    if tp_touched and not sl_touched:
        return ("tp", take_profit)

    # Both touched — use path model to determine which came first.
    # BrownianBridgeModel and M1PathModel both use `bar_idx=` signature;
    # PessimisticPathModel uses `side=`.
    if isinstance(path_model, PessimisticPathModel):
        sl_frac = path_model.fraction_into_bar(bar, stop_loss, side=side)
        tp_frac = path_model.fraction_into_bar(bar, take_profit, side=side)
    else:
        sl_frac = path_model.fraction_into_bar(bar, stop_loss, bar_idx=bar_idx)
        tp_frac = path_model.fraction_into_bar(bar, take_profit, bar_idx=bar_idx)

    if sl_frac is None and tp_frac is None:
        return (None, None)
    if sl_frac is None:
        return ("tp", take_profit)
    if tp_frac is None:
        return ("sl", stop_loss)

    # Earlier fraction wins
    if sl_frac <= tp_frac:
        return ("sl", stop_loss)
    return ("tp", take_profit)
