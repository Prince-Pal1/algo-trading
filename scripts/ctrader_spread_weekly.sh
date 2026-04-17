#!/usr/bin/env bash
# Weekly Monday workflow: capture XAUUSD spreads through London/NY peak
# overlap, then auto-generate the calibration report.
#
# Invoked by ~/Library/LaunchAgents/com.algo-trading.spread-sampler.plist
# every Monday at 18:30 IST (= 13:00 UTC).

set -euo pipefail
cd /Users/prince/algo-trading

PYTHON=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
LOG=data/logs/spread_weekly.log

mkdir -p data/logs

echo "" >> "$LOG"
echo "=== weekly run starting $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG"

# 1) Capture 240 minutes of XAUUSD ticks through the overlap.
"$PYTHON" -u scripts/ctrader_spread_sampler.py --minutes 240 --symbol XAUUSD >> "$LOG" 2>&1

# 2) Generate the calibration report from ALL captured CSVs (the analyzer
#    globs data/ctrader_spread_samples_XAUUSD_*.csv, so each weekly run
#    enriches the historical comparison automatically).
"$PYTHON" -u scripts/ctrader_spread_report.py >> "$LOG" 2>&1

echo "=== weekly run complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG"
echo "Report: $(pwd)/data/ctrader_spread_report.md" >> "$LOG"
