"""Per-trade cost attribution — records WHICH cost components drove a trade's
realized P&L so strategy evaluation can be honest ("your $50 loss was $30
commission + $10 spread + $10 alpha decay", not just "-$50").

Stores to `trade_cost_attribution` in data/trades.db — side table joined on
the `trades` table's `id` or (symbol, strategy, open/close timestamps).

Usage:

    from src.fees.attribution import stamp_round_trip, ensure_schema

    ensure_schema(db_path)  # idempotent, safe to call on every boot

    # At trade-close time:
    stamp_round_trip(
        db_path="data/trades.db",
        strategy="donchian_gold",
        symbol="XAUUSD",
        qty_lots=1.0,
        open_trade_id=123,        # id in trades table
        close_trade_id=124,
        open_price=4865.0,
        close_price=4890.0,
        open_ts_ms=...,
        close_ts_ms=...,
        side="long",
        style="swing",
    )

The writer pulls the current fee profile from FeeManager (active broker ×
symbol × auto-scenario from open_ts_ms) and computes:
  spread_cost_usd, commission_usd, swap_usd, slippage_usd
plus metadata (profile_name, scenario, broker_id) so analysts can always
trace what assumption was used.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.fees.manager import FeeManager
from src.fees.scenario import detect_simple


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trade_cost_attribution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    open_trade_id   INTEGER,
    close_trade_id  INTEGER,
    open_ts_ms      INTEGER NOT NULL,
    close_ts_ms     INTEGER,
    qty_lots        REAL NOT NULL,
    side            TEXT NOT NULL,
    spread_cost_usd     REAL NOT NULL,
    commission_usd      REAL NOT NULL,
    swap_usd            REAL NOT NULL,
    slippage_usd        REAL NOT NULL DEFAULT 0.0,
    total_cost_usd      REAL NOT NULL,
    broker_id       TEXT NOT NULL,
    profile_name    TEXT NOT NULL,
    scenario        TEXT NOT NULL,
    fee_style       TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_attribution_strategy ON trade_cost_attribution(strategy);
CREATE INDEX IF NOT EXISTS idx_attribution_symbol ON trade_cost_attribution(symbol);
CREATE INDEX IF NOT EXISTS idx_attribution_close_ts ON trade_cost_attribution(close_ts_ms);
"""


def ensure_schema(db_path: str | Path) -> None:
    """Create the trade_cost_attribution table + indexes if missing. Idempotent."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def stamp_round_trip(
    db_path: str | Path,
    *,
    strategy: str,
    symbol: str,
    qty_lots: float,
    open_price: float,
    close_price: float,
    open_ts_ms: int,
    close_ts_ms: int,
    side: str = "long",
    style: str = "intraday",
    open_trade_id: int | None = None,
    close_trade_id: int | None = None,
    slippage_usd: float = 0.0,
    broker_id: str | None = None,
    notes: str | None = None,
) -> int:
    """Write one attribution row for a closed round-trip.

    Pulls fee profile via FeeManager using the open_ts_ms (so the scenario
    matches the market state at entry). Commission + spread + swap are
    derived from the profile; slippage is caller-provided (defaults to 0
    when the broker fills at the mid).

    Returns the inserted row id.
    """
    ensure_schema(db_path)

    # Use mid = (open + close) / 2 for notional computation (conservative).
    mid_price = (open_price + close_price) / 2.0
    hold_hours = max(0.0, (close_ts_ms - open_ts_ms) / 1000 / 3600)

    cp = FeeManager.project_cost(
        symbol=symbol,
        qty_lots=qty_lots,
        style=style,
        hold_hours=hold_hours,
        mid_price=mid_price,
        timestamp_ms=open_ts_ms,
        broker_id=broker_id,
        side=side,
    )

    total = cp.spread + cp.commission + cp.swap + slippage_usd

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO trade_cost_attribution(
                strategy, symbol, open_trade_id, close_trade_id,
                open_ts_ms, close_ts_ms, qty_lots, side,
                spread_cost_usd, commission_usd, swap_usd, slippage_usd,
                total_cost_usd, broker_id, profile_name, scenario, fee_style, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy, symbol, open_trade_id, close_trade_id,
                int(open_ts_ms), int(close_ts_ms), float(qty_lots), side,
                float(cp.spread), float(cp.commission), float(cp.swap),
                float(slippage_usd),
                float(total),
                cp.broker_id, cp.profile_name, cp.scenario, style,
                notes,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def attribute_realized_pnl_pct(
    attribution_row: dict[str, Any], realized_pnl_usd: float
) -> dict[str, float]:
    """Given a closed trade's attribution row + its realized P&L, return the
    percentage breakdown of the *gross* P&L's cost components.

    Gross P&L = realized_pnl + total_cost (since total_cost eats into gross
    to give net realized). Each component is reported as a % of gross,
    which makes "how much edge did costs eat?" immediately visible.
    """
    total_cost = float(attribution_row.get("total_cost_usd", 0.0))
    gross = realized_pnl_usd + total_cost
    if gross == 0:
        return {"spread_pct": 0.0, "commission_pct": 0.0, "swap_pct": 0.0, "slippage_pct": 0.0}
    return {
        "spread_pct": 100 * float(attribution_row.get("spread_cost_usd", 0.0)) / gross,
        "commission_pct": 100 * float(attribution_row.get("commission_usd", 0.0)) / gross,
        "swap_pct": 100 * float(attribution_row.get("swap_usd", 0.0)) / gross,
        "slippage_pct": 100 * float(attribution_row.get("slippage_usd", 0.0)) / gross,
    }


def read_attribution(
    db_path: str | Path,
    *,
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read attribution rows for dashboard/report display."""
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM trade_cost_attribution WHERE 1=1"
        args: list = []
        if strategy:
            sql += " AND strategy = ?"
            args.append(strategy)
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        sql += " ORDER BY close_ts_ms DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
