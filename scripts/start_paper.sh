#!/usr/bin/env bash
# Start paper trading: risk server + trading engine
# Usage: ./scripts/start_paper.sh [--symbols BTCUSDT ETHUSDT] [--timeframes 1m 5m]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Directories
LOG_DIR="$PROJECT_DIR/data/logs"
PID_DIR="$PROJECT_DIR/data/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "=== Algo Trading — Paper Mode ==="
echo "  Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  Logs:    $LOG_DIR/"
echo ""

# Start risk server in background with log redirection
echo "[1/2] Starting risk server..."
python3 -m src.risk.server > "$LOG_DIR/risk_server.log" 2>&1 &
RISK_PID=$!
echo "$RISK_PID" > "$PID_DIR/risk_server.pid"
sleep 1

# Check risk server is running
if ! kill -0 "$RISK_PID" 2>/dev/null; then
    echo "ERROR: Risk server failed to start. Check $LOG_DIR/risk_server.log"
    exit 1
fi
echo "  Risk server running (PID $RISK_PID)"

# Cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$RISK_PID" 2>/dev/null || true
    wait "$RISK_PID" 2>/dev/null || true
    rm -f "$PID_DIR/risk_server.pid" "$PID_DIR/engine.pid"
    echo "Done. $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
}
trap cleanup EXIT INT TERM

# Start trading engine (foreground with tee to log file)
echo "[2/2] Starting trading engine..."
echo ""
echo "$$" > "$PID_DIR/engine.pid"
python3 -m src.main "$@" 2>&1 | tee "$LOG_DIR/engine_${TIMESTAMP}.log"
