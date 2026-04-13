"""Result storage — atomic triple output (SQLite + JSON + HTML).

Invariant #1: save_run() is the ONLY way to persist a backtest result.
There is no "save to DB now, generate HTML later" path.

Invariant #2: Metrics are computed once by compute_metrics(), then passed to
all three consumers (DB, JSON, HTML).

Invariant #3: Every run is logged with run_id + data_fingerprint via structlog.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import orjson
import pandas as pd
import structlog

from src.backtest.metrics import compute_metrics
from src.backtest.report import render_report

log = structlog.get_logger("result_store")

_BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT DEFAULT '1.0',
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    config_json TEXT,
    params_json TEXT,
    source_format TEXT DEFAULT 'python',
    data_fingerprint TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
    total_return_pct REAL,
    sharpe REAL,
    sortino REAL,
    max_drawdown_pct REAL,
    win_rate_pct REAL,
    profit_factor REAL,
    total_trades INTEGER,
    avg_win_loss_ratio REAL,
    calmar REAL,
    total_commission REAL,
    buy_hold_return_pct REAL,
    psr REAL,
    equity_curve_json TEXT,
    trades_json TEXT,
    metrics_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_id);

CREATE TABLE IF NOT EXISTS validation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT DEFAULT '1.0',
    symbol TEXT,
    tier TEXT NOT NULL,
    protocols_run TEXT,
    verdict TEXT,
    summary_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS protocol_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES validation_sessions(id),
    protocol TEXT NOT NULL,
    passed BOOLEAN,
    result_json TEXT,
    runtime_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_vs_strategy ON validation_sessions(strategy_id);

-- Phase 3c signal audit table. Every signal emission writes one row with
-- the feature vector as seen at signal time. Trade-close path UPDATEs the
-- row with the realized outcome + triple-barrier label. Read by the
-- meta-labeling training pipeline (src/m3s/signal_filter/train.py).
CREATE TABLE IF NOT EXISTS signal_audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,              -- backtest run UUID or 'live'
    strategy            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    signal_ts_ms        INTEGER NOT NULL,
    signal_action       TEXT NOT NULL,              -- LONG / SHORT / CLOSE
    signal_confidence   REAL,
    entry_price         REAL,
    stop_loss           REAL,
    take_profit         REAL,
    risk_pct_original   REAL,
    features_json       TEXT NOT NULL,              -- dict of features at signal time
    primary_model_ver   TEXT,                        -- strategy params hash

    -- UPDATEd at trade close
    trade_id            INTEGER,
    exit_ts_ms          INTEGER,
    exit_price          REAL,
    realized_pnl        REAL,
    realized_pnl_pct    REAL,
    barrier_hit         TEXT,                        -- 'pt' / 'sl' / 'time' / 'signal'
    triple_barrier_label INTEGER,                    -- +1 / -1 / 0
    meta_label          INTEGER,                     -- 1 if tb_label == signal_action else 0
    label_t1_ms         INTEGER,                     -- time label was resolved

    -- meta-classifier audit (populated if filter is active in Phase 3+)
    meta_proba          REAL,
    meta_decision       TEXT,                        -- 'pass' / 'veto' / 'scale'
    meta_size_mult      REAL,
    meta_model_ver      TEXT,

    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signal_audit_strategy_ts ON signal_audit(strategy, signal_ts_ms);
CREATE INDEX IF NOT EXISTS idx_signal_audit_label_t1    ON signal_audit(label_t1_ms);
CREATE INDEX IF NOT EXISTS idx_signal_audit_run         ON signal_audit(run_id);
CREATE INDEX IF NOT EXISTS idx_signal_audit_trade_id    ON signal_audit(trade_id);
"""


@dataclass
class RunRecord:
    """Returned by save_run() — everything you need to reference the result."""
    run_id: int
    json_path: Path
    html_path: Path
    metrics: dict


