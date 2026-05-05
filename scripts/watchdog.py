#!/usr/bin/env python3
"""Watchdog — monitors heartbeats for ALL engines and restarts frozen ones.

Runs independently of the trading engines. Uses ONLY stdlib + osascript —
no src/ imports. If an engine freezes (no heartbeat update OR candles stop
flowing), the watchdog uses `launchctl kickstart -k` to force-restart it
via launchd.

Engines monitored:
    com.algo-trading.engine        ← crypto (Binance altcoin universe)
    com.algo-trading.engine-gold   ← gold (IC Markets cTrader XAUUSD)

Thresholds:
    180s file-mtime → STALE warning (3 missed heartbeats)
    360s file-mtime → engine kicked via launchctl
    7200s candle-age → engine kicked via launchctl (handles 1h candles
                         with 2× grace; 30-min threshold spuriously fired
                         every hour because 1h candles naturally age 0-3600s)

2026-05-05 rewrite: previous version only checked one heartbeat, used a
broken PID-file mechanism (no kill ever fired), and had a 1800s candle
threshold that fired CRITICAL every hour with no actual recovery action.
After the 87h gold-engine STALE outage went unnoticed, switched to
multi-engine + launchctl-kickstart + osascript notify.

Writes status to data/watchdog_status.json for SSH checks.
Logs alerts to data/logs/alerts.log.

Usage:
    python3 scripts/watchdog.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATUS_PATH = PROJECT_DIR / "data" / "watchdog_status.json"
ALERTS_LOG = PROJECT_DIR / "data" / "logs" / "alerts.log"

CHECK_INTERVAL = 90               # seconds between checks
HEARTBEAT_STALE_WARN = 180        # 3 missed heartbeats — warn
HEARTBEAT_KICK_THRESHOLD = 360    # 6 missed heartbeats — kick
CANDLE_STALE_KICK_THRESHOLD = 7200  # 2h — handles 1h-candle natural cadence with grace

ENGINES = [
    {
        "label": "crypto",
        "heartbeat_path": PROJECT_DIR / "data" / "heartbeat.json",
        "launchd_label": "com.algo-trading.engine",
    },
    {
        "label": "gold",
        "heartbeat_path": PROJECT_DIR / "data" / "heartbeat_gold.json",
        "launchd_label": "com.algo-trading.engine-gold",
    },
]

# Suppress repeat kicks: do not kick the same engine more than once per N seconds.
KICK_COOLDOWN_S = 600

_running = True
_last_kick_at: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_alert(level: str, message: str, engine: str | None = None) -> None:
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "timestamp": _now_iso(),
        "level": level,
        "source": "watchdog",
        "engine": engine,
        "message": message,
    })
    with open(ALERTS_LOG, "a") as f:
        f.write(line + "\n")
    tag = f"[{level}]" + (f"[{engine}]" if engine else "")
    print(f"{tag} {message}")


def _macos_notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            check=False, timeout=5,
        )
    except Exception:
        pass


def _kick_engine(launchd_label: str) -> bool:
    """Kick the engine via launchd — kills + lets KeepAlive respawn."""
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{launchd_label}"],
            check=False, timeout=15, capture_output=True, text=True,
        )
        if result.returncode != 0:
            _log_alert("ERROR", f"kickstart failed rc={result.returncode}: {result.stderr.strip()}",
                       engine=launchd_label)
            return False
        return True
    except Exception as e:
        _log_alert("ERROR", f"kickstart exception: {e}", engine=launchd_label)
        return False


def _read_candle_age_s(heartbeat_path: Path) -> float | None:
    try:
        data = json.loads(heartbeat_path.read_text())
        age = data.get("last_candle_age_s")
        return float(age) if isinstance(age, (int, float)) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _check_engine(engine: dict) -> dict:
    label = engine["label"]
    hb_path: Path = engine["heartbeat_path"]
    launchd_label = engine["launchd_label"]
    now = time.time()

    if not hb_path.exists():
        _log_alert("WARN", f"heartbeat file missing: {hb_path}", engine=label)
        return {"label": label, "status": "NO_HEARTBEAT", "heartbeat_age_s": None}

    try:
        mtime = hb_path.stat().st_mtime
        hb_age = now - mtime
    except OSError as e:
        _log_alert("ERROR", f"stat failed: {e}", engine=label)
        return {"label": label, "status": "ERROR", "heartbeat_age_s": None}

    candle_age = _read_candle_age_s(hb_path)

    # File-mtime kick (process liveness)
    if hb_age > HEARTBEAT_KICK_THRESHOLD:
        last = _last_kick_at.get(label, 0.0)
        if now - last > KICK_COOLDOWN_S:
            _log_alert("CRITICAL",
                       f"heartbeat file {hb_age:.0f}s old (>{HEARTBEAT_KICK_THRESHOLD}s) — kicking",
                       engine=label)
            _macos_notify(f"algo-trading: {label} engine STALE",
                          f"heartbeat {hb_age:.0f}s — kicking via launchctl")
            if _kick_engine(launchd_label):
                _last_kick_at[label] = now
        return {"label": label, "status": "KICKED_HB_STALE", "heartbeat_age_s": hb_age,
                "candle_age_s": candle_age}

    # Candle-age kick (CandleBuilder liveness — feed alive but no candles)
    if candle_age is not None and candle_age > CANDLE_STALE_KICK_THRESHOLD:
        last = _last_kick_at.get(label, 0.0)
        if now - last > KICK_COOLDOWN_S:
            _log_alert("CRITICAL",
                       f"candle {candle_age:.0f}s stale (>{CANDLE_STALE_KICK_THRESHOLD}s) — kicking",
                       engine=label)
            _macos_notify(f"algo-trading: {label} candles STALE",
                          f"last candle {candle_age:.0f}s ago — kicking via launchctl")
            if _kick_engine(launchd_label):
                _last_kick_at[label] = now
        return {"label": label, "status": "KICKED_CANDLE_STALE", "heartbeat_age_s": hb_age,
                "candle_age_s": candle_age}

    # Soft warning
    if hb_age > HEARTBEAT_STALE_WARN:
        _log_alert("WARN", f"heartbeat {hb_age:.0f}s old (>{HEARTBEAT_STALE_WARN}s)", engine=label)
        return {"label": label, "status": "STALE", "heartbeat_age_s": hb_age,
                "candle_age_s": candle_age}

    return {"label": label, "status": "HEALTHY", "heartbeat_age_s": hb_age,
            "candle_age_s": candle_age}


def _write_status(per_engine: list[dict]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    overall = "HEALTHY"
    for e in per_engine:
        if e["status"] in ("KICKED_HB_STALE", "KICKED_CANDLE_STALE"):
            overall = "DEGRADED"
        elif e["status"] in ("STALE", "NO_HEARTBEAT", "ERROR") and overall == "HEALTHY":
            overall = "WARN"
    data = {
        "timestamp": _now_iso(),
        "overall": overall,
        "engines": per_engine,
    }
    STATUS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _check_all() -> None:
    results = []
    for eng in ENGINES:
        try:
            results.append(_check_engine(eng))
        except Exception as e:
            _log_alert("ERROR", f"check exception: {e}", engine=eng["label"])
            results.append({"label": eng["label"], "status": "ERROR", "error": str(e)})
    _write_status(results)


def _signal_handler(signum, frame):
    global _running
    _running = False


def main() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"Watchdog v2 started — checking every {CHECK_INTERVAL}s")
    for e in ENGINES:
        print(f"  {e['label']}: {e['heartbeat_path']} → {e['launchd_label']}")
    print(f"  Heartbeat kick threshold: {HEARTBEAT_KICK_THRESHOLD}s")
    print(f"  Candle-age kick threshold: {CANDLE_STALE_KICK_THRESHOLD}s")

    _log_alert("INFO", "watchdog v2 started")

    while _running:
        _check_all()
        for _ in range(CHECK_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    _log_alert("INFO", "watchdog stopped")
    print("Watchdog stopped.")


if __name__ == "__main__":
    main()
