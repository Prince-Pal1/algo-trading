#!/usr/bin/env python3
"""Pre-flight validation — run before every paper trading launch.

Checks all prerequisites: config, data, risk server, WS connectivity,
disk, database, kill file, stale PIDs.

Exit codes:
    0 = all checks pass
    1 = warnings only (non-blocking)
    2 = any failure (do NOT launch)

Usage:
    python3 scripts/preflight.py
"""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import sys
from pathlib import Path

# Add project root to path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

# ── Result tracking ──

_results: list[tuple[str, str, str]] = []  # (status, check_name, detail)


def _pass(name: str, detail: str = "") -> None:
    _results.append(("PASS", name, detail))
    print(f"  ✓ PASS  {name}" + (f" — {detail}" if detail else ""))


def _warn(name: str, detail: str = "") -> None:
    _results.append(("WARN", name, detail))
    print(f"  ⚠ WARN  {name}" + (f" — {detail}" if detail else ""))


def _fail(name: str, detail: str = "") -> None:
    _results.append(("FAIL", name, detail))
    print(f"  ✗ FAIL  {name}" + (f" — {detail}" if detail else ""))


# ── Checks ──

def check_config() -> list[tuple[str, str]]:
    """Parse strategies.toml and return list of (symbol, timeframe) pairs."""
    try:
        from src.utils.config import get_config
        cfg = get_config()
        pairs = []
        for name, strat in cfg.strategies.items():
            if not strat.get("enabled", False):
                continue
            tf = strat.get("timeframe", "1h")
            for market in strat.get("markets", []):
                pairs.append((market.upper(), tf))
        _pass("config", f"{len(pairs)} symbol-timeframe pairs from enabled strategies")
        return pairs
    except Exception as e:
        _fail("config", str(e))
        return []


def check_data(pairs: list[tuple[str, str]]) -> None:
    """Verify parquet files exist with enough candles."""
    if not pairs:
        _warn("data", "no pairs to check (config failed?)")
        return

    seen = set()
    missing = []
    short = []
    ok_count = 0

    for symbol, tf in pairs:
        key = f"{symbol}_{tf}"
        if key in seen:
            continue
        seen.add(key)

        path = PROJECT_DIR / "data" / "historical" / f"{key}.parquet"
        if not path.exists():
            missing.append(key)
            continue

        try:
            import pandas as pd
            df = pd.read_parquet(path)
            if len(df) < 200:
                short.append(f"{key}({len(df)})")
            else:
                ok_count += 1
        except Exception as e:
            missing.append(f"{key}(corrupt: {e})")

    if missing:
        _fail("data", f"missing: {', '.join(missing)}")
    if short:
        _warn("data", f"< 200 candles: {', '.join(short)}")
    if not missing and not short:
        _pass("data", f"{ok_count} parquet files OK")


def check_risk_server() -> None:
    """Check if risk server is reachable via ZMQ."""
    try:
        import zmq
        import msgspec
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 2000)
        sock.setsockopt(zmq.SNDTIMEO, 2000)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect("tcp://127.0.0.1:5555")
        sock.send(msgspec.json.encode({"cmd": "STATUS"}))
        reply = msgspec.json.decode(sock.recv())
        sock.close()
        ctx.term()
        _pass("risk_server", f"connected — kill_switch={reply.get('kill_switch_active', '?')}")
    except ImportError:
        _warn("risk_server", "zmq not installed — cannot check")
    except Exception as e:
        _fail("risk_server", f"unreachable: {e}")


def check_binance_ws() -> None:
    """Quick WS connectivity test."""
    try:
        import asyncio
        import websockets
        import certifi
        import ssl

        async def _test():
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
            async with websockets.connect(url, ssl=ssl_ctx, close_timeout=3) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                return len(msg) > 0

        result = asyncio.run(_test())
        if result:
            _pass("binance_ws", "connected, receiving data")
        else:
            _fail("binance_ws", "connected but no data received")
    except ImportError as e:
        _warn("binance_ws", f"missing dependency: {e}")
    except Exception as e:
        _fail("binance_ws", f"connection failed: {e}")


def check_disk() -> None:
    """Check disk space on data/ partition."""
    data_dir = PROJECT_DIR / "data"
    if not data_dir.exists():
        _warn("disk", "data/ directory does not exist")
        return

    usage = shutil.disk_usage(str(data_dir))
    free_mb = usage.free / (1024 * 1024)
    if free_mb < 500:
        _warn("disk", f"only {free_mb:.0f}MB free on data/ partition")
    else:
        _pass("disk", f"{free_mb:.0f}MB free")


def check_kill_file() -> None:
    """Ensure data/KILL does not exist."""
    kill_path = PROJECT_DIR / "data" / "KILL"
    if kill_path.exists():
        _fail("kill_file", "data/KILL exists — remove it before launching")
    else:
        _pass("kill_file", "absent")


def check_database() -> None:
    """Verify trades.db is accessible."""
    db_path = PROJECT_DIR / "data" / "trades.db"
    if not db_path.exists():
        _warn("database", "trades.db does not exist (will be created on startup)")
        return
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("SELECT 1")
        size_mb = db_path.stat().st_size / (1024 * 1024)
        conn.close()
        _pass("database", f"trades.db accessible ({size_mb:.1f}MB)")
    except Exception as e:
        _fail("database", f"SQLite error: {e}")


def check_pid_files() -> None:
    """Check for stale PID files."""
    pid_dir = PROJECT_DIR / "data" / "pids"
    if not pid_dir.exists():
        _pass("pid_files", "no pid directory")
        return

    stale = []
    for pid_file in pid_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process alive (signal 0 = no-op)
        except ProcessLookupError:
            stale.append(f"{pid_file.name}(PID {pid} dead)")
        except (ValueError, PermissionError):
            pass

    if stale:
        _warn("pid_files", f"stale: {', '.join(stale)} — clean up before launch")
    else:
        _pass("pid_files", "no stale PIDs")


# ── Main ──

def main() -> None:
    print(f"\n  Pre-Flight Check — {PROJECT_DIR.name}")
    print(f"  {'=' * 50}\n")

    pairs = check_config()
    check_data(pairs)
    check_risk_server()
    check_binance_ws()
    check_disk()
    check_kill_file()
    check_database()
    check_pid_files()

    print(f"\n  {'=' * 50}")
    fails = sum(1 for s, _, _ in _results if s == "FAIL")
    warns = sum(1 for s, _, _ in _results if s == "WARN")
    passes = sum(1 for s, _, _ in _results if s == "PASS")

    if fails:
        print(f"  RESULT: {fails} FAIL, {warns} WARN, {passes} PASS — DO NOT LAUNCH")
        sys.exit(2)
    elif warns:
        print(f"  RESULT: {warns} WARN, {passes} PASS — launch with caution")
        sys.exit(1)
    else:
        print(f"  RESULT: {passes} PASS — all clear, ready to launch")
        sys.exit(0)


if __name__ == "__main__":
    main()
