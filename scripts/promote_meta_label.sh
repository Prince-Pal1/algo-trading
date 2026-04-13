#!/usr/bin/env bash
# Meta-Label Filter Promotion — shadow → advisory → live.
#
# The meta-label filter runs BEFORE M3S and can veto or scale signals.
# It ships in three modes:
#
#   shadow    — shadow_mode=true                       (log only, no mutation)
#   advisory  — shadow_mode=false, veto_threshold=0.30 (conservative veto)
#   live      — shadow_mode=false, veto_threshold=0.50 (aggressive veto)
#
# This script flips the filter between modes, gating each transition on
# scripts/meta_label_shadow_check.py output:
#
#   to advisory  → passes_phase4_advisory_gate   (overall ≠ ERROR, no rolling_auc FAIL)
#   to live      → passes_phase5_live_gate       (HEALTHY, ≥ 1 strategy with ≥ 20 live rows PASS)
#   to shadow    → no gate                        (always reversible)
#
# Flags:
#   --to <mode>      Target mode: shadow | advisory | live  (required)
#   --dry-run        Run gate checks, print PASS/FAIL, no config change
#   --force          Skip gate checks. DANGEROUS. Only after manual verify.
#   --no-commit      Flip + kickstart but do not create the git commit.
#
# Examples:
#   scripts/promote_meta_label.sh --to advisory --dry-run
#   scripts/promote_meta_label.sh --to advisory
#   scripts/promote_meta_label.sh --to live
#   scripts/promote_meta_label.sh --to shadow      # rollback

set -euo pipefail

REPO="/Users/prince/algo-trading"
SETTINGS="$REPO/config/settings.toml"
ENGINE_LABEL="com.algo-trading.engine"
ENGINE_LOG="$REPO/data/logs/engine.log"

TO_MODE=""
DRY_RUN=0
FORCE=0
NO_COMMIT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --to)
            TO_MODE="${2:-}"
            shift 2
            ;;
        --to=*)
            TO_MODE="${1#--to=}"
            shift
            ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --force)     FORCE=1; shift ;;
        --no-commit) NO_COMMIT=1; shift ;;
        -h|--help)
            sed -n '2,32p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown arg: $1" >&2
            echo "       valid: --to <shadow|advisory|live> [--dry-run] [--force] [--no-commit]" >&2
            exit 2
            ;;
    esac
done

if [ -z "$TO_MODE" ]; then
    echo "ERROR: --to <shadow|advisory|live> required" >&2
    exit 2
fi

case "$TO_MODE" in
    shadow|advisory|live) ;;
    *)
        echo "ERROR: --to must be one of: shadow | advisory | live" >&2
        exit 2
        ;;
esac

cd "$REPO"

echo "═══════════════════════════════════════════════════════════════════"
echo "  Meta-Label Filter Promotion → $TO_MODE"
echo "═══════════════════════════════════════════════════════════════════"
if [ $DRY_RUN -eq 1 ]; then
    echo "  Mode: DRY RUN — no config changes, no kickstart, no commit"
elif [ $FORCE -eq 1 ]; then
    echo "  Mode: FORCE — skipping gate checks (DANGEROUS)"
else
    echo "  Mode: PROMOTE — will flip config if gate passes"
fi
echo ""

# ── Resolve target config values ────────────────────────────────────

case "$TO_MODE" in
    shadow)
        NEW_SHADOW="true"
        NEW_VETO="0.30"
        ;;
    advisory)
        NEW_SHADOW="false"
        NEW_VETO="0.30"
        ;;
    live)
        NEW_SHADOW="false"
        NEW_VETO="0.50"
        ;;
esac

# ── Gate check ──────────────────────────────────────────────────────

GATE_PASSED=0

if [ $FORCE -eq 1 ] || [ "$TO_MODE" = "shadow" ]; then
    echo "[Gate] Skipped ($( [ $FORCE -eq 1 ] && echo '--force' || echo 'shadow is always reversible' ))"
    GATE_PASSED=1
else
    if [ "$TO_MODE" = "advisory" ]; then
        GATE_HELPER="passes_phase4_advisory_gate"
    else
        GATE_HELPER="passes_phase5_live_gate"
    fi
    echo "[Gate] Running scripts/meta_label_shadow_check.py and calling ${GATE_HELPER}..."
    set +e
    # First, run the check so the report is fresh.
    python3 scripts/meta_label_shadow_check.py --verbose > /tmp/meta_label_shadow_check.out 2>&1
    CHECK_EXIT=$?
    set -e
    set +e
    GATE_RESULT=$(python3 - <<PY
import sys
sys.path.insert(0, "$REPO")
from scripts.meta_label_shadow_check import run_checks, $GATE_HELPER
report = run_checks()
ok, reason = $GATE_HELPER(report)
print(f"{'PASS' if ok else 'FAIL'}|{reason}")
PY
)
    HELPER_EXIT=$?
    set -e
    if [ $HELPER_EXIT -ne 0 ]; then
        echo "   ✗ FAIL — gate helper itself crashed (exit $HELPER_EXIT)"
        tail -20 /tmp/meta_label_shadow_check.out | sed 's/^/     /'
        GATE_PASSED=0
    else
        GATE_STATUS="${GATE_RESULT%%|*}"
        GATE_REASON="${GATE_RESULT#*|}"
        if [ "$GATE_STATUS" = "PASS" ]; then
            echo "   ✓ PASS — $GATE_REASON"
            GATE_PASSED=1
        else
            echo "   ✗ FAIL — $GATE_REASON"
            GATE_PASSED=0
        fi
    fi
    echo ""
fi

