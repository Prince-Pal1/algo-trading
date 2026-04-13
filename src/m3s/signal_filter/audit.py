"""SQLite write-through for the `signal_audit` table (Phase 0).

The table is created by `ResultStore._init_tables()` when the backtest
DB path is opened. This module provides the two narrow write APIs the
engine (backtest + live) calls:

1. `audit_signal(conn, run_id, signal, features_dict, primary_model_ver) -> audit_id`
   Called immediately after `strategy.process()` returns a non-None Signal.
   Writes ONE row with the features and NULL outcome columns.
   Returns the new row ID so the engine can pass it to the close handler.

2. `audit_close(conn, audit_id, trade_id, exit_info)` — UPDATE the row
   with the realized outcome (exit_price, pnl, barrier_hit, labels,
   label_t1_ms).

Both functions are idempotent and fail-safe: any exception is caught,
logged as a warning, and swallowed. The engine must never crash because
an audit write failed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.m3s.signal_filter.labels import meta_label_from_outcome
from src.utils.logger import get_logger
from src.utils.types import Signal

log = get_logger("signal_audit")


@dataclass(frozen=True)
class CloseInfo:
    """What the close handler knows at trade close time."""
    trade_id: int | None
    exit_ts_ms: int
    exit_price: float
    realized_pnl: float
    barrier_hit: str                    # 'pt' / 'sl' / 'time' / 'signal'
    entry_price: float                   # needed to compute meta label
    direction: int                       # +1 LONG / -1 SHORT


def audit_signal(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    signal: Signal,
    features_dict: dict[str, Any],
    primary_model_ver: str = "",
) -> int | None:
    """Write one signal_audit row. Returns the new row id on success, None on failure."""
    try:
        features_json = json.dumps(features_dict, default=_json_default)
    except Exception as e:
        log.warning("signal_audit_features_serialize_failed",
                    strategy=signal.strategy_name, error=str(e))
        features_json = "{}"

    try:
        cursor = conn.execute(
            """
            INSERT INTO signal_audit (
                run_id, strategy, symbol, timeframe, signal_ts_ms, signal_action,
                signal_confidence, entry_price, stop_loss, take_profit,
                risk_pct_original, features_json, primary_model_ver
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run_id),
                str(signal.strategy_name or ""),
                str(signal.symbol),
                str(signal.timeframe or ""),
                int(signal.timestamp or int(time.time() * 1000)),
                str(signal.action.value),
                float(signal.confidence) if signal.confidence is not None else None,
                float(signal.entry_price) if signal.entry_price is not None else None,
                float(signal.stop_loss) if signal.stop_loss is not None else None,
                float(signal.take_profit) if signal.take_profit is not None else None,
                float(signal.risk_pct) if signal.risk_pct is not None else None,
                features_json,
                primary_model_ver,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    except sqlite3.Error as e:
        log.warning("signal_audit_insert_failed",
                    strategy=signal.strategy_name, error=str(e))
        return None


def audit_close(
    conn: sqlite3.Connection,
    audit_id: int,
    info: CloseInfo,
) -> bool:
    """UPDATE the audit row with realized outcome + labels. Returns True on success."""
    if audit_id <= 0:
        return False

    try:
        tb_label, meta_label = meta_label_from_outcome(
            direction=info.direction,
            entry_price=info.entry_price,
            exit_price=info.exit_price,
            barrier_hit=info.barrier_hit,
        )
    except Exception as e:
        log.warning("signal_audit_label_failed",
                    audit_id=audit_id, error=str(e))
        tb_label, meta_label = 0, 0

    try:
        realized_pnl_pct = 0.0
        if info.entry_price > 0:
            realized_pnl_pct = (info.realized_pnl / info.entry_price) * 100.0

        conn.execute(
            """
            UPDATE signal_audit SET
                trade_id             = ?,
                exit_ts_ms           = ?,
                exit_price           = ?,
                realized_pnl         = ?,
                realized_pnl_pct     = ?,
                barrier_hit          = ?,
                triple_barrier_label = ?,
                meta_label           = ?,
                label_t1_ms          = ?
            WHERE id = ?
            """,
            (
                info.trade_id,
                int(info.exit_ts_ms),
                float(info.exit_price),
                float(info.realized_pnl),
                float(realized_pnl_pct),
                info.barrier_hit,
                int(tb_label),
                int(meta_label),
                int(info.exit_ts_ms),
                int(audit_id),
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        log.warning("signal_audit_update_failed",
                    audit_id=audit_id, error=str(e))
        return False


def _json_default(obj: Any) -> Any:
    """Fallback serializer for numpy scalars and other odd types."""
    if hasattr(obj, "item"):  # numpy scalar
        return obj.item()
    return str(obj)
