"""Heartbeat monitor — writes system health to data/heartbeat.json every 60s.

Also checks for:
- Stale data: no candle for 2x timeframe → STALE status
- Kill file: data/KILL exists → activate kill switch via risk client

Usage:
    hb = Heartbeat(engine)
    task = asyncio.create_task(hb.run())
    # ... later ...
    hb.stop()
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import orjson

from src.utils.logger import get_logger

log = get_logger("heartbeat")

HEARTBEAT_PATH = Path("data/heartbeat.json")
KILL_FILE_PATH = Path("data/KILL")
HEARTBEAT_INTERVAL = 60  # seconds


class Heartbeat:
    """Periodic health writer + stale data detector + kill file watcher."""

    def __init__(
        self,
        get_stats: callable,
        risk_client=None,
        stale_threshold_mult: float = 2.0,
        timeframe_seconds: int = 3600,  # default 1h
        heartbeat_path: Path | None = None,
    ):
        """
        Args:
            get_stats: Callable returning dict with tick_count, candle_count,
                       open_positions, equity, last_candle_time.
            risk_client: Optional RiskClient for kill switch activation.
            stale_threshold_mult: Multiplier for timeframe to detect stale data.
            timeframe_seconds: Primary timeframe in seconds.
            heartbeat_path: Override the default heartbeat.json path. Lets
                multiple engines (e.g., crypto + gold) coexist with disjoint
                heartbeat files.
        """
        self._get_stats = get_stats
        self._risk_client = risk_client
        self._stale_threshold = stale_threshold_mult * timeframe_seconds
        self._start_time = time.time()
        self._running = False
        self._kill_active = False
        self._path = heartbeat_path or HEARTBEAT_PATH

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main heartbeat loop — runs until stop() is called."""
        self._running = True
        self._path.parent.mkdir(parents=True, exist_ok=True)

        while self._running:
            try:
                self._write_heartbeat()
                self._check_kill_file()
            except Exception as e:
                log.error("heartbeat_error", error=str(e))

            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def _write_heartbeat(self) -> None:
        """Write current health status to heartbeat.json."""
        stats = self._get_stats()
        now = time.time()
        uptime = now - self._start_time

        last_candle_time = stats.get("last_candle_time", 0)
        candle_age = now - last_candle_time if last_candle_time > 0 else -1

        # Determine status
        strategy_exceptions = stats.get("strategy_exceptions", 0)
        if self._kill_active:
            status = "KILLED"
        elif candle_age > self._stale_threshold and candle_age > 0:
            status = "STALE"
            log.critical("stale_data", candle_age_s=round(candle_age),
                         threshold_s=round(self._stale_threshold))
        elif strategy_exceptions > 5:
            status = "WARNING"
            log.warning("strategy_exceptions_high", count=strategy_exceptions)
        else:
            status = "HEALTHY"

        heartbeat = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_s": round(uptime),
            "tick_count": stats.get("tick_count", 0),
            "candle_count": stats.get("candle_count", 0),
            "open_positions": stats.get("open_positions", 0),
            "equity": stats.get("equity", 0),
            "last_candle_age_s": round(candle_age) if candle_age > 0 else None,
            "risk_server_ok": stats.get("risk_server_ok", False),
            "signal_count": stats.get("signal_count", 0),
            "rejection_count": stats.get("rejection_count", 0),
            "strategy_exceptions": strategy_exceptions,
            "status": status,
        }

        self._path.write_bytes(orjson.dumps(heartbeat, option=orjson.OPT_INDENT_2 | orjson.OPT_SERIALIZE_NUMPY))

    def _check_kill_file(self) -> None:
        """Check for data/KILL file — emergency stop."""
        if KILL_FILE_PATH.exists():
            if not self._kill_active:
                log.critical("kill_file_detected", path=str(KILL_FILE_PATH))
                self._kill_active = True
                if self._risk_client:
                    try:
                        self._risk_client.kill(True, "kill_file")
                    except Exception as e:
                        log.error("kill_switch_failed", error=str(e))
        else:
            if self._kill_active:
                log.info("kill_file_removed", resuming=True)
                self._kill_active = False
                if self._risk_client:
                    try:
                        self._risk_client.kill(False, "kill_file_removed")
                    except Exception as e:
                        log.error("kill_resume_failed", error=str(e))
