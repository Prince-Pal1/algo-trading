#!/bin/bash
# Task #116 — swift_alma_v2 cross-mode walk-forward verdict sweep.
#
# Runs deep_backtest across all 5 leverage modes on 1h × [90,365]d XAUUSD.
# Each mode lands in its own report directory under
# reports/deep_backtest_swift_alma_v2_task116/<mode>/.
#
# Exit 0 always (even if individual modes fail) so the caller can inspect
# all partial results. A summary of per-mode verdicts is printed at the end.

set -u
cd "$(dirname "$0")/.."
OUT_BASE="reports/deep_backtest_swift_alma_v2_task116"
mkdir -p "$OUT_BASE"

MODES=(invariant margin_capped vol_targeted risk_scaled kelly_fractional)
RESULTS_FILE="$OUT_BASE/per_mode_verdicts.txt"
: > "$RESULTS_FILE"

echo "=== swift_alma_v2 task #116 cross-mode WF sweep ===" | tee -a "$RESULTS_FILE"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$RESULTS_FILE"
echo "" | tee -a "$RESULTS_FILE"

for mode in "${MODES[@]}"; do
    echo "--- mode=$mode ---" | tee -a "$RESULTS_FILE"
    OUT_DIR="$OUT_BASE/${mode}"
    mkdir -p "$OUT_DIR"

    # --baseline-leverage matters for risk_scaled (reference leverage).
    # --wf-fee lets WF use the matrix cell. Default fee = ic_markets_mt4_xauusd_normal
    # (MT4 native; swift_alma_v2 was tuned on MT4 per task #131).
    python3 scripts/deep_backtest.py swift_alma_v2 \
        --non-interactive \
        --timeframes 1h \
        --windows 90,365 \
        --leverages 10 \
        --fees ic_markets_mt4_xauusd_normal \
        --leverage-mode "$mode" \
        --baseline-leverage 10 \
        --wf-n-folds 7 \
        --wf-fold-days 90 \
        --wf-gate-calmar 0.5 \
        --out-dir "$OUT_DIR" \
        --no-pdf \
        > "$OUT_DIR/stdout.log" 2> "$OUT_DIR/stderr.log"
    RC=$?

    if [ -f "$OUT_DIR/summary.json" ]; then
        VERDICT=$(python3 -c "import json; d=json.load(open('$OUT_DIR/summary.json')); print(d.get('verdict','?'))" 2>/dev/null || echo "?")
        REASON=$(python3 -c "import json; d=json.load(open('$OUT_DIR/summary.json')); print(d.get('verdict_reason','?')[:160])" 2>/dev/null || echo "?")
    else
        VERDICT="NO_SUMMARY"
        REASON="exit=$RC; summary.json missing; see stderr.log"
    fi

    echo "  exit=$RC  verdict=$VERDICT" | tee -a "$RESULTS_FILE"
    echo "  reason: $REASON" | tee -a "$RESULTS_FILE"
    echo "" | tee -a "$RESULTS_FILE"
done

echo "=== Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$RESULTS_FILE"
echo ""
echo "Per-mode verdicts written to $RESULTS_FILE"
exit 0
