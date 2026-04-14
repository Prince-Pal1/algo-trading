#!/usr/bin/env python3
"""Stage 1 alpha research: news_spike_fade on XAUUSD.

Looks at what XAUUSD actually does post-news (NFP/FOMC/CPI/ECB) using M1
data inside each news window. Characterizes:
- How often a 20-min window contains a spike > N pips
- How often the spike reverses within 5/10/15/30 minutes
- Directional bias (up-spike vs down-spike reversal rates)
- Optimal fade target distance (how far does the reversal typically go?)

Output: stage1_news_spike_stats.json + console report. Use this to
design a signal that matches the data, not wishful thinking.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from src.backtest.costs import load_news_calendar_csv


M1_PATH = "data/historical/XAUUSD_1m.parquet"
NEWS_CALENDAR_PATH = "config/news_calendar.csv"
OUTPUT_JSON = "data/research/news_spike_stats.json"
PIP_SIZE = 0.10
WINDOW_PADDING_S = 30 * 60  # look 30 min past window end for reversal


@dataclass
class SpikeEvent:
    label: str
    window_start_ms: int
    window_end_ms: int
    pre_window_price: float
    spike_direction: int  # +1 up, -1 down
    spike_pips: float
    spike_at_minute: int  # minutes from window start
    reversal_pips_5m: float
    reversal_pips_10m: float
    reversal_pips_15m: float
    reversal_pips_30m: float
    max_continuation_pips: float


@dataclass
class StageOneReport:
    events_examined: int
    events_with_spike_30pip: int
    events_with_spike_50pip: int
    events_with_spike_100pip: int
    spike_direction_up_pct: float
    spike_direction_down_pct: float
    reversal_rate_5m_50pct: float  # % of spikes where price retraced >= 50% within 5min
    reversal_rate_10m_50pct: float
    reversal_rate_30m_50pct: float
    mean_max_continuation_after_spike_pips: float
    mean_reversal_5m_pips: float
    mean_reversal_10m_pips: float
    mean_reversal_30m_pips: float
    recommendation: str


def _find_pre_window_price(m1: pd.DataFrame, window_start_ms: int) -> float | None:
    """Price at the last M1 bar BEFORE the news window starts."""
    prior = m1[m1["timestamp"] < window_start_ms]
    if prior.empty:
        return None
    return float(prior.iloc[-1]["close"])


def _find_spike_in_window(
    m1: pd.DataFrame,
    window_start_ms: int,
    window_end_ms: int,
    pre_price: float,
) -> tuple[int, float, int] | None:
    """Return (direction, max_spike_pips, minutes_from_start) for the biggest
    excursion from pre_price inside the window. None if no M1 data in window.
    """
    inside = m1[(m1["timestamp"] >= window_start_ms) & (m1["timestamp"] <= window_end_ms)]
    if inside.empty:
        return None

    max_up_price = inside["high"].max()
    min_down_price = inside["low"].min()
    up_excursion = (max_up_price - pre_price) / PIP_SIZE
    down_excursion = (pre_price - min_down_price) / PIP_SIZE

    if up_excursion >= down_excursion:
        # Up spike
        direction = 1
        spike_pips = up_excursion
        spike_row_idx = inside["high"].idxmax()
    else:
        direction = -1
        spike_pips = down_excursion
        spike_row_idx = inside["low"].idxmin()

    spike_ts = int(inside.loc[spike_row_idx]["timestamp"])
    minutes = (spike_ts - window_start_ms) // 60_000
    return (direction, spike_pips, int(minutes))


def _measure_reversal(
    m1: pd.DataFrame,
    spike_ts: int,
    spike_price: float,
    direction: int,
    minutes_ahead: int,
) -> float:
    """How many pips did price move AGAINST the spike within `minutes_ahead`
    after the spike peak? Returns absolute pip count (non-negative).
    """
    end_ts = spike_ts + minutes_ahead * 60_000
    window = m1[(m1["timestamp"] > spike_ts) & (m1["timestamp"] <= end_ts)]
    if window.empty:
        return 0.0
    if direction > 0:
        # spike was up, reversal = lowest low below spike_price
        reversal_extreme = window["low"].min()
        pips = (spike_price - reversal_extreme) / PIP_SIZE
    else:
        reversal_extreme = window["high"].max()
        pips = (reversal_extreme - spike_price) / PIP_SIZE
    return max(0.0, float(pips))


def _measure_max_continuation(
    m1: pd.DataFrame,
    spike_ts: int,
    spike_price: float,
    direction: int,
    minutes_ahead: int = 30,
) -> float:
    """How many pips did price continue IN the spike direction after the spike peak?"""
    end_ts = spike_ts + minutes_ahead * 60_000
    window = m1[(m1["timestamp"] > spike_ts) & (m1["timestamp"] <= end_ts)]
    if window.empty:
        return 0.0
    if direction > 0:
        extreme = window["high"].max()
        pips = (extreme - spike_price) / PIP_SIZE
    else:
        extreme = window["low"].min()
        pips = (spike_price - extreme) / PIP_SIZE
    return max(0.0, float(pips))


def main() -> None:
    print("Stage 1 research — news_spike_fade")
    print("=" * 60)

    m1 = pd.read_parquet(M1_PATH).sort_values("timestamp").reset_index(drop=True)
    print(f"Loaded {len(m1):,} M1 bars")

    windows = load_news_calendar_csv(NEWS_CALENDAR_PATH)
    print(f"Loaded {len(windows)} news windows")

    events: list[SpikeEvent] = []
    for w in windows:
        pre_price = _find_pre_window_price(m1, w.start_ms)
        if pre_price is None:
            continue
        spike = _find_spike_in_window(m1, w.start_ms, w.end_ms, pre_price)
        if spike is None:
            continue
        direction, spike_pips, minutes = spike
        if spike_pips < 10.0:
            # Not even a minor excursion — skip
            continue

        spike_ts = w.start_ms + minutes * 60_000
        spike_price = pre_price + direction * spike_pips * PIP_SIZE

        events.append(SpikeEvent(
            label=w.label,
            window_start_ms=w.start_ms,
            window_end_ms=w.end_ms,
            pre_window_price=pre_price,
            spike_direction=direction,
            spike_pips=spike_pips,
            spike_at_minute=minutes,
            reversal_pips_5m=_measure_reversal(m1, spike_ts, spike_price, direction, 5),
            reversal_pips_10m=_measure_reversal(m1, spike_ts, spike_price, direction, 10),
            reversal_pips_15m=_measure_reversal(m1, spike_ts, spike_price, direction, 15),
            reversal_pips_30m=_measure_reversal(m1, spike_ts, spike_price, direction, 30),
            max_continuation_pips=_measure_max_continuation(
                m1, spike_ts, spike_price, direction, 30,
            ),
        ))

    if not events:
        print("No events found!")
        return

    n = len(events)
    spikes_30 = sum(1 for e in events if e.spike_pips >= 30)
    spikes_50 = sum(1 for e in events if e.spike_pips >= 50)
    spikes_100 = sum(1 for e in events if e.spike_pips >= 100)
    up_n = sum(1 for e in events if e.spike_direction > 0)
    down_n = n - up_n

    def _retrace_rate(events, minutes_key: str, pct: float) -> float:
        eligible = [e for e in events if e.spike_pips >= 30]
        if not eligible:
            return 0.0
        ok = 0
        for e in eligible:
            retraced = getattr(e, f"reversal_pips_{minutes_key}")
            if retraced >= e.spike_pips * pct:
                ok += 1
        return ok / len(eligible) * 100.0

    report = StageOneReport(
        events_examined=n,
        events_with_spike_30pip=spikes_30,
        events_with_spike_50pip=spikes_50,
        events_with_spike_100pip=spikes_100,
        spike_direction_up_pct=up_n / n * 100.0,
        spike_direction_down_pct=down_n / n * 100.0,
        reversal_rate_5m_50pct=_retrace_rate(events, "5m", 0.50),
        reversal_rate_10m_50pct=_retrace_rate(events, "10m", 0.50),
        reversal_rate_30m_50pct=_retrace_rate(events, "30m", 0.50),
        mean_max_continuation_after_spike_pips=(
            sum(e.max_continuation_pips for e in events) / n
        ),
        mean_reversal_5m_pips=sum(e.reversal_pips_5m for e in events) / n,
        mean_reversal_10m_pips=sum(e.reversal_pips_10m for e in events) / n,
        mean_reversal_30m_pips=sum(e.reversal_pips_30m for e in events) / n,
        recommendation="",
    )

    print()
    print(f"Events examined:       {report.events_examined}")
    print(f"  spike >= 30 pips:    {report.events_with_spike_30pip} ({report.events_with_spike_30pip/n*100:.1f}%)")
    print(f"  spike >= 50 pips:    {report.events_with_spike_50pip} ({report.events_with_spike_50pip/n*100:.1f}%)")
    print(f"  spike >= 100 pips:   {report.events_with_spike_100pip} ({report.events_with_spike_100pip/n*100:.1f}%)")
    print()
    print(f"Spike direction balance:")
    print(f"  up:   {report.spike_direction_up_pct:.1f}%")
    print(f"  down: {report.spike_direction_down_pct:.1f}%")
    print()
    print(f"Retracement rates (spikes >= 30 pips, retrace >= 50% of spike):")
    print(f"  within 5 min:  {report.reversal_rate_5m_50pct:.1f}%")
    print(f"  within 10 min: {report.reversal_rate_10m_50pct:.1f}%")
    print(f"  within 30 min: {report.reversal_rate_30m_50pct:.1f}%")
    print()
    print(f"Mean retracement (absolute pips):")
    print(f"  5 min:  {report.mean_reversal_5m_pips:.2f}")
    print(f"  10 min: {report.mean_reversal_10m_pips:.2f}")
    print(f"  30 min: {report.mean_reversal_30m_pips:.2f}")
    print(f"Mean continuation after spike (within 30 min): {report.mean_max_continuation_after_spike_pips:.2f}")
    print()

    # Recommendation logic
    if report.reversal_rate_30m_50pct > 55.0:
        rec = f"FADE VIABLE — {report.reversal_rate_30m_50pct:.0f}% of >=30pip spikes retrace >= 50% within 30min. "
        rec += f"Target 0.5 × spike, SL = 1.2 × spike. "
        rec += f"Expected trade frequency: {spikes_30} events over ~2 years."
    elif report.reversal_rate_30m_50pct > 45.0:
        rec = f"FADE MARGINAL — {report.reversal_rate_30m_50pct:.0f}% retracement rate, near coin flip. "
        rec += "Needs additional filter (direction bias, volatility regime) to have edge."
    else:
        rec = f"FADE NOT VIABLE — only {report.reversal_rate_30m_50pct:.0f}% retracement. "
        rec += "Spikes continue more often than reverse. Strategy premise is wrong. KILL."
    report.recommendation = rec
    print(f"Recommendation: {rec}")

    # Write JSON
    Path(OUTPUT_JSON).parent.mkdir(parents=True, exist_ok=True)
    out = {
        "stage1_report": asdict(report),
        "events": [asdict(e) for e in events],
    }
    Path(OUTPUT_JSON).write_text(json.dumps(out, indent=2))
    print(f"\n✓ Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
