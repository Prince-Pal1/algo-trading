"""Unit tests for the TV parity validation framework."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.tv_parity import (
    _collapse_ladder_legs,
    anchor_for,
    bar_by_bar_match,
    detect_alt_anchor_segments,
    detect_chart_tz_from_csv,
    extract_pine_config_from_xlsx,
    format_parity_report,
    load_tv_chart_csv,
    load_tv_trades_csv,
    load_tv_trades_xlsx,
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

    def test_iso_8601_with_tz_offset(self, tmp_path):
        """Newer TV exports use ISO 8601 strings like '2025-12-29T04:30:00+05:30'."""
        p = tmp_path / "iso.csv"
        p.write_text(
            "time,open,high,low,close,Long,Short\n"
            "2025-12-29T04:30:00+05:30,100,101,99,100.5,,\n"
            "2025-12-29T04:35:00+05:30,100.5,102,100,101.5,1,\n"
            "2025-12-29T04:40:00+05:30,101.5,102,101,101.8,,1\n"
        )
        df = load_tv_chart_csv(p)
        assert len(df) == 3
        # 04:30 IST = 23:00 UTC of prev day
        first_dt = df["dt"].iloc[0]
        assert first_dt.hour == 23  # 23:00 UTC
        assert first_dt.day == 28
        # Bars are 5 min apart → timestamps differ by 300_000 ms
        diffs = df["timestamp"].diff().dropna().tolist()
        assert all(d == 300_000 for d in diffs)
        assert df["tv_long_entry"].tolist() == [False, True, False]
        assert df["tv_short_entry"].tolist() == [False, False, True]


# ── load_tv_trades_xlsx + collapse_ladder_legs ──────────────────────────


class TestCollapseLadderLegs:
    def test_three_legs_same_entry_collapse_to_one(self):
        # Same entry signal split into 3 ladder legs
        legs = [
            {"trade_num": 1, "side": "long", "entry_ts_ms": 1000,
             "entry_dt": "2025-01-01 00:00", "entry_price": 100.0,
             "exit_ts_ms": 2000, "exit_dt": "2025-01-01 00:01", "exit_price": 101.0,
             "pnl_usd": 50.0, "pnl_pct": 0.5, "exit_signal": "LXTP1"},
            {"trade_num": 2, "side": "long", "entry_ts_ms": 1000,
             "entry_dt": "2025-01-01 00:00", "entry_price": 100.0,
             "exit_ts_ms": 3000, "exit_dt": "2025-01-01 00:02", "exit_price": 101.5,
             "pnl_usd": 30.0, "pnl_pct": 0.3, "exit_signal": "LXTP2"},
            {"trade_num": 3, "side": "long", "entry_ts_ms": 1000,
             "entry_dt": "2025-01-01 00:00", "entry_price": 100.0,
             "exit_ts_ms": 4000, "exit_dt": "2025-01-01 00:03", "exit_price": 102.0,
             "pnl_usd": 20.0, "pnl_pct": 0.2, "exit_signal": "LXTP3"},
        ]
        collapsed = _collapse_ladder_legs(legs)
        assert len(collapsed) == 1
        c = collapsed[0]
        assert c["leg_count"] == 3
        assert c["pnl_usd"] == 100.0  # 50 + 30 + 20
        assert c["pnl_pct"] == pytest.approx(1.0)
        # Latest exit wins
        assert c["exit_ts_ms"] == 4000
        assert c["exit_price"] == 102.0
        assert "LXTP1" in c["exit_signals"]
        assert "LXTP3" in c["exit_signals"]

    def test_distinct_entries_not_collapsed(self):
        legs = [
            {"trade_num": 1, "side": "long", "entry_ts_ms": 1000,
             "entry_dt": "1", "entry_price": 100.0,
             "exit_ts_ms": 2000, "exit_dt": "2", "exit_price": 101.0,
             "pnl_usd": 50.0, "pnl_pct": 0.5, "exit_signal": "SE"},
            {"trade_num": 2, "side": "short", "entry_ts_ms": 2000,
             "entry_dt": "2", "entry_price": 101.0,
             "exit_ts_ms": 3000, "exit_dt": "3", "exit_price": 100.5,
             "pnl_usd": 25.0, "pnl_pct": 0.25, "exit_signal": "LE"},
        ]
        collapsed = _collapse_ladder_legs(legs)
        assert len(collapsed) == 2
        assert all(c["leg_count"] == 1 for c in collapsed)

    def test_long_and_short_at_same_ts_not_collapsed(self):
        # Same ts but different sides → different keys → no collapse
        legs = [
            {"trade_num": 1, "side": "long", "entry_ts_ms": 1000,
             "entry_dt": "1", "entry_price": 100.0,
             "exit_ts_ms": 2000, "exit_dt": "2", "exit_price": 101.0,
             "pnl_usd": 50.0, "pnl_pct": 0.5, "exit_signal": "TP1"},
            {"trade_num": 2, "side": "short", "entry_ts_ms": 1000,
             "entry_dt": "1", "entry_price": 100.0,
             "exit_ts_ms": 2000, "exit_dt": "2", "exit_price": 99.0,
             "pnl_usd": 25.0, "pnl_pct": 0.25, "exit_signal": "TP1"},
        ]
        collapsed = _collapse_ladder_legs(legs)
        assert len(collapsed) == 2


class TestLoadTvTradesXlsx:
    def _write_xlsx(self, tmp_path, rows: list[dict]) -> Path:
        """Build a minimal xlsx with a 'List of trades' sheet."""
        p = tmp_path / "trades.xlsx"
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(p, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="List of trades", index=False)
        return p

    def test_loads_basic_long_trade(self, tmp_path):
        rows = [
            {"Trade #": 1, "Type": "Exit long",
             "Date and time": pd.Timestamp("2025-12-29 09:10:00"),
             "Signal": "SE", "Price USD": 101.0,
             "Size (qty)": 22.1, "Size (value)": 2200,
             "Net P&L USD": 50.0, "Net P&L %": 0.5},
            {"Trade #": 1, "Type": "Entry long",
             "Date and time": pd.Timestamp("2025-12-29 06:30:00"),
             "Signal": "LE", "Price USD": 100.0,
             "Size (qty)": 22.1, "Size (value)": 2200,
             "Net P&L USD": 50.0, "Net P&L %": 0.5},
        ]
        p = self._write_xlsx(tmp_path, rows)
        trades = load_tv_trades_xlsx(p, tz="UTC")
        assert len(trades) == 1
        t = trades[0]
        assert t["side"] == "long"
        assert t["entry_price"] == 100.0
        assert t["exit_price"] == 101.0
        assert t["pnl_usd"] == 50.0
        assert t["leg_count"] == 1

    def test_tz_conversion_ist_to_utc(self, tmp_path):
        # 06:30 IST = 01:00 UTC
        rows = [
            {"Trade #": 1, "Type": "Exit long",
             "Date and time": pd.Timestamp("2025-12-29 09:10:00"),
             "Signal": "SE", "Price USD": 101.0,
             "Size (qty)": 1, "Size (value)": 100,
             "Net P&L USD": 0, "Net P&L %": 0},
            {"Trade #": 1, "Type": "Entry long",
             "Date and time": pd.Timestamp("2025-12-29 06:30:00"),
             "Signal": "LE", "Price USD": 100.0,
             "Size (qty)": 1, "Size (value)": 100,
             "Net P&L USD": 0, "Net P&L %": 0},
        ]
        p = self._write_xlsx(tmp_path, rows)
        trades = load_tv_trades_xlsx(p, tz="Asia/Kolkata")
        # 06:30 IST = 01:00 UTC
        expected_ts = int(pd.Timestamp("2025-12-29 01:00:00", tz="UTC").value // 1_000_000)
        assert trades[0]["entry_ts_ms"] == expected_ts
        assert trades[0]["entry_dt"] == "2025-12-29 01:00"

    def test_three_leg_ladder_collapses(self, tmp_path):
        # Same entry, 3 ladder legs
        entry_ts = pd.Timestamp("2025-12-29 06:00:00")
        rows = []
        for trade_num, (qty, exit_min, exit_px, pnl) in enumerate(
            [(11.0, 10, 101.0, 11.0), (6.6, 20, 101.5, 9.9), (4.4, 30, 102.0, 8.8)],
            start=1,
        ):
            exit_ts = pd.Timestamp(f"2025-12-29 06:{exit_min:02d}:00")
            rows.append({"Trade #": trade_num, "Type": "Exit long",
                         "Date and time": exit_ts, "Signal": f"LXTP{trade_num}",
                         "Price USD": exit_px, "Size (qty)": qty,
                         "Size (value)": qty*100, "Net P&L USD": pnl, "Net P&L %": 0.1})
            rows.append({"Trade #": trade_num, "Type": "Entry long",
                         "Date and time": entry_ts, "Signal": "LE",
                         "Price USD": 100.0, "Size (qty)": qty,
                         "Size (value)": qty*100, "Net P&L USD": pnl, "Net P&L %": 0.1})
        p = self._write_xlsx(tmp_path, rows)
        trades = load_tv_trades_xlsx(p, tz="UTC", dedupe_ladder_legs=True)
        assert len(trades) == 1
        t = trades[0]
        assert t["leg_count"] == 3
        assert t["pnl_usd"] == pytest.approx(29.7)  # 11+9.9+8.8

    def test_dedupe_disabled_keeps_legs(self, tmp_path):
        entry_ts = pd.Timestamp("2025-12-29 06:30:00")
        rows = []
        for tn in (1, 2, 3):
            rows.append({"Trade #": tn, "Type": "Exit long",
                         "Date and time": pd.Timestamp(f"2025-12-29 07:{tn:02d}:00"),
                         "Signal": f"LXTP{tn}", "Price USD": 100 + tn, "Size (qty)": 1,
                         "Size (value)": 100, "Net P&L USD": tn, "Net P&L %": 0.1})
            rows.append({"Trade #": tn, "Type": "Entry long",
                         "Date and time": entry_ts, "Signal": "LE",
                         "Price USD": 100.0, "Size (qty)": 1,
                         "Size (value)": 100, "Net P&L USD": tn, "Net P&L %": 0.1})
        p = self._write_xlsx(tmp_path, rows)
        trades = load_tv_trades_xlsx(p, tz="UTC", dedupe_ladder_legs=False)
        assert len(trades) == 3


class TestExtractPineConfigFromXlsx:
    def _write_props_xlsx(self, tmp_path, props: list[tuple[str, str]]) -> Path:
        p = tmp_path / "props.xlsx"
        df = pd.DataFrame(props, columns=["name", "value"])
        with pd.ExcelWriter(p, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Properties", index=False)
        return p

    def test_extracts_pine_inputs(self, tmp_path):
        props = [
            ("Symbol", "VANTAGE:XAUUSD"),
            ("Timeframe", "5 minutes"),
            ("TIMEFRAME", "15"),  # Pine input default
            ("Use Alternate Signals", "On"),
            ("Multiplier for Alernate Signals", "8"),
            ("MA Type: ", "ALMA"),
            ("MA Period", "2"),
            ("Offset for ALMA", "0.85"),
            ("Offset for LSMA / Sigma for ALMA", "5"),
            ("Stop Loss", "0.5"),
            ("Level TP1", "1"),
            ("Level TP2", "1.5"),
            ("Level TP3", "2"),
            ("Qty   TP1", "50"),
            ("Qty   TP2", "30"),
            ("Qty   TP3", "20"),
            ("Initial capital", "1000000"),
            ("Order size", "10% of equity"),
            ("Commission", "0"),
        ]
        p = self._write_props_xlsx(tmp_path, props)
        cfg = extract_pine_config_from_xlsx(p)
        assert cfg["symbol"] == "VANTAGE:XAUUSD"
        assert cfg["chart_tf_min"] == 5
        # alt_tf_min = chart_tf × multiplier (NOT pine_res × multiplier)
        # because Pine uses chart TF at runtime, not the input value
        assert cfg["alt_tf_min"] == 40  # 5 × 8
        assert cfg["alt_tf_multiplier"] == 8
        assert cfg["alma_length"] == 2
        assert cfg["alma_offset"] == 0.85
        assert cfg["alma_sigma"] == 5.0
        assert cfg["sl_pct"] == 0.5
        assert cfg["tp1_pct"] == 1.0
        assert cfg["tp2_pct"] == 1.5
        assert cfg["tp3_pct"] == 2.0

    def test_missing_properties_returns_empty(self, tmp_path):
        # File without Properties sheet
        p = tmp_path / "no_props.xlsx"
        df = pd.DataFrame({"foo": [1, 2]})
        with pd.ExcelWriter(p, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Other", index=False)
        cfg = extract_pine_config_from_xlsx(p)
        assert cfg == {}


class TestDetectChartTzFromCsv:
    def test_iso_with_offset(self, tmp_path):
        p = tmp_path / "iso.csv"
        p.write_text(
            "time,open,high,low,close\n"
            "2025-12-29T04:30:00+05:30,100,101,99,100\n"
            "2025-12-29T04:35:00+05:30,100,101,99,100\n"
        )
        tz = detect_chart_tz_from_csv(p)
        assert tz == "UTC+05:30"

    def test_unix_seconds_returns_none(self, tmp_path):
        p = tmp_path / "unix.csv"
        p.write_text(
            "time,open,high,low,close\n"
            "1000,100,101,99,100\n"
        )
        assert detect_chart_tz_from_csv(p) is None

    def test_iso_utc_offset_zero(self, tmp_path):
        p = tmp_path / "utc.csv"
        p.write_text(
            "time,open,high,low,close\n"
            "2025-12-29T04:30:00+00:00,100,101,99,100\n"
        )
        tz = detect_chart_tz_from_csv(p)
        assert tz == "UTC+00:00"


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
