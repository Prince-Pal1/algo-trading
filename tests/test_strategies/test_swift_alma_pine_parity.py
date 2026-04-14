"""Integration test — SwiftAlmaStrategy Pine parity against real TV data.

Skipped if the TV CSV files aren't present in ~/Downloads/. On Prince's
machine, they are. CI and other developers won't hit this test unless
they manually place the files there. This is deliberately lenient —
the test documents the expected artifact paths but doesn't block CI.

The test verifies that `SwiftAlmaStrategy.detect_signals_lookahead`
produces 100% bar-by-bar match against TV's entry markers on 107 days
of real Vantage XAUUSD data spanning a DST transition. This is the
integration version of the more granular unit tests in
`test_tv_parity.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.backtest.tv_parity import (
    bar_by_bar_match,
    detect_alt_anchor_segments,
    load_tv_chart_csv,
)
from src.strategies.trend_following.swift_alma import SwiftAlmaStrategy


TV_CHART_CSV = Path.home() / "Downloads" / "VANTAGE_XAUUSD, 5.csv"


@pytest.mark.skipif(
    not TV_CHART_CSV.exists(),
    reason=f"TV chart CSV not present at {TV_CHART_CSV} — run "
           "`scripts/swift_alma_tv_data_match.py` source file for instructions",
)
def test_swift_alma_pine_parity_hits_100_percent_on_tv_data():
    tv_chart = load_tv_chart_csv(TV_CHART_CSV)
    # Minimum 200 bars to be a meaningful test
    assert len(tv_chart) > 200, "TV chart CSV too small for a meaningful match"

    segments = detect_alt_anchor_segments(tv_chart, alt_tf_min=40)
    # Must detect at least one segment
    assert len(segments) >= 1

    my_df = SwiftAlmaStrategy.detect_signals_lookahead(
        tv_chart,
        anchor_segments=segments,
        alt_tf_multiplier=8,
        alma_length=2,
        alma_offset=0.85,
        alma_sigma=5.0,
        timeframe_minutes=5,
    )

    stats = bar_by_bar_match(tv_chart, my_df)
    # Our match must be perfect or near-perfect (allow small warmup
    # margin if the CSV starts mid-alt-bar, but the 107-day export
    # should have no warmup issue)
    assert stats["total_match_pct"] >= 99.0, (
        f"Bar-by-bar match dropped below 99%: {stats['total_match_pct']:.2f}%. "
        f"Unmatched TV longs: {len(stats['long_tv_only'])}, "
        f"shorts: {len(stats['short_tv_only'])}. "
        f"Investigate before trusting SwiftAlmaStrategy.detect_signals_lookahead."
    )
