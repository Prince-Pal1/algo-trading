"""Storage layer — SQLite trade log, Parquet historical data, optional Redis cache.

Three storage tiers:
- Hot: Redis (optional, in-memory, sub-ms reads)
- Warm: SQLite (trade log, signals, P&L)
- Cold: Parquet files (historical OHLCV bars for backtesting)

Usage:
    store = Storage()
    await store.init()
    await store.log_signal(signal)
    await store.log_trade(fill)
    store.save_candles_parquet(df, "BTCUSDT", "1m")
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import orjson
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.utils.config import get_config
from src.utils.logger import get_logger
from src.utils.types import Fill, Signal

log = get_logger("storage")


# ── SQLite Trade Log ───────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    metadata TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    commission REAL DEFAULT 0,
    exchange TEXT,
    strategy TEXT,
    signal_id INTEGER REFERENCES signals(id),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    equity REAL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Backtest run metadata (one strategy + one config + one data window)
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

-- Results for a single run (1:1 with backtest_runs)
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

-- Protocol-level validation session (groups multiple protocol runs)
CREATE TABLE IF NOT EXISTS validation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT DEFAULT '1.0',
    tier TEXT NOT NULL,
    protocols_run TEXT,
    verdict TEXT,
    summary_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Individual protocol results within a validation session
CREATE TABLE IF NOT EXISTS protocol_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES validation_sessions(id),
    protocol TEXT NOT NULL,
    passed BOOLEAN,
    result_json TEXT,
    runtime_seconds REAL
);

-- Risk state key-value persistence (survives restarts)
CREATE TABLE IF NOT EXISTS risk_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Audit log of every risk decision
CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    signal_symbol TEXT NOT NULL,
    signal_strategy TEXT NOT NULL,
    signal_action TEXT NOT NULL,
    approved BOOLEAN NOT NULL,
    reason TEXT,
    original_risk_pct REAL,
    adjusted_quantity REAL,
    checks_json TEXT,
    equity_at_decision REAL,
    drawdown_pct REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Paper trading position persistence (survives crashes)
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    unrealized_pnl REAL DEFAULT 0,
    strategy_name TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    PRIMARY KEY (strategy_name, symbol)
);

-- Paper trading equity state (singleton row)
CREATE TABLE IF NOT EXISTS paper_equity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    equity REAL NOT NULL,
    initial_capital REAL NOT NULL,
    trade_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_validation_sessions_strategy ON validation_sessions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_ts ON risk_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_strategy ON risk_decisions(signal_strategy);
"""


