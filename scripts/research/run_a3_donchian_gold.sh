#!/usr/bin/env bash
# Workstream A3 — donchian_gold sl/tp ATR-multiplier 2D sweep.
#
# Per Super Plan Phase 4.1 + OOS lock 2026-05-06:
#   - 12-cell 2D grid (HFM expansion of the original sl-only sweep)
#   - Tune window: 2024-05-06 → 2025-12-31 (shared Phase G epoch with vol_momentum_gold)
#   - Holdout window: 2026-04-15 → 2026-05-05 (21 days)
#   - Fee profile: ic_markets_ctrader_xauusd_normal
#
# Verdict gates (HFM):
#   tune WF avg Calmar > 1.0  AND  holdout Calmar > 1.0
#
# If winning cell sits at the boundary (sl_atr_mult=6.0 or tp_atr_mult=7.0),
# expand grid ONCE and re-run. After that, accept whichever cell wins.
#
# Default: dry-run. Pass --apply to actually invoke the engine.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

EXTRA_ARGS=()
if [[ "${1:-}" == "--apply" ]]; then
    EXTRA_ARGS+=(--apply)
fi

python3 scripts/research/deep_backtest_with_holdout.py \
    --strategy donchian_gold \
    --symbol XAUUSD \
    --timeframe 1h \
    --tune-start 2024-05-06 \
    --tune-end 2025-12-31 \
    --holdout-start 2026-04-15 \
    --holdout-end 2026-05-05 \
    --param-grid 'sl_atr_mult=3.0|4.0|5.0|6.0,tp_atr_mult=3.0|5.0|7.0' \
    --fee-profile ic_markets_ctrader_xauusd_normal \
    --leverage 1.0 \
    --wf-n-folds 5 \
    --wf-fold-days 18 \
    --tune-gate-calmar 1.0 \
    --holdout-gate-pf 0 \
    --holdout-gate-calmar 1.0 \
    --label a3_donchian_gold_2026-05-11 \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
