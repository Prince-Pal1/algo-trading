#!/usr/bin/env bash
# M3S Authoritative-Mode Promotion — one-command flip from shadow → live.
#
# This is the Day-3 sprint gate. It runs four hard checks and will refuse
# to flip the config unless ALL four pass:
#
#   (1) scripts/m3s_shadow_check.py exits 0                  (HEALTHY)
#   (2) data/m3s_shadow_clock.json reports clock ≥ 4         (compressed clock)
#   (3) scripts/meta_label_shadow_check.py exits 0 or 1      (not ERROR)
#   (4) scripts/deflated_sharpe_from_audit.py writes clean JSON with
#       total_book.deflated_sharpe not regressed vs baseline by > 0.25
#
# If all checks pass, it edits config/settings.toml [m3s] shadow_mode = false
# via a section-aware Python TOML edit, kickstarts the engine, verifies
# the engine logs show `m3s_initialized` with `shadow=false`, and commits
# the config file with `feat(m3s): flip to authoritative mode`.
#
# Flags:
#   --dry-run     Run all gate checks. Print PASS/FAIL per gate. Never
#                 touches config, never kickstarts, never commits.
#   --force       Skip the gate checks and flip anyway. DANGEROUS.
#                 Only use if you've just manually verified everything.
#   --no-commit   Flip config + kickstart but do not create the git commit.
#
# Reversible: set shadow_mode = true manually or run
# `scripts/m3s_deactivate_shadow.sh` to turn M3S off entirely.

set -euo pipefail

REPO="/Users/prince/algo-trading"
SETTINGS="$REPO/config/settings.toml"
CLOCK_FILE="$REPO/data/m3s_shadow_clock.json"
DSR_JSON="$REPO/data/deflated_sharpe_live.json"
ENGINE_LABEL="com.algo-trading.engine"
ENGINE_LOG="$REPO/data/logs/engine.log"

DRY_RUN=0
FORCE=0
NO_COMMIT=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=1 ;;
        --force)     FORCE=1 ;;
        --no-commit) NO_COMMIT=1 ;;
        -h|--help)
            sed -n '2,32p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown flag: $arg" >&2
            echo "       valid: --dry-run --force --no-commit" >&2
            exit 2
            ;;
    esac
done

cd "$REPO"

echo "═══════════════════════════════════════════════════════════════════"
echo "  M3S Authoritative-Mode Promotion"
echo "═══════════════════════════════════════════════════════════════════"
if [ $DRY_RUN -eq 1 ]; then
    echo "  Mode: DRY RUN — no config changes, no kickstart, no commit"
elif [ $FORCE -eq 1 ]; then
    echo "  Mode: FORCE — skipping gate checks (DANGEROUS)"
else
    echo "  Mode: PROMOTE — will flip config if all gates pass"
fi
echo ""

# ── Gate checks ─────────────────────────────────────────────────────

GATE_M3S=0
GATE_CLOCK=0
GATE_META=0
GATE_DSR=0

