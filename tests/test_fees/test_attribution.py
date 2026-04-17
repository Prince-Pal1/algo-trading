"""Phase E — per-trade cost attribution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.fees.attribution import (
    attribute_realized_pnl_pct,
    ensure_schema,
    read_attribution,
    stamp_round_trip,
)


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    db = tmp_path / "trades.db"
    ensure_schema(db)
    return db


class TestSchema:
    def test_schema_is_idempotent(self, tmp_db):
        # Calling ensure_schema twice must not error
        ensure_schema(tmp_db)
        ensure_schema(tmp_db)

    def test_table_exists(self, tmp_db):
        import sqlite3
        conn = sqlite3.connect(str(tmp_db))
        r = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_cost_attribution'"
        ).fetchone()
        conn.close()
        assert r is not None


class TestStampRoundTrip:
    def test_basic_stamp(self, tmp_db):
        rid = stamp_round_trip(
            tmp_db,
            strategy="donchian_gold",
            symbol="XAUUSD",
            qty_lots=1.0,
            open_price=4850.0,
            close_price=4880.0,
            open_ts_ms=1776000000000,  # 2026-04-12
            close_ts_ms=1776173000000,  # +48h
            side="long",
            style="swing",
        )
        assert rid > 0

        rows = read_attribution(tmp_db, strategy="donchian_gold")
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"] == "XAUUSD"
        assert r["qty_lots"] == 1.0
        assert r["side"] == "long"
        assert r["fee_style"] == "swing"
        assert r["broker_id"] == "ic_markets_ctrader"
        assert r["scenario"] in ("normal", "news_active", "illiquid", "volatile")
        assert r["spread_cost_usd"] > 0
        assert r["commission_usd"] > 0
        # Long swing on gold over 48h (2 nights) → negative swap cost
        assert r["swap_usd"] < 0
        # Total is sum of components
        assert r["total_cost_usd"] == pytest.approx(
            r["spread_cost_usd"] + r["commission_usd"] + r["swap_usd"] + r["slippage_usd"],
            rel=1e-6,
        )

    def test_short_side_different_swap(self, tmp_db):
        stamp_round_trip(
            tmp_db, strategy="x", symbol="XAUUSD", qty_lots=1.0,
            open_price=4850.0, close_price=4830.0,
            open_ts_ms=1776000000000, close_ts_ms=1776173000000,
            side="short", style="swing",
        )
        rows = read_attribution(tmp_db, strategy="x")
        # Short gold earns swap (positive)
        assert rows[0]["swap_usd"] > 0

    def test_slippage_additive(self, tmp_db):
        stamp_round_trip(
            tmp_db, strategy="x", symbol="XAUUSD", qty_lots=1.0,
            open_price=4850.0, close_price=4860.0,
            open_ts_ms=1776000000000, close_ts_ms=1776010000000,
            side="long", style="intraday",
            slippage_usd=7.5,
        )
        rows = read_attribution(tmp_db, strategy="x")
        assert rows[0]["slippage_usd"] == 7.5
        assert rows[0]["total_cost_usd"] == pytest.approx(
            rows[0]["spread_cost_usd"] + rows[0]["commission_usd"] + rows[0]["swap_usd"] + 7.5,
            rel=1e-6,
        )


class TestReadAttribution:
    def test_filter_by_strategy(self, tmp_db):
        stamp_round_trip(
            tmp_db, strategy="A", symbol="XAUUSD", qty_lots=1.0,
            open_price=4850.0, close_price=4860.0,
            open_ts_ms=1776000000000, close_ts_ms=1776010000000,
            side="long", style="intraday",
        )
        stamp_round_trip(
            tmp_db, strategy="B", symbol="XAUUSD", qty_lots=1.0,
            open_price=4850.0, close_price=4860.0,
            open_ts_ms=1776000000000, close_ts_ms=1776010000000,
            side="long", style="intraday",
        )
        a_rows = read_attribution(tmp_db, strategy="A")
        b_rows = read_attribution(tmp_db, strategy="B")
        assert len(a_rows) == 1
        assert len(b_rows) == 1
        assert a_rows[0]["strategy"] == "A"
        assert b_rows[0]["strategy"] == "B"


class TestAttributePct:
    def test_pct_breakdown_sums(self):
        row = {
            "spread_cost_usd": 10.0,
            "commission_usd": 20.0,
            "swap_usd": -5.0,
            "slippage_usd": 0.0,
            "total_cost_usd": 25.0,
        }
        realized = 100.0  # net profit
        # Gross = 100 + 25 = 125
        pct = attribute_realized_pnl_pct(row, realized)
        assert pct["spread_pct"] == pytest.approx(100 * 10 / 125, rel=1e-3)
        assert pct["commission_pct"] == pytest.approx(100 * 20 / 125, rel=1e-3)
        assert pct["swap_pct"] == pytest.approx(100 * -5 / 125, rel=1e-3)

    def test_zero_gross_no_divide(self):
        row = {"spread_cost_usd": 0.0, "commission_usd": 0.0, "swap_usd": 0.0, "slippage_usd": 0.0, "total_cost_usd": 0.0}
        pct = attribute_realized_pnl_pct(row, 0.0)
        assert pct["spread_pct"] == 0.0
