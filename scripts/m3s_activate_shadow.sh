#!/usr/bin/env bash
# M3S Shadow-Mode Activation Helper
#
# Runs a 5-step flip-and-verify procedure:
#   1. Flip config/settings.toml [m3s] enabled = true (shadow_mode stays true)
#   2. Restart the trading engine via launchd
#   3. Install + load the shadow-check launchd agent (runs every 6h)
#   4. Fire an immediate shadow check to seed data/m3s_shadow_report.md
#   5. Print the report path and current engine status
#
# Reversible: scripts/m3s_deactivate_shadow.sh flips everything back.

set -euo pipefail

REPO="/Users/prince/algo-trading"
SETTINGS="$REPO/config/settings.toml"
PLIST_SRC="$REPO/scripts/launchd/com.algo-trading.m3s-shadow-check.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.algo-trading.m3s-shadow-check.plist"
ENGINE_LABEL="com.algo-trading.engine"

echo "[1/5] Enabling M3S shadow mode in config/settings.toml..."
# Section-aware TOML edit — only touches [m3s] section's `enabled` line.
python3 - <<'PY'
from pathlib import Path
import tomllib

path = Path("/Users/prince/algo-trading/config/settings.toml")
lines = path.read_text().splitlines(keepends=True)
current_section = None
changed = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        current_section = stripped[1:-1]
        continue
    if current_section == "m3s" and stripped.startswith("enabled"):
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == "enabled":
                lines[i] = "enabled = true\n"
                changed = True
                break
if not changed:
    raise RuntimeError("failed to locate [m3s] enabled line")
path.write_text("".join(lines))

# Parse-check the result so we know the file is still valid TOML.
parsed = tomllib.loads(path.read_text())
assert parsed["m3s"]["enabled"] is True, "sanity check failed"
print("   ✓ [m3s] enabled = true (verified with tomllib)")
PY

echo "[2/5] Restarting trading engine via launchd..."
if launchctl list | grep -q "$ENGINE_LABEL"; then
    launchctl kickstart -k "gui/$(id -u)/$ENGINE_LABEL"
    echo "   ✓ engine restarted"
else
    echo "   ⚠ engine not running under launchd — skipping restart"
    echo "     If you run the engine manually, restart it yourself."
fi

echo "[3/5] Installing shadow-check launchd agent..."
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "   ✓ loaded: com.algo-trading.m3s-shadow-check (runs every 6h)"

echo "[4/5] Firing immediate shadow check..."
cd "$REPO"
python3 scripts/m3s_shadow_check.py --verbose || true
echo ""

echo "[5/5] Status files:"
echo "   Report:  $REPO/data/m3s_shadow_report.md"
echo "   JSON:    $REPO/data/m3s_shadow_status.json"
echo "   Alerts:  $REPO/data/m3s_shadow_alerts.log"
echo "   Clock:   $REPO/data/m3s_shadow_clock.json"
echo ""
echo "Done. Run anytime:"
echo "   cat $REPO/data/m3s_shadow_report.md"
echo "   python3 $REPO/scripts/m3s_shadow_check.py --verbose"
echo ""
echo "To deactivate:"
echo "   $REPO/scripts/m3s_deactivate_shadow.sh"
