#!/usr/bin/env python3
"""Watchdog — monitors heartbeat.json and kills frozen engine.

Runs independently of the trading engine. Uses ONLY stdlib — no src/ imports.
If the engine freezes (no heartbeat update), the watchdog force-kills it
so launchd can restart it.

Thresholds:
    180s (3 missed heartbeats) → STALE alert
    360s (6 missed heartbeats) → KILL engine process

Writes status to data/watchdog_status.json for SSH checks.
Logs alerts to data/logs/alerts.log.

Usage:
    python3 scripts/watchdog.py
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Config
PROJECT_DIR = Path(__file__).resolve().parent.parent
HEARTBEAT_PATH = PROJECT_DIR / "data" / "heartbeat.json"
PID_PATH = PROJECT_DIR / "data" / "pids" / "engine.pid"
STATUS_PATH = PROJECT_DIR / "data" / "watchdog_status.json"
ALERTS_LOG = PROJECT_DIR / "data" / "logs" / "alerts.log"
CHECK_INTERVAL = 90     # seconds between checks
STALE_THRESHOLD = 180   # seconds before STALE alert
KILL_THRESHOLD = 360    # seconds before force-kill

_running = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_alert(level: str, message: str) -> None:
    """Append alert to alerts.log."""
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "timestamp": _now_iso(),
        "level": level,
        "source": "watchdog",
        "message": message,
    })
    with open(ALERTS_LOG, "a") as f:
        f.write(line + "\n")
    print(f"[{level}] {message}")


def _write_status(status: str, heartbeat_age: float | None, detail: str = "") -> None:
    """Write current watchdog status for SSH checks."""
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": _now_iso(),
        "status": status,
        "heartbeat_age_s": round(heartbeat_age, 1) if heartbeat_age is not None else None,
        "detail": detail,
    }
    STATUS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _get_engine_pid() -> int | None:
    """Read engine PID from pidfile."""
    if not PID_PATH.exists():
        return None
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)  # Check alive
        return pid
    except (ValueError, ProcessLookupError):
        return None


def _kill_engine(pid: int) -> bool:
    """Force-kill engine process."""
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(3)
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)  # Force if still alive
        except ProcessLookupError:
            pass
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        _log_alert("ERROR", f"no permission to kill PID {pid}")
        return False


def _check_heartbeat() -> None:
    """Single check cycle."""
    if not HEARTBEAT_PATH.exists():
        _write_status("NO_HEARTBEAT", None, "heartbeat.json does not exist")
        _log_alert("WARN", "heartbeat.json not found — engine may not be running")
        return

    try:
        mtime = HEARTBEAT_PATH.stat().st_mtime
        age = time.time() - mtime
    except OSError as e:
        _write_status("ERROR", None, str(e))
        return

    if age > KILL_THRESHOLD:
        pid = _get_engine_pid()
        detail = f"heartbeat {age:.0f}s old (>{KILL_THRESHOLD}s)"
        if pid:
            _log_alert("CRITICAL", f"{detail} — killing engine PID {pid}")
            _kill_engine(pid)
            _write_status("KILLED", age, f"killed PID {pid}")
        else:
            _log_alert("CRITICAL", f"{detail} — no engine PID found to kill")
            _write_status("DEAD", age, "engine not running, heartbeat stale")

    elif age > STALE_THRESHOLD:
        _log_alert("WARN", f"heartbeat {age:.0f}s old (>{STALE_THRESHOLD}s) — engine may be frozen")
        _write_status("STALE", age)

    else:
        _write_status("HEALTHY", age)


def _signal_handler(signum, frame):
    global _running
    _running = False


def main() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"Watchdog started — checking every {CHECK_INTERVAL}s")
    print(f"  Heartbeat: {HEARTBEAT_PATH}")
    print(f"  Stale threshold: {STALE_THRESHOLD}s")
    print(f"  Kill threshold: {KILL_THRESHOLD}s")

    _log_alert("INFO", "watchdog started")

    while _running:
        _check_heartbeat()
        # Sleep in small increments for responsive shutdown
        for _ in range(CHECK_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    _log_alert("INFO", "watchdog stopped")
    print("Watchdog stopped.")


if __name__ == "__main__":
    main()