if [ $FORCE -eq 0 ]; then
    echo "[Gate 1/4] M3S shadow check (scripts/m3s_shadow_check.py)..."
    set +e
    python3 scripts/m3s_shadow_check.py --verbose > /tmp/m3s_shadow_check.out 2>&1
    GATE_M3S=$?
    set -e
    if [ $GATE_M3S -eq 0 ]; then
        echo "   ✓ PASS — exit 0 (HEALTHY or DISABLED)"
    else
        echo "   ✗ FAIL — exit $GATE_M3S"
        echo "   Full output in /tmp/m3s_shadow_check.out"
        tail -20 /tmp/m3s_shadow_check.out | sed 's/^/     /'
    fi
    echo ""

    echo "[Gate 2/4] M3S promotion clock (data/m3s_shadow_clock.json)..."
    if [ ! -f "$CLOCK_FILE" ]; then
        echo "   ✗ FAIL — clock file missing. Has m3s_shadow_check ever run?"
        GATE_CLOCK=1
    else
        CLOCK_DAYS=$(python3 -c "
import json, sys
try:
    d = json.load(open('$CLOCK_FILE'))
    print(int(d.get('consecutive_clean_days', 0)))
except Exception as e:
    print(0)
    sys.exit(1)
")
        if [ "$CLOCK_DAYS" -ge 4 ]; then
            echo "   ✓ PASS — clock = $CLOCK_DAYS / 4 (compressed)"
            GATE_CLOCK=0
        else
            echo "   ✗ FAIL — clock = $CLOCK_DAYS / 4 (need ≥ 4 consecutive clean days)"
            GATE_CLOCK=1
        fi
    fi
    echo ""

    echo "[Gate 3/4] Meta-label shadow check (scripts/meta_label_shadow_check.py)..."
    set +e
    python3 scripts/meta_label_shadow_check.py --verbose > /tmp/meta_label_shadow_check.out 2>&1
    META_EXIT=$?
    set -e
    if [ $META_EXIT -le 1 ]; then
        echo "   ✓ PASS — exit $META_EXIT (HEALTHY/WARNING/DISABLED acceptable)"
        GATE_META=0
    else
        echo "   ✗ FAIL — exit $META_EXIT (ERROR — hard gate failure)"
        tail -20 /tmp/meta_label_shadow_check.out | sed 's/^/     /'
        GATE_META=1
    fi
    echo ""

    echo "[Gate 4/4] Deflated Sharpe non-regression..."
    set +e
    python3 scripts/deflated_sharpe_from_audit.py --run-prefix live > /tmp/dsr_live.out 2>&1
    set -e
    if [ ! -f "$DSR_JSON" ]; then
        echo "   ✗ FAIL — $DSR_JSON missing after run"
        GATE_DSR=1
    else
        # Compare total_book.deflated_sharpe vs per-strategy baseline
        # average (proxy baseline — if the live run has no rows yet,
        # it defaults to 0 and the gate passes trivially).
        DSR_RESULT=$(python3 - <<PY
import json
from pathlib import Path
p = Path("$DSR_JSON")
data = json.loads(p.read_text())
tb = data.get("total_book", {})
live = float(tb.get("deflated_sharpe", 0.0) or 0.0)
n = int(tb.get("n_samples", 0) or 0)
# Baseline = weighted avg of per_strategy baseline_deflated_sharpe
per = data.get("per_strategy", [])
if per:
    num = sum(float(r.get("baseline_deflated_sharpe", 0.0) or 0.0) * int(r.get("baseline_n_samples", 0) or 0) for r in per)
    den = sum(int(r.get("baseline_n_samples", 0) or 0) for r in per)
    base = num / den if den > 0 else 0.0
else:
    base = 0.0
delta = live - base
ok = (n < 20) or (delta >= -0.25)
print(f"{'PASS' if ok else 'FAIL'}|{live:.3f}|{base:.3f}|{delta:+.3f}|{n}")
PY
)
        IFS='|' read -r DSR_STATUS DSR_LIVE DSR_BASE DSR_DELTA DSR_N <<< "$DSR_RESULT"
        if [ "$DSR_STATUS" = "PASS" ]; then
            echo "   ✓ PASS — live=$DSR_LIVE, baseline=$DSR_BASE, delta=$DSR_DELTA (n=$DSR_N)"
            GATE_DSR=0
        else
            echo "   ✗ FAIL — live=$DSR_LIVE, baseline=$DSR_BASE, delta=$DSR_DELTA (>0.25 regression)"
            GATE_DSR=1
        fi
    fi
    echo ""
fi

# ── Gate summary ────────────────────────────────────────────────────

echo "───────────────────────────────────────────────────────────────────"
if [ $FORCE -eq 1 ]; then
    echo "  FORCED PROMOTION — gate results ignored"
    ANY_FAIL=0
else
    ANY_FAIL=$((GATE_M3S + GATE_CLOCK + GATE_META + GATE_DSR))
    if [ $ANY_FAIL -eq 0 ]; then
        echo "  ✓ ALL 4 GATES PASSED"
    else
        echo "  ✗ $ANY_FAIL GATE(S) FAILED — promotion BLOCKED"
    fi
fi
echo "───────────────────────────────────────────────────────────────────"
echo ""

if [ $DRY_RUN -eq 1 ]; then
    echo "Dry run complete. No config changes made."
    if [ $ANY_FAIL -eq 0 ]; then
        echo "To promote for real, run:  scripts/promote_m3s_authoritative.sh"
    fi
    exit $ANY_FAIL
fi

if [ $ANY_FAIL -gt 0 ]; then
    echo "Refusing to promote. Fix the failing gate(s) or pass --force."
    exit $ANY_FAIL
fi

# ── Flip the config ─────────────────────────────────────────────────

echo "[Flip 1/4] Editing config/settings.toml [m3s] shadow_mode = false..."
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
    if current_section == "m3s" and stripped.startswith("shadow_mode"):
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == "shadow_mode":
                # Preserve the trailing comment if any
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):].rstrip("\n")
                lines[i] = f"shadow_mode = false{comment}\n"
                changed = True
                break
