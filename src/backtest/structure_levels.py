"""Price-structure level detection for gold strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd


@dataclass(frozen=True)
class StructureLevel:
    price: float
    kind: str           # "pdh", "pdl", "session_open_high", "session_open_low",
                        # "round", "swing_high", "swing_low", "fib_50", "fib_618", "fib_786"
    ts_ms: int = 0


def prior_day_high_low(df: pd.DataFrame, now_ts_ms: int) -> tuple[float, float] | None:
    """Return (prior_day_high, prior_day_low) relative to `now_ts_ms`.

    Expects df sorted ascending with a `timestamp` column (unix ms) and OHLC.
    Returns None if no prior day exists.
    """
    if df.empty or "timestamp" not in df.columns:
        return None
    now_day = datetime.fromtimestamp(now_ts_ms / 1000, tz=timezone.utc).date()
    ts_series = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    days = ts_series.dt.date
    mask_prior = days < now_day
    if not mask_prior.any():
        return None
    last_prior_day = days[mask_prior].max()
    prior_rows = df[days == last_prior_day]
    if prior_rows.empty:
        return None
    return (float(prior_rows["high"].max()), float(prior_rows["low"].min()))


def session_open_range(
    df: pd.DataFrame,
    now_ts_ms: int,
    session_start_hour: int,
    session_end_hour: int,
    range_minutes: int = 30,
) -> tuple[float, float] | None:
    """Opening-range high/low for the current day's session.

    E.g., NY ORB with session_start_hour=13, session_end_hour=16, range_minutes=15
    computes the H/L of bars in the [13:30, 13:45] window on today's date.
    """
    if df.empty:
        return None
    now_dt = datetime.fromtimestamp(now_ts_ms / 1000, tz=timezone.utc)
    day = now_dt.date()
    ts_series = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    same_day = df[ts_series.dt.date == day]
    if same_day.empty:
        return None
    ts_same = pd.to_datetime(same_day["timestamp"], unit="ms", utc=True)
    start_minute = session_start_hour * 60
    end_minute = start_minute + range_minutes
    rows_in_range = same_day[
        (ts_same.dt.hour * 60 + ts_same.dt.minute >= start_minute)
        & (ts_same.dt.hour * 60 + ts_same.dt.minute < end_minute)
    ]
    if rows_in_range.empty:
        return None
    return (float(rows_in_range["high"].max()), float(rows_in_range["low"].min()))


def round_number_levels(price: float, step: float = 50.0, count: int = 4) -> list[float]:
    """Return the `count` nearest round-number levels to `price`.

    step=50 gives $2400, $2450, $2500, ... for gold. count is split half above
    and half below.
    """
    if step <= 0 or count <= 0:
        return []
    nearest = round(price / step) * step
    half = count // 2
    levels = sorted({nearest + (i - half) * step for i in range(count + 1)})
    return levels


def swing_high_low(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float] | None:
    """Highest high and lowest low over the last `lookback` bars."""
    if df.empty or len(df) < lookback:
        return None
    tail = df.iloc[-lookback:]
    return (float(tail["high"].max()), float(tail["low"].min()))


def fib_retracements(swing_low: float, swing_high: float) -> dict[str, float]:
    """Classic Fibonacci retracements between a low and high."""
    if swing_high <= swing_low:
        return {}
    rng = swing_high - swing_low
    return {
        "fib_50": swing_low + 0.5 * rng,
        "fib_618": swing_low + 0.618 * rng,
        "fib_786": swing_low + 0.786 * rng,
    }


def collect_levels(
    df: pd.DataFrame,
    now_ts_ms: int,
    *,
    include_pdh_pdl: bool = True,
    include_session_ranges: bool = True,
    include_round_numbers: bool = True,
    round_step: float = 50.0,
    round_count: int = 4,
    include_swing: bool = True,
    swing_lookback: int = 20,
    include_fib: bool = True,
) -> list[StructureLevel]:
    """Aggregate all enabled structure levels into a flat list."""
    out: list[StructureLevel] = []

    if include_pdh_pdl:
        pdh_pdl = prior_day_high_low(df, now_ts_ms)
        if pdh_pdl is not None:
            pdh, pdl = pdh_pdl
            out.append(StructureLevel(price=pdh, kind="pdh", ts_ms=now_ts_ms))
            out.append(StructureLevel(price=pdl, kind="pdl", ts_ms=now_ts_ms))

    if include_session_ranges:
        london = session_open_range(df, now_ts_ms, session_start_hour=7, session_end_hour=11, range_minutes=30)
        if london is not None:
            out.append(StructureLevel(price=london[0], kind="session_open_high", ts_ms=now_ts_ms))
            out.append(StructureLevel(price=london[1], kind="session_open_low", ts_ms=now_ts_ms))
        ny = session_open_range(df, now_ts_ms, session_start_hour=13, session_end_hour=16, range_minutes=30)
        if ny is not None:
            out.append(StructureLevel(price=ny[0], kind="session_open_high", ts_ms=now_ts_ms))
            out.append(StructureLevel(price=ny[1], kind="session_open_low", ts_ms=now_ts_ms))

    if include_round_numbers and not df.empty:
        current = float(df.iloc[-1]["close"])
        for level in round_number_levels(current, step=round_step, count=round_count):
            out.append(StructureLevel(price=level, kind="round", ts_ms=now_ts_ms))

    if include_swing:
        swing = swing_high_low(df, lookback=swing_lookback)
        if swing is not None:
            out.append(StructureLevel(price=swing[0], kind="swing_high", ts_ms=now_ts_ms))
            out.append(StructureLevel(price=swing[1], kind="swing_low", ts_ms=now_ts_ms))
            if include_fib:
                for name, price in fib_retracements(swing[1], swing[0]).items():
                    out.append(StructureLevel(price=price, kind=name, ts_ms=now_ts_ms))

    return out


def nearest_level_above(levels: list[StructureLevel], price: float) -> StructureLevel | None:
    above = [lev for lev in levels if lev.price > price]
    if not above:
        return None
    return min(above, key=lambda lev: lev.price - price)


def nearest_level_below(levels: list[StructureLevel], price: float) -> StructureLevel | None:
    below = [lev for lev in levels if lev.price < price]
    if not below:
        return None
    return min(below, key=lambda lev: price - lev.price)
