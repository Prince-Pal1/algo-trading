"""Risk state — in-memory tracking with SQLite persistence.

Tracks equity, drawdown, PnL at daily/weekly/monthly levels,
open positions, and kill switch state. Persists to risk_state table
so state survives process restarts.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from datetime import datetime, timezone

import orjson

from src.utils.logger import get_logger

log = get_logger("risk_state")


class PositionRiskInfo:
    """Lightweight position info for risk tracking."""

    __slots__ = ("symbol", "strategy", "side", "quantity", "entry_price", "notional")

    def __init__(self, symbol: str, strategy: str, side: str, quantity: float, entry_price: float):
        self.symbol = symbol
        self.strategy = strategy
        self.side = side
        self.quantity = quantity
        self.entry_price = entry_price
        self.notional = quantity * entry_price


class RiskState:
    """In-memory risk state with SQLite persistence."""

    def __init__(self, db_path: str = "data/trades.db"):
        self.db_path = db_path

        # Equity tracking
        self.peak_equity: float = 10_000.0
        self.current_equity: float = 10_000.0

        # PnL tracking (reset on period boundary)
        self.daily_pnl: float = 0.0
        self.weekly_pnl: float = 0.0
        self.monthly_pnl: float = 0.0
        self._daily_start_equity: float = 10_000.0

        # Period boundary tracking
        self._last_daily_reset: str = ""
        self._last_weekly_reset: str = ""
        self._last_monthly_reset: str = ""

        # Position tracking
        self.open_positions: dict[str, PositionRiskInfo] = {}

        # Per-strategy PnL (rolling)
        self.strategy_pnl: dict[str, float] = {}

        # Recent signals for duplicate detection
        self.recent_signals: deque[tuple[str, float]] = deque(maxlen=200)

        # Kill switch
        self.kill_switch_active: bool = False
        self.kill_switch_reason: str = ""

        # Strategy pause flags
        self.strategy_paused: set[str] = set()

        # Operating mode
        self.active_mode: str = "AGGRESSIVE"
        self.custom_multipliers: dict[str, float] = {}

    def load_from_db(self) -> None:
        """Restore state from SQLite on startup."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT key, value FROM risk_state")
            for key, value in cursor.fetchall():
                if key == "peak_equity":
                    self.peak_equity = float(value)
                elif key == "current_equity":
                    self.current_equity = float(value)
                    self._daily_start_equity = float(value)
                elif key == "kill_switch_active":
                    self.kill_switch_active = value == "true"
                elif key == "kill_switch_reason":
                    self.kill_switch_reason = value
                elif key == "strategy_paused":
                    self.strategy_paused = set(orjson.loads(value))
                elif key == "last_daily_reset":
                    self._last_daily_reset = value
                elif key == "last_weekly_reset":
                    self._last_weekly_reset = value
                elif key == "last_monthly_reset":
                    self._last_monthly_reset = value
                elif key == "active_mode":
                    self.active_mode = value
                elif key == "custom_multipliers":
                    self.custom_multipliers = orjson.loads(value)
            conn.close()
            log.info("risk_state_loaded", peak_equity=self.peak_equity,
                     kill_switch=self.kill_switch_active)
        except (sqlite3.OperationalError, FileNotFoundError):
            log.info("risk_state_fresh_start")

    def persist(self) -> None:
        """Write current state to SQLite."""
        if self.db_path == ":memory:":
            return
        try:
            conn = sqlite3.connect(self.db_path)
            now = datetime.now(timezone.utc).isoformat()
            pairs = [
                ("peak_equity", str(self.peak_equity)),
                ("current_equity", str(self.current_equity)),
                ("kill_switch_active", "true" if self.kill_switch_active else "false"),
                ("kill_switch_reason", self.kill_switch_reason),
                ("strategy_paused", orjson.dumps(list(self.strategy_paused)).decode()),
                ("last_daily_reset", self._last_daily_reset),
                ("last_weekly_reset", self._last_weekly_reset),
                ("last_monthly_reset", self._last_monthly_reset),
                ("active_mode", self.active_mode),
                ("custom_multipliers", orjson.dumps(self.custom_multipliers).decode()),
            ]
            for key, value in pairs:
                conn.execute(
                    "INSERT OR REPLACE INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
            conn.commit()
            conn.close()
        except sqlite3.OperationalError as e:
            log.error("risk_state_persist_failed", error=str(e))

    def update_equity(self, equity: float) -> None:
        """Update equity and high water mark."""
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

    def record_trade_pnl(self, strategy: str, pnl: float) -> None:
        """Record PnL from a closed trade."""
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        self.monthly_pnl += pnl
        self.strategy_pnl[strategy] = self.strategy_pnl.get(strategy, 0.0) + pnl

    def get_drawdown_pct(self) -> float:
        """Current drawdown from peak as a fraction (0.0 to 1.0)."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity)

    def get_daily_loss_pct(self) -> float:
        """Daily loss as fraction of daily start equity."""
        if self._daily_start_equity <= 0:
            return 0.0
        return max(0.0, -self.daily_pnl / self._daily_start_equity)

    def get_weekly_loss_pct(self) -> float:
        """Weekly loss as fraction of current equity."""
        if self.current_equity <= 0:
            return 0.0
        return max(0.0, -self.weekly_pnl / self.current_equity)

    def get_monthly_loss_pct(self) -> float:
        """Monthly loss as fraction of current equity."""
        if self.current_equity <= 0:
            return 0.0
        return max(0.0, -self.monthly_pnl / self.current_equity)

    def get_total_exposure(self) -> float:
        """Total notional across all open positions."""
        return sum(p.notional for p in self.open_positions.values())

    def check_period_resets(self, timestamp_ms: int = 0) -> None:
        """Reset PnL counters at period boundaries (UTC).

        Args:
            timestamp_ms: Unix timestamp in milliseconds (backtest mode).
                          When 0, uses wall-clock time (live mode).
        """
        if timestamp_ms > 0:
            now = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        else:
            now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week = now.strftime("%Y-W%W")
        month = now.strftime("%Y-%m")

        if today != self._last_daily_reset:
            self._daily_start_equity = self.current_equity
            self.daily_pnl = 0.0
            self.strategy_pnl.clear()
            self.strategy_paused.clear()
            self._last_daily_reset = today
            log.info("daily_pnl_reset", equity=self.current_equity)

        if week != self._last_weekly_reset:
            self.weekly_pnl = 0.0
            self._last_weekly_reset = week

        if month != self._last_monthly_reset:
            self.monthly_pnl = 0.0
            self._last_monthly_reset = month

    def add_position(self, symbol: str, strategy: str, side: str,
                     quantity: float, entry_price: float) -> None:
        """Track a new open position."""
        self.open_positions[symbol] = PositionRiskInfo(
            symbol=symbol, strategy=strategy, side=side,
            quantity=quantity, entry_price=entry_price,
        )

    def remove_position(self, symbol: str) -> None:
        """Remove a closed position."""
        self.open_positions.pop(symbol, None)

    def record_signal(self, signal_key: str) -> None:
        """Record a signal for duplicate detection."""
        self.recent_signals.append((signal_key, time.time()))