class TradeLog:
    """SQLite-backed trade and signal logger."""

    def __init__(self, db_path: str | None = None):
        cfg = get_config()
        self.db_path = db_path or cfg.db_path
        self._conn: sqlite3.Connection | None = None

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        log.info("trade_log_ready", path=self.db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TradeLog not initialized — call init() first")
        return self._conn

    async def log_signal(self, signal: Signal) -> int:
        conn = self._ensure_conn()
        cursor = conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, action, confidence, "
            "entry_price, stop_loss, take_profit, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal.timestamp or int(time.time() * 1000),
                signal.symbol,
                signal.strategy_name,
                signal.action.value,
                signal.confidence,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit,
                orjson.dumps(signal.metadata, option=orjson.OPT_SERIALIZE_NUMPY).decode() if signal.metadata else None,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0

    async def log_trade(self, fill: Fill, strategy: str = "", signal_id: int | None = None) -> int:
        conn = self._ensure_conn()
        cursor = conn.execute(
            "INSERT INTO trades (order_id, timestamp, symbol, side, price, quantity, "
            "commission, exchange, strategy, signal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.order_id,
                fill.timestamp,
                fill.symbol,
                fill.side.value,
                fill.price,
                fill.quantity,
                fill.commission,
                fill.exchange,
                strategy,
                signal_id,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0

    async def log_risk_event(self, event_type: str, severity: str, description: str) -> None:
        conn = self._ensure_conn()
        conn.execute(
            "INSERT INTO risk_events (timestamp, event_type, severity, description) "
            "VALUES (?, ?, ?, ?)",
            (int(time.time() * 1000), event_type, severity, description),
        )
        conn.commit()

    def get_trades(self, symbol: str | None = None, limit: int = 100) -> list[dict]:
        conn = self._ensure_conn()
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trades WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Parquet Historical Storage ─────────────────────────────────────────────


class ParquetStore:
    """Read/write historical OHLCV data as Parquet files."""

    def __init__(self, data_dir: str = "data/historical"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, timeframe: str) -> Path:
        return self.data_dir / f"{symbol}_{timeframe}.parquet"

    def save(self, df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        """Save/append candle data to Parquet file."""
        path = self._path(symbol, timeframe)
        table = pa.Table.from_pandas(df)

        if path.exists():
            existing = pq.read_table(path)
            table = pa.concat_tables([existing, table])
            # Deduplicate by timestamp
            df_combined = table.to_pandas().drop_duplicates(subset=["timestamp"], keep="last")
            df_combined = df_combined.sort_values("timestamp").reset_index(drop=True)
            table = pa.Table.from_pandas(df_combined)

        pq.write_table(table, path, compression="snappy")
        log.info("parquet_saved", symbol=symbol, timeframe=timeframe, rows=table.num_rows)

    def load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Load historical data from Parquet file."""
        path = self._path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"])
        return pq.read_table(path).to_pandas()

    def exists(self, symbol: str, timeframe: str) -> bool:
        return self._path(symbol, timeframe).exists()

    def list_files(self) -> list[str]:
        return [f.stem for f in self.data_dir.glob("*.parquet")]


# ── Redis Cache (Optional) ─────────────────────────────────────────────────


class RedisCache:
    """Optional Redis hot data cache. Degrades gracefully when unavailable."""

    def __init__(self) -> None:
        self._client = None
        self._available = False

    async def init(self) -> None:
        cfg = get_config()
        if not cfg.redis_enabled:
            log.info("redis_disabled", reason="config")
            return

        try:
            import redis.asyncio as aioredis

            self._client = aioredis.Redis(
                host=cfg.redis_host,
                port=cfg.redis_port,
                db=cfg.settings.get("redis", {}).get("db", 0),
                decode_responses=True,
            )
            await self._client.ping()
            self._available = True
            log.info("redis_connected", host=cfg.redis_host, port=cfg.redis_port)
        except Exception as e:
            log.warning("redis_unavailable", error=str(e))
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if not self._available:
            return
        try:
            if ttl:
                await self._client.setex(key, ttl, value)  # type: ignore[union-attr]
            else:
                await self._client.set(key, value)  # type: ignore[union-attr]
        except Exception as e:
            log.warning("redis_set_error", key=key, error=str(e))

    async def get(self, key: str) -> str | None:
        if not self._available:
            return None
        try:
            return await self._client.get(key)  # type: ignore[union-attr]
        except Exception:
            return None

    async def hset(self, name: str, mapping: dict) -> None:
        if not self._available:
            return
        try:
            await self._client.hset(name, mapping=mapping)  # type: ignore[union-attr]
        except Exception as e:
            log.warning("redis_hset_error", name=name, error=str(e))

    async def hgetall(self, name: str) -> dict:
        if not self._available:
            return {}
        try:
            return await self._client.hgetall(name)  # type: ignore[union-attr]
        except Exception:
            return {}

    async def close(self) -> None:
        if self._client:
            await self._client.close()


# ── Unified Storage Facade ─────────────────────────────────────────────────


class Storage:
    """Unified access to all storage tiers."""

    def __init__(self, db_path: str | None = None) -> None:
        self.trade_log = TradeLog(db_path=db_path)
        self.parquet = ParquetStore()
        self.redis = RedisCache()

    async def init(self) -> None:
        await self.trade_log.init()
        await self.redis.init()
        log.info("storage_ready",
                 sqlite=self.trade_log.db_path,
                 redis=self.redis.available)

    async def close(self) -> None:
        await self.trade_log.close()
        await self.redis.close()
