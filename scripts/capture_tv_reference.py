"""Capture TradingView reference data for cross-validation tests.

Run manually when TradingView Desktop is open:
    python3 scripts/capture_tv_reference.py

Saves OHLCV bars + indicator values to tests/fixtures/tv_reference_btcusdt_1h.json.
This fixture is then used by tests/test_data/test_tv_crossval.py.

Requirements:
    - TradingView Desktop must be running with CDP enabled (port 9222)
    - TradingView MCP server must be configured in ~/.claude.json
    - Chart should be on BTCUSDT 1H (script will set it)

Indicators captured: RSI(14), EMA(9), EMA(21), BB(20), MACD, ATR(14)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "tv_reference_btcusdt_1h.json"


def _call_mcp(tool: str, args: dict | None = None) -> dict:
    """Call a TradingView MCP tool via the Claude CLI.

    This is a placeholder — in practice, capture is done interactively
    via Claude Code with MCP tools. This script documents the process.
    """
    raise NotImplementedError(
        "Run capture interactively via Claude Code with TradingView MCP tools.\n"
        "Steps:\n"
        "  1. Open TradingView Desktop to BTCUSDT 1H\n"
        "  2. Add indicators: RSI(14), EMA(9), EMA(21), BB(20), MACD, ATR(14)\n"
        "  3. Use data_get_ohlcv(count=200) to capture bars\n"
        "  4. Use data_get_study_values() to capture indicator values\n"
        "  5. Save combined JSON to tests/fixtures/tv_reference_btcusdt_1h.json\n"
        "\n"
        "JSON format:\n"
        '  {"captured_at": "2026-04-12T...", "symbol": "BTCUSDT", "timeframe": "1H",\n'
        '   "ohlcv": [...], "indicators": {"RSI_14": ..., "EMA_9": ..., ...}}\n'
    )


if __name__ == "__main__":
    print("TradingView Reference Data Capture")
    print("=" * 50)
    print()
    print("This script documents the capture process.")
    print("Run interactively via Claude Code with TradingView MCP tools.")
    print()
    print(f"Fixture path: {FIXTURE_PATH}")
    print()
    _call_mcp("chart_get_state")
