"""Phase B — Scenario detector tests.

Covers:
- News-window detection (±15min tolerance)
- Illiquid-hour detection per instrument_class
- Volatile-regime detection via ATR ratio
- Precedence: news > volatile > illiquid > normal
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtest.costs import NewsWindow
from src.fees.scenario import (
    ScenarioContext,
    detect,
    detect_simple,
    reset_news_cache,
)


def _iso(s: str) -> int:
    """Convert an ISO-8601 UTC string to ms epoch."""
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _reset():
    reset_news_cache()
    yield
    reset_news_cache()


class TestIlliquidHours:
    def test_xauusd_asian_session_is_illiquid(self):
        # 03:00 UTC — middle of Asian session
        ts = _iso("2026-04-17T03:00:00Z")
        assert detect_simple(ts, "xauusd_metals") == "illiquid"

    def test_xauusd_london_ny_overlap_is_normal(self):
        # 14:00 UTC — peak London/NY overlap
        ts = _iso("2026-04-17T14:00:00Z")
        assert detect_simple(ts, "xauusd_metals") == "normal"

    def test_xauusd_post_ny_is_illiquid(self):
        # 22:00 UTC — post-NY, pre-Asian
        ts = _iso("2026-04-17T22:00:00Z")
        assert detect_simple(ts, "xauusd_metals") == "illiquid"

    def test_fx_majors_have_different_illiquid_window(self):
        # 10:00 UTC — FX majors liquid (London morning)
        ts = _iso("2026-04-17T10:00:00Z")
        assert detect_simple(ts, "fx_majors") == "normal"

    def test_any_instrument_class_has_no_session_rules(self):
        # 'any' (pine research profile) never hits illiquid by time
        ts = _iso("2026-04-17T03:00:00Z")
        assert detect_simple(ts, "any") == "normal"


class TestVolatileRegime:
    def test_volatile_fires_when_atr_ratio_exceeds_1_5(self):
        ts = _iso("2026-04-17T14:00:00Z")  # otherwise normal (overlap)
        assert detect_simple(
            ts, "xauusd_metals", bar_atr=15.0, rolling_atr=5.0  # ratio 3.0
        ) == "volatile"

    def test_not_volatile_when_at_threshold(self):
        ts = _iso("2026-04-17T14:00:00Z")
        # Ratio exactly 1.5 → NOT volatile (strict >)
        assert detect_simple(
            ts, "xauusd_metals", bar_atr=7.5, rolling_atr=5.0
        ) == "normal"

    def test_not_volatile_when_below_threshold(self):
        ts = _iso("2026-04-17T14:00:00Z")
        assert detect_simple(
            ts, "xauusd_metals", bar_atr=5.0, rolling_atr=5.0
        ) == "normal"

    def test_volatile_overrides_illiquid(self):
        # 03:00 UTC would be illiquid, but ATR ratio 3.0 → volatile
        ts = _iso("2026-04-17T03:00:00Z")
        assert detect_simple(
            ts, "xauusd_metals", bar_atr=15.0, rolling_atr=5.0
        ) == "volatile"

    def test_missing_atr_inputs_skip_volatile_check(self):
        ts = _iso("2026-04-17T14:00:00Z")
        assert detect_simple(ts, "xauusd_metals") == "normal"
        assert detect_simple(ts, "xauusd_metals", bar_atr=None, rolling_atr=5.0) == "normal"
        assert detect_simple(ts, "xauusd_metals", bar_atr=15.0, rolling_atr=None) == "normal"
        # Zero rolling baseline → skip (not divide-by-zero)
        assert detect_simple(ts, "xauusd_metals", bar_atr=15.0, rolling_atr=0.0) == "normal"


class TestNewsActive:
    def test_news_window_fires_exact(self):
        # NFP 2025-01-10 runs 13:25-13:45Z per news_calendar.csv
        ts = _iso("2025-01-10T13:35:00Z")
        assert detect_simple(ts, "xauusd_metals") == "news_active"

    def test_news_window_fires_within_15min_buffer_before(self):
        # 15 min before NFP start → still news_active (tolerance)
        ts = _iso("2025-01-10T13:15:00Z")
        assert detect_simple(ts, "xauusd_metals") == "news_active"

    def test_news_window_fires_within_15min_buffer_after(self):
        # 15 min after NFP end → still news_active
        ts = _iso("2025-01-10T13:55:00Z")
        assert detect_simple(ts, "xauusd_metals") == "news_active"

    def test_news_window_does_not_fire_outside_buffer(self):
        # 20 min before NFP start → NOT news_active (buffer is 15 min)
        ts = _iso("2025-01-10T13:05:00Z")
        assert detect_simple(ts, "xauusd_metals") in ("normal", "illiquid")

    def test_news_overrides_illiquid(self):
        # If NFP were at 03:00 UTC (it isn't in real life but test contract):
        fake_news = (NewsWindow(
            label="fake",
            start_ms=_iso("2026-04-17T03:00:00Z"),
            end_ms=_iso("2026-04-17T03:20:00Z"),
        ),)
        ts = _iso("2026-04-17T03:10:00Z")
        ctx = ScenarioContext(
            timestamp_ms=ts, instrument_class="xauusd_metals", news_windows=fake_news
        )
        assert detect(ctx) == "news_active"

    def test_news_overrides_volatile(self):
        # At news time, even if volatile would fire, news wins
        fake_news = (NewsWindow(
            label="fake",
            start_ms=_iso("2026-04-17T14:00:00Z"),
            end_ms=_iso("2026-04-17T14:20:00Z"),
        ),)
        ts = _iso("2026-04-17T14:10:00Z")
        ctx = ScenarioContext(
            timestamp_ms=ts,
            instrument_class="xauusd_metals",
            bar_atr=100.0,
            rolling_atr=5.0,
            news_windows=fake_news,
        )
        assert detect(ctx) == "news_active"


class TestEmptyNewsCalendar:
    def test_no_news_windows_falls_to_session_logic(self):
        # Explicitly empty news → never news_active
        ts = _iso("2025-01-10T13:35:00Z")  # NFP time, but we pass empty
        ctx = ScenarioContext(
            timestamp_ms=ts, instrument_class="xauusd_metals", news_windows=()
        )
        assert detect(ctx) == "normal"  # 13:35 UTC is overlap, so normal


class TestPrecedence:
    def test_precedence_order(self):
        """news > volatile > illiquid > normal."""
        overlap_ts = _iso("2026-04-17T14:00:00Z")
        asian_ts = _iso("2026-04-17T03:00:00Z")

        # Normal
        assert detect_simple(overlap_ts, "xauusd_metals") == "normal"
        # Illiquid
        assert detect_simple(asian_ts, "xauusd_metals") == "illiquid"
        # Volatile overrides illiquid
        assert detect_simple(asian_ts, "xauusd_metals", bar_atr=15.0, rolling_atr=5.0) == "volatile"
        # News overrides volatile (tested in TestNewsActive.test_news_overrides_volatile)
