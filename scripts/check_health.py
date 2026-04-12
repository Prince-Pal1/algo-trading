#!/usr/bin/env python3
"""Health check — reads heartbeat.json and prints human-readable status.

Exit codes:
    0 = HEALTHY
    1 = WARNING (stale data, high age)
    2 = CRITICAL (killed, no heartbeat, very stale)

Usage:
    python3 scripts/check_health.py
    # Or from phone via SSH:
    ssh mac "cd ~/algo-trading && python3 scripts/check_health.py"
"""

import json
import sys
import time
from pathlib import Path

HEARTBEAT_PATH = Path("data/heartbeat.json")
MAX_HEARTBEAT_AGE = 120  # seconds — heartbeat should update every 60s


def main() -> int:
    if not HEARTBEAT_PATH.exists():
        print("CRITICAL: No heartbeat file found")
        print(f"  Expected: {HEARTBEAT_PATH.resolve()}")
        print("  Is the engine running?")
        return 2

    data = json.loads(HEARTBEAT_PATH.read_text())
    status = data.get("status", "UNKNOWN")
    ts = data.get("timestamp", "")
    uptime = data.get("uptime_s", 0)
    ticks = data.get("tick_count", 0)
    candles = data.get("candle_count", 0)
    positions = data.get("open_positions", 0)
    equity = data.get("equity", 0)
    candle_age = data.get("last_candle_age_s")
    risk_ok = data.get("risk_server_ok", False)

    # Check heartbeat file freshness
    file_age = time.time() - HEARTBEAT_PATH.stat().st_mtime
    if file_age > MAX_HEARTBEAT_AGE:
        status = "STALE_HEARTBEAT"

    # Format uptime
    hours, rem = divmod(uptime, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{int(hours)}h {int(mins)}m {int(secs)}s"

    # Print report
    print(f"  Status:     {status}")
    print(f"  Timestamp:  {ts}")
    print(f"  Uptime:     {uptime_str}")
    print(f"  Ticks:      {ticks:,}")
    print(f"  Candles:    {candles:,}")
    print(f"  Positions:  {positions}")
    print(f"  Equity:     ${equity:,.2f}")
    print(f"  Candle age: {candle_age}s" if candle_age else "  Candle age: N/A")
    print(f"  Risk srv:   {'OK' if risk_ok else 'DOWN'}")
    print(f"  File age:   {int(file_age)}s")

    if status == "HEALTHY":
        return 0
    elif status in ("STALE", "STALE_HEARTBEAT"):
        return 1
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())
