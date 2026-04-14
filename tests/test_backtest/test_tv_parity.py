"""Unit tests for the TV parity validation framework."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.tv_parity import (
    anchor_for,
    bar_by_bar_match,
    detect_alt_anchor_segments,
    format_parity_report,
    load_tv_chart_csv,
    load_tv_trades_csv,
    simulate_reversal_strategy,
    trade_by_trade_match,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _write_tv_chart_csv(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a minimal TV chart export CSV at the given path."""
    p = tmp_path / "chart.csv"
    columns = ["time", "open", "high", "low", "close", "volume", "Long", "Short"]
    with open(p, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return p


def _write_tv_trades_csv(tmp_path: Path, trades: list[dict]) -> Path:
    """Write a minimal TV trades export (2 rows per trade: Entry + Exit)."""
    p = tmp_path / "trades.csv"
    columns = [
        "Trade #", "Type", "Date and time", "Signal", "Price USD",
        "Size (qty)", "Size (value)", "Net P&L USD", "Net P&L %",
    ]
    with open(p, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for t in trades:
            # Exit row comes first in TV exports
            writer.writerow({
                "Trade #": t["trade_num"],
                "Type": f"Exit {t['side']}",
                "Date and time": t["exit_dt"],
                "Price USD": t["exit_price"],
                "Net P&L USD": t["pnl_usd"],
                "Net P&L %": t["pnl_pct"],
                "Signal": "",
                "Size (qty)": 1,
                "Size (value)": 1000,
            })
            writer.writerow({
                "Trade #": t["trade_num"],
                "Type": f"Entry {t['side']}",
                "Date and time": t["entry_dt"],
                "Price USD": t["entry_price"],
                "Net P&L USD": t["pnl_usd"],
                "Net P&L %": t["pnl_pct"],
                "Signal": "",
                "Size (qty)": 1,
                "Size (value)": 1000,
            })
    return p


# ── load_tv_chart_csv ───────────────────────────────────────────────────


class TestLoadTvChartCsv:
    def test_loads_ohlc_and_adds_helper_columns(self, tmp_path):
        rows = [
            {"time": 1000, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10, "Long": "", "Short": ""},
            {"time": 1300, "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 15, "Long": 1, "Short": ""},
            {"time": 1600, "open": 101.5, "high": 102, "low": 101, "close": 101.8, "volume": 12, "Long": "", "Short": 1},
        ]
        p = _write_tv_chart_csv(tmp_path, rows)
        df = load_tv_chart_csv(p)
        assert len(df) == 3
        assert list(df["timestamp"]) == [1_000_000, 1_300_000, 1_600_000]
        assert df["tv_long_entry"].tolist() == [False, True, False]
        assert df["tv_short_entry"].tolist() == [False, False, True]

    def test_missing_time_column_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("foo,bar\n1,2\n")
        with pytest.raises(ValueError, match="missing 'time' column"):
            load_tv_chart_csv(p)


# ── load_tv_trades_csv ──────────────────────────────────────────────────


class TestLoadTvTradesCsv:
    def test_collapses_entry_exit_pairs(self, tmp_path):
        trades = [
            {"trade_num": 1, "side": "long",
             "entry_dt": "2025-04-14 04:50", "entry_price": 3219.0,
             "exit_dt": "2025-04-14 06:10", "exit_price": 3220.84,
             "pnl_usd": 57.04, "pnl_pct": 0.06},
            {"trade_num": 2, "side": "short",
             "entry_dt": "2025-04-14 06:10", "entry_price": 3220.84,
             "exit_dt": "2025-04-14 06:50", "exit_price": 3227.67,
             "pnl_usd": -211.73, "pnl_pct": -0.21},
        ]
        p = _write_tv_trades_csv(tmp_path, trades)
        parsed = load_tv_trades_csv(p)
        assert len(parsed) == 2
        assert parsed[0]["side"] == "long"
        assert parsed[0]["entry_price"] == 3219.0
        assert parsed[0]["pnl_usd"] == 57.04
        assert parsed[1]["side"] == "short"


# ── detect_alt_anchor_segments ──────────────────────────────────────────


class TestDetectAltAnchorSegments:
    def _df_with_entries(self, entry_timestamps_ms: list[int]) -> pd.DataFrame:
        """Build a DataFrame with just the columns the detector needs."""
        rows = []
        entry_set = set(entry_timestamps_ms)
        # Add some non-entry bars before + between + after entries
        all_ts = sorted(set(entry_timestamps_ms))
        for ts in all_ts:
            rows.append({
                "timestamp": ts,
                "tv_long_entry": True,
                "tv_short_entry": False,
            })
        df = pd.DataFrame(rows)
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def test_single_anchor_all_entries_at_mod_0(self):
        # All entries on 40-min boundaries from UTC 00:00
        # 2025-01-01 00:00, 00:40, 01:20 UTC
        base = int(pd.Timestamp("2025-01-01 00:00", tz="UTC").timestamp() * 1000)
        entries = [base, base + 40 * 60_000, base + 80 * 60_000]
        df = self._df_with_entries(entries)
        segs = detect_alt_anchor_segments(df, alt_tf_min=40)
        # All entries have mod 0, so single segment at anchor 0
        assert len(segs) == 1
        assert segs[0][1] == 0

    def test_dst_transition_produces_two_segments(self):
        # Day 1 entries at anchor +20 min (mod 20)
        # Day 2 entries at anchor 0 min (mod 0)
        base1 = int(pd.Timestamp("2025-01-01 00:20", tz="UTC").timestamp() * 1000)
        base2 = int(pd.Timestamp("2025-01-02 00:00", tz="UTC").timestamp() * 1000)
        entries = [
            base1, base1 + 40 * 60_000, base1 + 80 * 60_000,  # day 1
            base2, base2 + 40 * 60_000, base2 + 80 * 60_000,  # day 2
        ]
        df = self._df_with_entries(entries)
        segs = detect_alt_anchor_segments(df, alt_tf_min=40)
        assert len(segs) == 2
        # First segment should be +20 min, second should be 0 min
        anchors = [s[1] for s in segs]
        assert anchors == [20, 0]

    def test_empty_entries_returns_default(self):
        df = pd.DataFrame({"timestamp": [], "tv_long_entry": [], "tv_short_entry": [], "dt": []})
        segs = detect_alt_anchor_segments(df, alt_tf_min=40)
        assert segs == [(0, 0)]


# ── anchor_for ──────────────────────────────────────────────────────────


class TestAnchorFor:
    def test_step_function_lookup(self):
        segments = [(1000, 20), (5000, 0), (9000, 40)]
        assert anchor_for(500, segments) == 20   # before first segment → first anchor
        assert anchor_for(1000, segments) == 20  # exactly at segment start
        assert anchor_for(3000, segments) == 20  # inside first segment
        assert anchor_for(5000, segments) == 0   # exactly at second segment start
        assert anchor_for(7000, segments) == 0   # inside second segment
        assert anchor_for(9000, segments) == 40  # exactly at third
        assert anchor_for(99999, segments) == 40  # past all segments

    def test_empty_segments(self):
        assert anchor_for(1000, []) == 0


# ── simulate_reversal_strategy ──────────────────────────────────────────


class TestSimulateReversalStrategy:
    def test_basic_long_then_short_flip(self):
        df = pd.DataFrame({
            "timestamp": [1000, 2000, 3000, 4000],
            "close": [100.0, 100.0, 102.0, 101.0],
            "le_trigger": [True, False, False, False],
            "se_trigger": [False, False, True, False],
        })
        trades = simulate_reversal_strategy(
            df, initial_equity=1_000_000.0, position_pct=0.10,
        )
        # Open long at bar 1 (close 100), close long + open short at bar 3 (close 102)
        # One closed trade: long from 100 → 102, +2%
        assert len(trades) == 1
        assert trades[0]["side"] == "long"
        assert trades[0]["entry_price"] == 100.0
        assert trades[0]["exit_price"] == 102.0
        # PnL% = (102-100)/100 * 100 = 2%
        assert trades[0]["pnl_pct"] == pytest.approx(2.0)
        # PnL$ = 100000 (position) * 0.02 = 2000
        assert trades[0]["pnl_usd"] == pytest.approx(2000.0)

    def test_multiple_flips_accumulate_equity(self):
        df = pd.DataFrame({
            "timestamp": [1, 2, 3, 4, 5, 6, 7, 8],
            "close": [100.0, 100.0, 110.0, 105.0, 95.0, 100.0, 100.0, 100.0],
            "le_trigger": [True, False, False, False, True, False, False, False],
            "se_trigger": [False, False, True, False, False, False, True, False],
        })
        trades = simulate_reversal_strategy(
            df, initial_equity=1_000_000.0, position_pct=0.10,
        )
        # 1. long 100 → close at 110 via SE at bar 3, +10% → +$10k, equity = $1.01M
        # 2. short 110 → close at 95 via LE at bar 5, +13.64%
        #    notional = 1.01M × 0.10 = $101k; pnl = 101k × (110-95)/110 = $13,772
        # 3. long 95 → close at 100 via SE at bar 7, +5.26%
        assert len(trades) == 3
        assert trades[0]["side"] == "long"
        assert trades[0]["pnl_pct"] == pytest.approx(10.0)
        assert trades[1]["side"] == "short"
        # Short from 110 to 95 = +15/110 = 13.636%
        assert trades[1]["pnl_pct"] == pytest.approx((110 - 95) / 110 * 100)
        assert trades[2]["side"] == "long"

    def test_no_triggers_no_trades(self):
        df = pd.DataFrame({
            "timestamp": [1, 2, 3],
            "close": [100.0, 101.0, 102.0],
            "le_trigger": [False, False, False],
            "se_trigger": [False, False, False],
        })
        trades = simulate_reversal_strategy(df)
        assert trades == []


# ── bar_by_bar_match ────────────────────────────────────────────────────


class TestBarByBarMatch:
    def test_perfect_match(self):
        tv = pd.DataFrame({
            "timestamp": [1000, 2000, 3000, 4000],
            "tv_long_entry": [True, False, False, False],
            "tv_short_entry": [False, False, True, False],
        })
        mine = pd.DataFrame({
            "timestamp": [1000, 2000, 3000, 4000],
            "le_trigger": [True, False, False, False],
            "se_trigger": [False, False, True, False],
        })
        stats = bar_by_bar_match(tv, mine)
        assert stats["long_match"] == 1
        assert stats["short_match"] == 1
        assert stats["total_tv"] == 2
        assert stats["total_match"] == 2
        assert stats["total_match_pct"] == 100.0

    def test_missing_mine_signal(self):
        tv = pd.DataFrame({
            "timestamp": [1000, 2000, 3000],
            "tv_long_entry": [True, False, True],
            "tv_short_entry": [False, False, False],
        })
        mine = pd.DataFrame({
            "timestamp": [1000, 2000, 3000],
            "le_trigger": [True, False, False],  # missing 3000
            "se_trigger": [False, False, False],
        })
        stats = bar_by_bar_match(tv, mine)
        assert stats["long_match"] == 1
        assert stats["tv_longs"] == 2
        assert stats["long_tv_only"] == [3000]
        assert stats["total_match_pct"] == 50.0

    def test_spurious_mine_signal(self):
        tv = pd.DataFrame({
            "timestamp": [1000, 2000],
            "tv_long_entry": [True, False],
            "tv_short_entry": [False, False],
        })
        mine = pd.DataFrame({
            "timestamp": [1000, 2000],
            "le_trigger": [True, True],  # spurious
            "se_trigger": [False, False],
        })
        stats = bar_by_bar_match(tv, mine)
        assert stats["long_match"] == 1
        assert stats["long_mine_only"] == [2000]


# ── trade_by_trade_match ────────────────────────────────────────────────


class TestTradeByTradeMatch:
    def test_exact_time_and_side_match(self):
        tv = [{
            "trade_num": 1, "side": "long",
            "entry_ts_ms": 1_000_000, "entry_price": 100.0,
            "exit_ts_ms": 2_000_000, "exit_price": 101.0,
            "pnl_usd": 100.0, "pnl_pct": 1.0,
            "entry_dt": "2025-01-01 00:00", "exit_dt": "2025-01-01 00:01",
        }]
        mine = [{
            "trade_num": 1, "side": "long",
            "entry_ts_ms": 1_000_000, "entry_price": 100.0,
            "exit_ts_ms": 2_000_000, "exit_price": 101.0,
            "pnl_usd": 100.0, "pnl_pct": 1.0,
            "entry_dt": "2025-01-01 00:00", "exit_dt": "2025-01-01 00:01",
            "equity_after": 1_000_100.0,
        }]
        stats = trade_by_trade_match(tv, mine)
        assert stats["matched"] == 1
        assert stats["tv_match_pct"] == 100.0
        assert stats["pnl_sign_agreement_pct"] == 100.0

    def test_time_outside_tolerance_fails(self):
        tv = [{
            "trade_num": 1, "side": "long",
            "entry_ts_ms": 1_000_000, "entry_price": 100.0,
            "exit_ts_ms": 2_000_000, "exit_price": 101.0,
            "pnl_usd": 100.0, "pnl_pct": 1.0,
            "entry_dt": "2025-01-01 00:00", "exit_dt": "2025-01-01 00:01",
        }]
        # Mine entry is 10 minutes after TV — outside ±5 min tolerance
        mine = [{
            "trade_num": 1, "side": "long",
            "entry_ts_ms": 1_000_000 + 10 * 60 * 1000,
            "entry_price": 100.0,
            "exit_ts_ms": 2_000_000, "exit_price": 101.0,
            "pnl_usd": 100.0, "pnl_pct": 1.0,
            "entry_dt": "?", "exit_dt": "?", "equity_after": 0,
        }]
        stats = trade_by_trade_match(tv, mine, time_tolerance_min=5)
        assert stats["matched"] == 0
        assert stats["tv_only"] == 1

    def test_side_mismatch_no_match(self):
        tv = [{
            "trade_num": 1, "side": "long",
            "entry_ts_ms": 1_000_000, "entry_price": 100.0,
            "exit_ts_ms": 2_000_000, "exit_price": 101.0,
            "pnl_usd": 100.0, "pnl_pct": 1.0,
            "entry_dt": "?", "exit_dt": "?",
        }]
        mine = [{
            "trade_num": 1, "side": "short",  # wrong side
            "entry_ts_ms": 1_000_000, "entry_price": 100.0,
            "exit_ts_ms": 2_000_000, "exit_price": 101.0,
            "pnl_usd": 100.0, "pnl_pct": 1.0,
            "entry_dt": "?", "exit_dt": "?", "equity_after": 0,
        }]
        stats = trade_by_trade_match(tv, mine)
        assert stats["matched"] == 0


# ── format_parity_report ────────────────────────────────────────────────


class TestFormatParityReport:
    def test_pass_verdict_at_100_percent(self):
        stats = {
            "tv_longs": 10, "tv_shorts": 10,
            "my_longs": 10, "my_shorts": 10,
            "long_match": 10, "short_match": 10,
            "long_tv_only": [], "long_mine_only": [],
            "short_tv_only": [], "short_mine_only": [],
            "total_tv": 20, "total_my": 20, "total_match": 20,
            "total_match_pct": 100.0,
        }
        report = format_parity_report(
            strategy_name="Test",
            tv_chart_csv="foo.csv",
            tv_trades_csv=None,
            bar_stats=stats,
            trade_stats=None,
            anchor_segments=[(0, 0)],
        )
        assert "✅ PASS" in report
        assert "100.00%" in report

    def test_fail_verdict_below_90_percent(self):
        stats = {
            "tv_longs": 100, "tv_shorts": 100,
            "my_longs": 50, "my_shorts": 50,
            "long_match": 50, "short_match": 50,
            "long_tv_only": [1, 2, 3], "long_mine_only": [],
            "short_tv_only": [], "short_mine_only": [],
            "total_tv": 200, "total_my": 100, "total_match": 100,
            "total_match_pct": 50.0,
        }
        report = format_parity_report(
            strategy_name="Test",
            tv_chart_csv="foo.csv",
            tv_trades_csv=None,
            bar_stats=stats,
            trade_stats=None,
            anchor_segments=[(0, 0)],
        )
        assert "❌ FAIL" in report
