"""Scalp Zone Alerter — Dispatches notifications across Terminal, Audio, macOS Desktop, and JSONL log."""

from __future__ import annotations

import datetime
import os
import subprocess
import time
from pathlib import Path
import orjson

from src.strategies.scalping.scalp_zone_detector import ScalpZoneResult
from src.utils.logger import get_logger

log = get_logger("scalp_zone_alerter")


class ScalpZoneAlerter:
    """Dispatches alerts when a qualifying scalp zone is detected."""

    def __init__(
        self,
        cooldown_seconds: int = 300,
        desktop_notifications: bool = True,
        audio_beep: bool = True,
        log_filepath: str = "data/logs/scalp_zones.jsonl",
        tv_screenshot_enabled: bool = False,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.desktop_notifications = desktop_notifications
        self.audio_beep = audio_beep
        self.log_path = Path(log_filepath)
        self.tv_screenshot_enabled = tv_screenshot_enabled

        self._last_alert_ts: dict[str, float] = {}  # direction -> epoch seconds
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def process_result(self, result: ScalpZoneResult) -> bool:
        """Evaluate a detector result and dispatch alert if threshold passed and cooldown elapsed.

        Returns:
            True if an alert was dispatched, False otherwise.
        """
        if not result.is_scalpable:
            return False

        # Use candle timestamp (in seconds) for time tracking (works seamlessly in live & replay)
        event_time_sec = result.timestamp_ms / 1000.0
        last_ts = self._last_alert_ts.get(result.direction, 0.0)
        elapsed = event_time_sec - last_ts

        if elapsed < self.cooldown_seconds:
            # Under cooldown for this direction, log debug and skip noisy alerts
            log.debug(
                "scalp_zone_cooldown_active",
                direction=result.direction,
                score=result.composite_score,
                elapsed_s=round(elapsed),
            )
            return False

        self._last_alert_ts[result.direction] = event_time_sec
        self._dispatch_alert(result)
        return True

    def _dispatch_alert(self, result: ScalpZoneResult) -> None:
        dt_str = datetime.datetime.fromtimestamp(
            result.timestamp_ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        # 1. Terminal Visual Banner
        color = "\033[92m" if result.direction == "LONG" else "\033[91m"  # Green / Red
        bold = "\033[1m"
        reset = "\033[0m"

        banner = (
            f"\n{bold}{color}╔════════════════════════════════════════════════════════════════════════════╗\n"
            f"║  ⚡ GOLD SCALP REGIME DETECTED — {result.direction:5s} BIAS | SCORE: {result.composite_score:.1f}/100 ⚡   ║\n"
            f"╠════════════════════════════════════════════════════════════════════════════╣\n"
            f"║  Time:       {dt_str:<58} ║\n"
            f"║  Price:      ${result.current_close:<10.2f} (Net Move: ${result.net_move_dollars:.2f} / {result.atr_norm_move:.1f}x 1m ATR)       ║\n"
            f"║  Path ER:    {result.er:<10.2f} (Efficiency 1.0=Straight Line)               ║\n"
            f"║  Trend R²:   {result.r_squared:<10.2f} (Linear Fit Quality for Trendlines)          ║\n"
            f"║  Candles:    {result.directional_ratio*100:.0f}% in Direction | Median Body: {result.median_body_pct*100:.0f}%                  ║\n"
            f"║  Strategy:   Wait for smooth leg completion → Scalp 30s Trendline Break   ║\n"
            f"╚════════════════════════════════════════════════════════════════════════════╝{reset}\n"
        )
        print(banner)

        # 2. Audio Beep
        if self.audio_beep:
            try:
                print("\a", end="", flush=True)
            except Exception:
                pass

        # 3. macOS Native Desktop Notification
        if self.desktop_notifications and os.uname().sysname == "Darwin":
            try:
                title = f"Gold Scalp Alert: {result.direction} ({result.composite_score:.0f}/100)"
                msg = (
                    f"${result.net_move_dollars:.2f} move ({result.atr_norm_move:.1f}x ATR). "
                    f"ER: {result.er:.2f}, R²: {result.r_squared:.2f}. Clean trendline ready!"
                )
                apple_script = (
                    f'display notification "{msg}" with title "{title}" sound name "Ping"'
                )
                subprocess.run(["osascript", "-e", apple_script], check=False)
            except Exception as e:
                log.warning("desktop_notification_failed", error=str(e))

        # 4. JSONL Event Log
        event = {
            "timestamp": dt_str,
            "timestamp_ms": int(result.timestamp_ms),
            "direction": str(result.direction),
            "composite_score": float(result.composite_score),
            "net_move_dollars": float(result.net_move_dollars),
            "atr_norm_move": float(result.atr_norm_move),
            "er": float(result.er),
            "r_squared": float(result.r_squared),
            "directional_ratio": float(result.directional_ratio),
            "cv_range": float(result.cv_range),
            "median_body_pct": float(result.median_body_pct),
            "current_close": float(result.current_close),
            "atr_14": float(result.atr_14),
        }
        with self.log_path.open("ab") as f:
            f.write(orjson.dumps(event, option=orjson.OPT_SERIALIZE_NUMPY) + b"\n")

        # 5. Optional TradingView MCP Screenshot
        if self.tv_screenshot_enabled:
            try:
                tv_cli = Path(__file__).resolve().parents[3] / "tradingview-mcp-jackson" / "src" / "cli" / "index.js"
                if tv_cli.exists():
                    subprocess.run(["node", str(tv_cli), "screenshot", "-r", "chart"], check=False)
            except Exception as e:
                log.debug("tv_screenshot_failed", error=str(e))
