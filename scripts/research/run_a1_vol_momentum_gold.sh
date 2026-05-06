#!/usr/bin/env bash
# Workstream A1 — vol_momentum_gold parameter sensitivity sweep.
#
# Per Super Plan Phase 2 + OOS lock 2026-05-06:
#   - 48-cell param grid (HFM-recommended)
#   - Tune window: 2024-05-06 → 2025-12-31 (~604 days)
#   - Holdout window: 2026-04-15 → 2026-05-05 (21 days)
#   - Fee profile: ic_markets_ctrader_xauusd_normal
#   - 5-fold WF on tune; single-window pass on holdout with chosen params.
#
# Verdict gates (HFM):
#   tune WF avg Calmar > 1.0  AND  holdout PF > 1.2
#
# Default: dry-run. Pass --apply to actually invoke the engine.
#
# Usage:
#   bash scripts/research/run_a1_vol_momentum_gold.sh             # dry-run
#   bash scripts/research/run_a1_vol_momentum_gold.sh --apply     # real

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

EXTRA_ARGS=()
if [[ "${1:-}" == "--apply" ]]; then
    EXTRA_ARGS+=(--apply)
fi

python3 scripts/research/deep_backtest_with_holdout.py \
    --strategy vol_momentum_gold \
    --symbol XAUUSD \
    --timeframe 1h \
    --tune-start 2024-05-06 \
    --tune-end 2025-12-31 \
    --holdout-start 2026-04-15 \
    --holdout-end 2026-05-05 \
    --param-grid 'momentum_threshold=0.0|0.005|0.01|0.02,vol_lookback=168|240|336,cooldown_bars=0|5|24' \
    --fee-profile ic_markets_ctrader_xauusd_normal \
    --leverage 1.0 \
    --wf-n-folds 5 \
    --wf-fold-days 18 \
    --tune-gate-calmar 1.0 \
    --holdout-gate-pf 1.2 \
    --label a1_vol_momentum_gold_2026-05-07 \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