class ResultStore:
    """Persists backtest results as the atomic triple: SQLite row + JSON + HTML."""

    def __init__(
        self,
        db_path: str = "data/trades.db",
        reports_dir: str = "reports",
    ):
        self._db_path = db_path
        self._reports_dir = Path(reports_dir)
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_tables()
        return self._conn

    def _init_tables(self) -> None:
        """Create backtest tables if they don't exist."""
        assert self._conn is not None
        self._conn.executescript(_BACKTEST_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # THE entry point — atomic triple output
    # ------------------------------------------------------------------

    def save_run(
        self,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        equity: pd.Series,
        trades: list,
        ohlcv_df: pd.DataFrame,
        *,
        strategy_version: str = "1.0",
        config_json: str = "",
        params_json: str = "",
        source_format: str = "python",
    ) -> RunRecord:
        """Produce all 3 outputs atomically:

        1. SQLite rows in backtest_runs + backtest_results
        2. JSON file at reports/<run_id>.json
        3. HTML file at reports/<run_id>.html

        Returns a RunRecord with run_id and file paths.
        """
        conn = self._ensure_conn()

        # Step 1: compute_metrics() — SINGLE source of truth (Invariant #2)
        metrics = compute_metrics(equity, trades, ohlcv=ohlcv_df)

        # Step 2: data fingerprint (Invariant #3)
        fingerprint = _data_fingerprint(ohlcv_df)

        # Step 3: date range
        start_date, end_date = _date_range_str(ohlcv_df)

        # Step 4: INSERT into backtest_runs
        cursor = conn.execute(
            """INSERT INTO backtest_runs
               (strategy_id, strategy_version, symbol, timeframe,
                start_date, end_date, config_json, params_json,
                source_format, data_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, strategy_version, symbol, timeframe,
             start_date, end_date, config_json, params_json,
             source_format, fingerprint),
        )
        run_id = cursor.lastrowid or 0

        # Step 5: INSERT into backtest_results (metrics from the SAME dict)
        trades_dicts = [_trade_to_dict(t) for t in trades]
        equity_json = orjson.dumps(
            {str(k): float(v) for k, v in zip(equity.index, equity.values)},
            option=orjson.OPT_SERIALIZE_NUMPY,
        ).decode()

        conn.execute(
            """INSERT INTO backtest_results
               (run_id, total_return_pct, sharpe, sortino, max_drawdown_pct,
                win_rate_pct, profit_factor, total_trades, avg_win_loss_ratio,
                calmar, total_commission, buy_hold_return_pct, psr,
                equity_curve_json, trades_json, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                metrics.get("total_return_pct"),
                metrics.get("sharpe"),
                metrics.get("sortino"),
                metrics.get("max_drawdown_pct"),
                metrics.get("win_rate_pct"),
                metrics.get("profit_factor"),
                metrics.get("total_trades"),
                metrics.get("avg_win_loss_ratio"),
                metrics.get("calmar"),
                metrics.get("total_commission"),
                metrics.get("buy_hold_return_pct"),
                metrics.get("psr"),
                equity_json,
                orjson.dumps(trades_dicts, option=orjson.OPT_SERIALIZE_NUMPY).decode(),
                orjson.dumps(metrics, option=orjson.OPT_SERIALIZE_NUMPY).decode(),
            ),
        )
        conn.commit()

        # Step 6: structlog with run_id + fingerprint (Invariant #3)
        log.info(
            "backtest_complete",
            run_id=run_id,
            data_fingerprint=fingerprint,
            strategy=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            sharpe=metrics.get("sharpe"),
            max_dd=metrics.get("max_drawdown_pct"),
            trades=metrics.get("total_trades"),
        )

        # Step 7: JSON export
        json_path = self._reports_dir / f"{run_id}.json"
        json_payload = metrics | {
            "run_id": run_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_date": start_date,
            "end_date": end_date,
            "data_fingerprint": fingerprint,
            "trades": trades_dicts,
        }
        json_path.write_text(orjson.dumps(
            json_payload,
            option=orjson.OPT_INDENT_2 | orjson.OPT_SERIALIZE_NUMPY,
        ).decode())

        # Step 8: HTML report (reads metrics dict, does NOT recompute)
        html_path = self._reports_dir / f"{run_id}.html"
        html = render_report(
            strategy_id=strategy_id,
            metrics=metrics,
            equity=equity,
            trades=trades,
            run_id=run_id,
            symbol=symbol,
            timeframe=timeframe,
            data_fingerprint=fingerprint,
        )
        html_path.write_text(html)

        return RunRecord(
            run_id=run_id,
            json_path=json_path,
            html_path=html_path,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Validation persistence
    # ------------------------------------------------------------------

    def save_validation(
        self,
        strategy_id: str,
        symbol: str,
        tier: str,
        protocol_results: list,
        verdict: str = "",
        strategy_version: str = "1.0",
    ) -> int:
        """Persist a validation session and its protocol results to DB.

        Args:
            strategy_id: Strategy that was validated.
            symbol: Symbol tested.
            tier: Validation tier (lite, standard, intense, research).
            protocol_results: List of ProtocolResult objects.
            verdict: Summary verdict string (e.g. "2/2 passed").
            strategy_version: Version tag.

        Returns:
            session_id from validation_sessions table.
        """
        conn = self._ensure_conn()
        protocols_run = ",".join(r.protocol for r in protocol_results)
        summary = {
            "verdict": verdict,
            "protocols": [
                {"protocol": r.protocol, "passed": r.passed, "summary": r.summary}
                for r in protocol_results
            ],
        }

        cur = conn.execute(
            """INSERT INTO validation_sessions
               (strategy_id, strategy_version, symbol, tier, protocols_run, verdict, summary_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, strategy_version, symbol, tier, protocols_run, verdict,
             orjson.dumps(summary).decode()),
        )
        session_id = cur.lastrowid or 0

        for r in protocol_results:
            detail_json = "{}"
            if hasattr(r, "detail") and r.detail is not None:
                try:
                    detail_json = orjson.dumps(r.detail, option=orjson.OPT_SERIALIZE_NUMPY).decode()
                except Exception:
                    detail_json = str(r.detail)
            conn.execute(
                """INSERT INTO protocol_results
                   (session_id, protocol, passed, result_json, runtime_seconds)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, r.protocol, r.passed, detail_json,
                 getattr(r, "runtime_seconds", 0.0)),
            )

        conn.commit()

        log.info(
            "validation_complete",
            session_id=session_id,
            strategy=strategy_id,
            symbol=symbol,
            tier=tier,
            verdict=verdict,
        )

        return session_id

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    def get_runs(
        self, strategy_id: str, limit: int = 50
    ) -> list[dict]:
        conn = self._ensure_conn()
        rows = conn.execute(
            """SELECT r.*, res.sharpe, res.max_drawdown_pct, res.total_return_pct,
                      res.total_trades, res.profit_factor
               FROM backtest_runs r
               JOIN backtest_results res ON res.run_id = r.id
               WHERE r.strategy_id = ?
               ORDER BY r.created_at DESC LIMIT ?""",
            (strategy_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_run_metrics(self, run_id: int) -> dict | None:
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT metrics_json FROM backtest_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row and row["metrics_json"]:
            return orjson.loads(row["metrics_json"])
        return None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _data_fingerprint(df: pd.DataFrame) -> str:
    """Deterministic hash of input OHLCV data (Invariant #3)."""
    h = hashlib.sha256(
        pd.util.hash_pandas_object(df).values.tobytes()
    ).hexdigest()[:12]
    return h


def _date_range_str(df: pd.DataFrame) -> tuple[str, str]:
    """Extract start/end date strings from OHLCV DataFrame."""
    try:
        ts_col = df["timestamp"]
        start = pd.to_datetime(ts_col.iloc[0], unit="ms").strftime("%Y-%m-%d")
        end = pd.to_datetime(ts_col.iloc[-1], unit="ms").strftime("%Y-%m-%d")
        return start, end
    except Exception:
        return "unknown", "unknown"


def _trade_to_dict(trade) -> dict:
    """Convert a Trade dataclass to a plain dict for JSON serialization."""
    return {
        "entry_idx": int(trade.entry_idx),
        "exit_idx": int(trade.exit_idx),
        "side": str(trade.side),
        "entry_price": float(trade.entry_price),
        "exit_price": float(trade.exit_price),
        "quantity": float(trade.quantity),
        "pnl": round(float(trade.pnl), 4),
        "pnl_pct": round(float(trade.pnl_pct), 6),
        "commission": round(float(trade.commission), 4),
        "exit_reason": str(trade.exit_reason),
    }
