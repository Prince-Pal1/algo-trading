"""Scenario auto-detection for the FeeManager.

Given a (timestamp, symbol, market context) tuple, pick one of:
    normal       — calm liquid session, no news
    news_active  — within ±15min of a high-impact calendar event
    illiquid     — thin-liquidity window (Asian session for gold/EU FX,
                   post-NY gap, weekend approach)
    volatile     — current ATR > 1.5× rolling 30-bar ATR

Precedence (highest first):
    news_active > volatile > illiquid > normal

Session rules are per-instrument-class since liquidity schedules vary:
    - xauusd_metals: peak 13:00-16:00 UTC; illiquid 00:00-07:00, 21:00-24:00.
    - fx_majors:     peak 07:00-16:00 UTC; illiquid 21:00-07:00 (narrow Asian
                     for EUR/GBP, wider for USD/JPY but still thinner than London).
    - any:           no session rules (never illiquid by time-of-day alone).

The detector is stateless and deterministic — pure function of its inputs.
The FeeManager caches the news calendar load but otherwise calls detect()
fresh for each lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.backtest.costs import NewsWindow, load_news_calendar_csv


ScenarioName = Literal["normal", "news_active", "illiquid", "volatile"]


# UTC hour ranges (half-open: [start, end)) where liquidity is NOT peak.
# Outside these windows, the default is `normal`.
_ILLIQUID_HOURS_UTC: dict[str, list[tuple[int, int]]] = {
    "xauusd_metals": [(0, 7), (21, 24)],   # Asian session + post-NY gap
    "fx_majors":     [(21, 24), (0, 7)],   # post-NY + Asian
    # 'any' has no session rules (research profile)
}

# Default news-window tolerance — signals within ±this many seconds of a
# news event are treated as news_active.
_NEWS_TOLERANCE_S = 15 * 60  # 15 minutes

# Volatile threshold — current bar ATR / rolling 30-bar ATR
_VOLATILE_ATR_RATIO = 1.5


@dataclass(frozen=True)
class ScenarioContext:
    """Inputs needed to classify a scenario.

    timestamp_ms:    required; used for session + news lookup
    instrument_class: required; used for session rules
    bar_atr:         optional; if None, skips volatile detection
    rolling_atr:     optional; if None, skips volatile detection
    news_windows:    optional; if None, uses the default news calendar
    """
    timestamp_ms: int
    instrument_class: str
    bar_atr: float | None = None
    rolling_atr: float | None = None
    news_windows: tuple[NewsWindow, ...] | None = None


def _is_within_news_window(
    ts_ms: int, windows: tuple[NewsWindow, ...], tolerance_s: int
) -> bool:
    """True if timestamp falls within [start - tolerance, end + tolerance] for any window.

    NewsWindow stores start_ms/end_ms in unix milliseconds (see
    src/backtest/costs.py), so we convert the tolerance to ms before comparing.
    """
    tol_ms = tolerance_s * 1000
    for w in windows:
        if (w.start_ms - tol_ms) <= ts_ms <= (w.end_ms + tol_ms):
            return True
    return False


def _is_illiquid_hour(ts_ms: int, instrument_class: str) -> bool:
    windows = _ILLIQUID_HOURS_UTC.get(instrument_class)
    if not windows:
        return False
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    h = dt.hour
    for start, end in windows:
        if start <= h < end:
            return True
    return False


def _is_volatile(bar_atr: float | None, rolling_atr: float | None) -> bool:
    if bar_atr is None or rolling_atr is None or rolling_atr <= 0:
        return False
    return (bar_atr / rolling_atr) > _VOLATILE_ATR_RATIO


# Module-level cache for the news calendar to avoid repeated disk reads.
_NEWS_CACHE: tuple[NewsWindow, ...] | None = None


def _default_news_windows() -> tuple[NewsWindow, ...]:
    global _NEWS_CACHE
    if _NEWS_CACHE is None:
        repo_root = Path(__file__).resolve().parents[2]
        csv_path = repo_root / "config" / "news_calendar.csv"
        if csv_path.exists():
            _NEWS_CACHE = tuple(load_news_calendar_csv(str(csv_path)))
        else:
            _NEWS_CACHE = ()
    return _NEWS_CACHE


def reset_news_cache() -> None:
    """Test helper — force the next call to re-read news_calendar.csv."""
    global _NEWS_CACHE
    _NEWS_CACHE = None


def detect(ctx: ScenarioContext) -> ScenarioName:
    """Classify the scenario for a (timestamp, instrument_class, [atr]) context.

    Precedence: news_active > volatile > illiquid > normal.
    """
    windows = ctx.news_windows if ctx.news_windows is not None else _default_news_windows()
    if windows and _is_within_news_window(ctx.timestamp_ms, windows, _NEWS_TOLERANCE_S):
        return "news_active"
    if _is_volatile(ctx.bar_atr, ctx.rolling_atr):
        return "volatile"
    if _is_illiquid_hour(ctx.timestamp_ms, ctx.instrument_class):
        return "illiquid"
    return "normal"


def detect_simple(
    timestamp_ms: int,
    instrument_class: str,
    *,
    bar_atr: float | None = None,
    rolling_atr: float | None = None,
) -> ScenarioName:
    """Convenience wrapper — build ScenarioContext from kwargs."""
    return detect(
        ScenarioContext(
            timestamp_ms=timestamp_ms,
            instrument_class=instrument_class,
            bar_atr=bar_atr,
            rolling_atr=rolling_atr,
        )
    )
