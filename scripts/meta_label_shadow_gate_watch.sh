#!/bin/bash
# Meta-label live-gate watcher.
#
# Runs `meta_label_shadow_check.py --verbose`, then inspects
# data/meta_label_shadow_status.json to decide whether the live veto gate
# has transitioned from BLOCKED → READY. Fires a macOS notification
# EXACTLY ONCE per transition (idempotency guard via .flag file).
#
# Designed to be invoked every 6h by launchd
# (~/Library/LaunchAgents/com.algo-trading.meta-label-shadow-check.plist).

set -u
cd "$(dirname "$0")/.."

PYTHON=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
[ -x "$PYTHON" ] || PYTHON=python3

FLAG_FILE=data/meta_label_gate_cleared.flag
EVENT_LOG=data/logs/meta_label_gate_events.log
STATUS_JSON=data/meta_label_shadow_status.json

# 1. Run the shadow check (writes STATUS_JSON + report.md + alerts.log).
"$PYTHON" scripts/meta_label_shadow_check.py --verbose
CHECK_RC=$?

# 2. Evaluate the live gate against the fresh JSON.
#    Live gate = overall HEALTHY + at least one strategy with rolling_auc
#    PASS AND n >= 20 (mirrors passes_phase5_live_gate in the script).
GATE_STATE=$("$PYTHON" - "$STATUS_JSON" <<'PY'
import json, sys
path = sys.argv[1]
try:
    d = json.load(open(path))
except Exception:
    print("NO_JSON")
    sys.exit(0)
if d.get("overall_status") != "HEALTHY":
    print("BLOCKED_NOT_HEALTHY")
    sys.exit(0)
for name, checks in d.get("per_strategy", {}).items():
    for c in checks:
        if c.get("name") == "rolling_auc" and c.get("status") == "PASS":
            n = c.get("data", {}).get("n", 0)
            if n >= 20:
                print("READY")
                sys.exit(0)
print("BLOCKED_COLD_START")
PY
)

# 3. Fire one-shot notification if gate just cleared.
if [ "$GATE_STATE" = "READY" ] && [ ! -f "$FLAG_FILE" ]; then
    osascript -e 'display notification "Meta-label live gate ready — run promote_meta_label.sh --to live" with title "Algo Trading"' 2>/dev/null || true
    mkdir -p data/logs
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) GATE_CLEARED (check_rc=$CHECK_RC)" >> "$EVENT_LOG"
    touch "$FLAG_FILE"
fi

# 4. Always log the current state so the .log tail is useful.
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) gate_state=$GATE_STATE check_rc=$CHECK_RC"

exit 0