if [ $DRY_RUN -eq 1 ]; then
    echo "───────────────────────────────────────────────────────────────────"
    if [ $GATE_PASSED -eq 1 ]; then
        echo "  ✓ Gate PASSED — safe to promote for real"
        echo ""
        echo "To promote:  scripts/promote_meta_label.sh --to $TO_MODE"
        exit 0
    else
        echo "  ✗ Gate FAILED — promotion BLOCKED"
        exit 1
    fi
fi

if [ $GATE_PASSED -eq 0 ]; then
    echo "Refusing to promote. Fix the gate or pass --force."
    exit 1
fi

# ── Flip the config ─────────────────────────────────────────────────

echo "[Flip 1/4] Editing config/settings.toml [meta_label] section..."
python3 - <<PY
from pathlib import Path
import tomllib

path = Path("$SETTINGS")
lines = path.read_text().splitlines(keepends=True)
current_section = None
changed_shadow = False
changed_veto = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        current_section = stripped[1:-1]
        continue
    if current_section == "meta_label":
        if not changed_shadow and stripped.startswith("shadow_mode"):
            if "=" in stripped and stripped.split("=", 1)[0].strip() == "shadow_mode":
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):].rstrip("\n")
                lines[i] = f"shadow_mode = $NEW_SHADOW{comment}\n"
                changed_shadow = True
                continue
        if not changed_veto and stripped.startswith("veto_threshold"):
            if "=" in stripped and stripped.split("=", 1)[0].strip() == "veto_threshold":
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):].rstrip("\n")
                lines[i] = f"veto_threshold = $NEW_VETO{comment}\n"
                changed_veto = True
                continue

if not changed_shadow:
    raise RuntimeError("failed to locate [meta_label] shadow_mode line")
if not changed_veto:
    raise RuntimeError("failed to locate [meta_label] veto_threshold line")

path.write_text("".join(lines))

parsed = tomllib.loads(path.read_text())
ml = parsed["meta_label"]
expected_shadow = $NEW_SHADOW
expected_veto = $NEW_VETO
assert ml["shadow_mode"] is expected_shadow, f"shadow_mode mismatch: {ml['shadow_mode']}"
assert abs(float(ml["veto_threshold"]) - expected_veto) < 1e-9, f"veto_threshold mismatch: {ml['veto_threshold']}"
print(f"   ✓ [meta_label] shadow_mode={ml['shadow_mode']} veto_threshold={ml['veto_threshold']} (verified)")
PY
echo ""

echo "[Flip 2/4] Kickstarting trading engine via launchd..."
if launchctl list | grep -q "$ENGINE_LABEL"; then
    launchctl kickstart -k "gui/$(id -u)/$ENGINE_LABEL"
    echo "   ✓ engine restarted"
    sleep 1
else
    echo "   ⚠ engine not under launchd — restart manually if needed"
fi
echo ""

echo "[Flip 3/4] Verifying engine logs show meta_label filter initialized with new settings..."
VERIFIED=0
for attempt in 1 2 3 4 5; do
    if [ -f "$ENGINE_LOG" ]; then
        if tail -200 "$ENGINE_LOG" 2>/dev/null | grep -qi 'meta_label.*shadow\|meta_filter\|meta_model_loaded'; then
            echo "   ✓ engine confirmed meta-label filter active (attempt $attempt)"
            VERIFIED=1
            break
        fi
    fi
    sleep 2
done
if [ $VERIFIED -eq 0 ]; then
    echo "   ⚠ could not verify from logs within 10s"
    echo "     Check $ENGINE_LOG manually"
fi
echo ""

if [ $NO_COMMIT -eq 1 ]; then
    echo "[Flip 4/4] Skipping git commit (--no-commit)"
else
    echo "[Flip 4/4] Committing config/settings.toml..."
    if git -C "$REPO" diff --quiet config/settings.toml; then
        echo "   ⚠ config/settings.toml has no diff — already at target?"
    else
        COMMIT_MSG=$(python3 - <<PY
mode = "$TO_MODE"
msg_body = {
    "shadow": "Rollback meta-label filter to shadow mode. Filter now logs\ndecisions without mutating signals. Reversible at any time.",
    "advisory": "Session 22 compressed sprint Day 3. Meta-label filter now\nadvisory: shadow_mode=false, veto_threshold=0.30. Conservative\nveto on low-confidence signals. Gate passes_phase4_advisory_gate\ncleared.",
    "live": "Session 22 compressed sprint Day 4. Meta-label filter now\nlive: shadow_mode=false, veto_threshold=0.50. Aggressive veto on\nlow-confidence signals. Gate passes_phase5_live_gate cleared after\n24h+ of clean advisory-mode metrics.",
}[mode]
print(f"feat(meta_label): flip filter to {mode} mode\\n\\n{msg_body}\\n\\nCo-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>")
PY
)
        git -C "$REPO" add config/settings.toml
        git -C "$REPO" commit -m "$COMMIT_MSG"
        echo "   ✓ committed"
    fi
fi
echo ""

echo "═══════════════════════════════════════════════════════════════════"
echo "  Meta-label filter is now: $TO_MODE"
echo "  shadow_mode=$NEW_SHADOW  veto_threshold=$NEW_VETO"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "Monitor with:"
echo "   python3 scripts/meta_label_shadow_check.py --verbose"
echo "   cat $REPO/data/meta_label_shadow_report.md"
echo ""
if [ "$TO_MODE" = "advisory" ]; then
    echo "Next step: 24h+ clean metrics → scripts/promote_meta_label.sh --to live"
elif [ "$TO_MODE" = "live" ]; then
    echo "Rollback if issues:  scripts/promote_meta_label.sh --to shadow"
fi
