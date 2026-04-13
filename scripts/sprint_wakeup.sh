#!/usr/bin/env bash
# Session 22 Compressed Sprint — Wall-Clock Wakeup Dispatcher
#
# Called by launchd plists (com.algo-trading.sprint-day-{1,3,4,5}.plist) at
# specific dates/times during the sprint. Runs the appropriate day's
# promotion steps NON-INTERACTIVELY, logs everything to
# data/logs/sprint_wakeup_day<N>.log, and exits.
#
# Why this exists: CronCreate `durable: true` is silently ignored, so
# Claude session-only wake-ups die on compaction/session end. launchd is
# 100% OS-durable — this script is the real safety net.
#
# Usage (called by launchd, not humans):
#   scripts/sprint_wakeup.sh --day 1
#   scripts/sprint_wakeup.sh --day 3
#   scripts/sprint_wakeup.sh --day 4
#   scripts/sprint_wakeup.sh --day 5
#
# Human diagnostic:
#   scripts/sprint_wakeup.sh --day 3 --dry-run    # runs gates without flipping
#
# Each day is idempotent — safe to re-run.

set -euo pipefail

REPO="/Users/prince/algo-trading"
LOG_DIR="$REPO/data/logs"
SENTINEL_DIR="$REPO/data/sprint_sentinels"
DAY=""
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --day) DAY="${2:-}"; shift 2 ;;
        --day=*) DAY="${1#--day=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,22p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

if [ -z "$DAY" ]; then
    echo "ERROR: --day <1|3|4|5> required" >&2
    exit 2
fi

cd "$REPO"
mkdir -p "$LOG_DIR" "$SENTINEL_DIR"

LOG="$LOG_DIR/sprint_wakeup_day${DAY}.log"
SENTINEL="$SENTINEL_DIR/day${DAY}_done"
TODAY="$(date -u +%Y-%m-%d)"

# ── Idempotency check ───────────────────────────────────────────────

if [ $DRY_RUN -eq 0 ] && [ -f "$SENTINEL" ]; then
    echo "[$(date -u +%FT%TZ)] Day $DAY already completed on $(cat "$SENTINEL"), skipping." | tee -a "$LOG"
    exit 0
fi

# ── Log everything to sprint_wakeup_day<N>.log ──────────────────────

exec > >(tee -a "$LOG") 2>&1

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  Sprint wakeup Day $DAY — $(date -u +%FT%TZ)"
echo "  Mode: $( [ $DRY_RUN -eq 1 ] && echo DRY-RUN || echo PROMOTE )"
echo "════════════════════════════════════════════════════════════════════"
echo ""

# Minimal PATH — do NOT source .zshrc (set -e incompatibility). Framework
# python3 MUST come first so child scripts calling `python3` use the
# install that has joblib/sklearn/lightgbm. Git credentials come from
# osxkeychain credential helper which works under launchd without
# interactive shell setup.
export PATH="/Library/Frameworks/Python.framework/Versions/3.11/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$REPO:${PYTHONPATH:-}"

DRY_FLAG=""
if [ $DRY_RUN -eq 1 ]; then
    DRY_FLAG="--dry-run"
fi

# ── Day 1 — morning status check ────────────────────────────────────

do_day_1() {
    echo "[Day 1] Running status checks..."
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/project_status.py --verbose || true
    echo ""
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/m3s_shadow_check.py --verbose || true
    echo ""
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/meta_label_shadow_check.py --verbose || true
    echo ""
    echo "[Day 1] Status check complete. No promotions today."
}

# ── Day 3 — M3S authoritative + meta-label advisory ────────────────

do_day_3() {
    echo "[Day 3] M3S shadow → authoritative + meta-label shadow → advisory"
    echo ""
    echo "[Day 3 / Step 1] promote_m3s_authoritative.sh $DRY_FLAG"
    if ! ./scripts/promote_m3s_authoritative.sh $DRY_FLAG; then
        echo "❌ [Day 3] promote_m3s_authoritative.sh FAILED. Aborting."
        return 1
    fi
    echo ""
    echo "[Day 3 / Step 2] promote_meta_label.sh --to advisory $DRY_FLAG"
    if ! ./scripts/promote_meta_label.sh --to advisory $DRY_FLAG; then
        echo "❌ [Day 3] promote_meta_label.sh --to advisory FAILED."
        return 1
    fi
    echo ""
    if [ $DRY_RUN -eq 0 ]; then
        echo "[Day 3 / Step 3] git push origin main"
        if git push origin main 2>&1; then
            echo "   ✓ pushed"
        else
            echo "   ⚠ push failed — commit is local, push manually with git push origin main"
        fi
    fi
    echo ""
    echo "[Day 3] M3S authoritative + meta advisory flip complete."
    return 0
}

# ── Day 4 — meta-label advisory → live ─────────────────────────────

do_day_4() {
    echo "[Day 4] meta-label advisory → live"
    echo ""
    echo "[Day 4 / Step 1] Verify Day 3 landed"
    if ! grep -q "shadow_mode = false" config/settings.toml; then
        echo "❌ [Day 4] Day 3 promotions did NOT land — config still shows shadow_mode=true. Aborting."
        return 1
    fi
    echo "   ✓ Day 3 flip present in config"
    echo ""
    echo "[Day 4 / Step 2] promote_meta_label.sh --to live $DRY_FLAG"
    if ! ./scripts/promote_meta_label.sh --to live $DRY_FLAG; then
        echo "⚠ [Day 4] promote_meta_label.sh --to live gate failed"
        echo "   (Expected when live rows < 20 per strategy on sparse-signal strategies.)"
        echo "   Meta-label stays in advisory mode. Not a sprint failure — partial success."
        return 0  # Not fatal
    fi
    echo ""
    if [ $DRY_RUN -eq 0 ]; then
        echo "[Day 4 / Step 3] git push origin main"
        git push origin main 2>&1 || echo "   ⚠ push failed — commit is local"
    fi
    return 0
}

# ── Day 5 — final wrap ─────────────────────────────────────────────

do_day_5() {
    echo "[Day 5] Final sprint wrap — regenerate status + test suite"
    echo ""
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/project_status.py --verbose || true
    echo ""
    echo "[Day 5] Test suite:"
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m pytest tests/ -q 2>&1 | tail -10 || true
    echo ""
    echo "[Day 5] Sprint commits since d673a71:"
    git log --oneline d673a71..HEAD || true
    echo ""
    echo "[Day 5] Final state captured. Manual STATE.md/ROADMAP.md updates"
    echo "         should happen via Claude session — this wakeup just logs."
}

# ── Dispatch ───────────────────────────────────────────────────────

EXIT_CODE=0
case "$DAY" in
    1) do_day_1 || EXIT_CODE=$? ;;
    3) do_day_3 || EXIT_CODE=$? ;;
    4) do_day_4 || EXIT_CODE=$? ;;
    5) do_day_5 || EXIT_CODE=$? ;;
    *) echo "ERROR: Day must be 1, 3, 4, or 5 (got: $DAY)"; exit 2 ;;
esac

# ── Write sentinel on success ──────────────────────────────────────

if [ $DRY_RUN -eq 0 ] && [ $EXIT_CODE -eq 0 ]; then
    echo "$TODAY" > "$SENTINEL"
    echo ""
    echo "[Day $DAY] ✓ complete — sentinel written: $SENTINEL"
fi

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  Day $DAY exit code: $EXIT_CODE"
echo "════════════════════════════════════════════════════════════════════"
exit $EXIT_CODE