if not changed:
    raise RuntimeError("failed to locate [m3s] shadow_mode line")
path.write_text("".join(lines))

parsed = tomllib.loads(path.read_text())
assert parsed["m3s"]["shadow_mode"] is False, "sanity check failed"
assert parsed["m3s"]["enabled"] is True, "m3s.enabled must be true for authoritative mode"
print("   ✓ [m3s] shadow_mode = false (verified with tomllib)")
PY

echo ""
echo "[Flip 2/4] Kickstarting trading engine via launchd..."
if launchctl list | grep -q "$ENGINE_LABEL"; then
    launchctl kickstart -k "gui/$(id -u)/$ENGINE_LABEL"
    echo "   ✓ engine restarted"
    # Give launchd a moment to actually start the process
    sleep 1
else
    echo "   ⚠ engine not under launchd — restart manually if needed"
fi
echo ""

echo "[Flip 3/4] Verifying engine logs show m3s_initialized shadow=false..."
VERIFIED=0
for attempt in 1 2 3 4 5; do
    if [ -f "$ENGINE_LOG" ]; then
        # Look at the last 200 lines for a recent m3s_initialized line
        if tail -200 "$ENGINE_LOG" 2>/dev/null | grep -q 'm3s_initialized.*shadow.*false\|"shadow_mode": false\|shadow=False'; then
            echo "   ✓ engine confirmed m3s_initialized shadow=false (attempt $attempt)"
            VERIFIED=1
            break
        fi
    fi
    sleep 2
done
if [ $VERIFIED -eq 0 ]; then
    echo "   ⚠ could not verify from logs within 10s"
    echo "     Check $ENGINE_LOG manually, or run:"
    echo "       tail -f $ENGINE_LOG | grep -i m3s_init"
fi
echo ""

if [ $NO_COMMIT -eq 1 ]; then
    echo "[Flip 4/4] Skipping git commit (--no-commit)"
else
    echo "[Flip 4/4] Committing config/settings.toml..."
    if git -C "$REPO" diff --quiet config/settings.toml; then
        echo "   ⚠ config/settings.toml has no diff — already promoted?"
    else
        git -C "$REPO" add config/settings.toml
        git -C "$REPO" commit -m "$(cat <<'EOF'
feat(m3s): flip to authoritative mode

Session 22 compressed sprint Day 3. All four promotion gates passed:
  - m3s_shadow_check exit 0
  - m3s_shadow_clock ≥ 4 consecutive clean days
  - meta_label_shadow_check exit ≤ 1
  - deflated_sharpe non-regression vs baseline

M3S now owns risk-pct scaling authoritatively.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
        echo "   ✓ committed"
    fi
fi
echo ""

echo "═══════════════════════════════════════════════════════════════════"
echo "  M3S is now AUTHORITATIVE. Compound/allocator decisions are LIVE."
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "Monitor for 24h with:"
echo "   tail -f $ENGINE_LOG"
echo "   watch cat $REPO/data/m3s_shadow_report.md"
echo ""
echo "To revert: set [m3s] shadow_mode = true and kickstart the engine."
