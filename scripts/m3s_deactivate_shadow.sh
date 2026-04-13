#!/usr/bin/env bash
# M3S Shadow-Mode Deactivation — reverse of m3s_activate_shadow.sh.
#
# Flips enabled=false, unloads the shadow-check launchd agent, restarts
# the engine. Leaves the shadow report/status files in place for audit.

set -euo pipefail

REPO="/Users/prince/algo-trading"
SETTINGS="$REPO/config/settings.toml"
PLIST_DST="$HOME/Library/LaunchAgents/com.algo-trading.m3s-shadow-check.plist"
ENGINE_LABEL="com.algo-trading.engine"

echo "[1/3] Disabling M3S in config/settings.toml..."
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
                lines[i] = "enabled = false\n"
                changed = True
                break
if not changed:
    raise RuntimeError("failed to locate [m3s] enabled line")
path.write_text("".join(lines))

parsed = tomllib.loads(path.read_text())
assert parsed["m3s"]["enabled"] is False, "sanity check failed"
print("   ✓ [m3s] enabled = false (verified with tomllib)")
PY

echo "[2/3] Unloading shadow-check launchd agent..."
if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm "$PLIST_DST"
    echo "   ✓ agent removed"
else
    echo "   ⚠ agent not installed — nothing to remove"
fi

echo "[3/3] Restarting engine..."
if launchctl list | grep -q "$ENGINE_LABEL"; then
    launchctl kickstart -k "gui/$(id -u)/$ENGINE_LABEL"
    echo "   ✓ engine restarted"
else
    echo "   ⚠ engine not under launchd — restart manually if needed"
fi

echo ""
echo "Done. M3S is back to disabled. Shadow report files preserved in data/ for audit."
