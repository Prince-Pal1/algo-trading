# Phase 3c — Meta-Labeling: Execution Plan

**Stage:** Phase 3c planning | **Status:** Research complete, plan approved, Phase 0 ready to ship | **Date:** 2026-04-13

> Execution plan for Meta-Labeling (Lopez de Prado AFML ch. 3). Research memo with full rationale at `docs/planning/phase3c_meta_labeling_research.md`.

---

## Context

Phase 3b-2 (M3S) and 3b-3 (Strategy A funding_carry) are live. Phase 3c was deferred from the M3S plan because meta-labeling needs **real live signal data to train on**, not backtest fixtures. Now that the engine is in shadow mode and funding_carry is active, we can start accumulating that data.

Meta-labeling is not the highest-ROI item on the roadmap for this book (per the research verdict: +0.10-0.25 Sharpe uplift, vs. a new strategy's typical +0.3-0.5 in less time). But it's a **durable pipeline investment**: the audit table, feature engineering layer, and calibrated ML classifier infrastructure are reusable for every future strategy and every future tuning exercise.

The plan below sequences it so **Phase 0 ships now** (useful even without ML) while the actual classifier training is gated on accumulating ≥500 events per strategy — a 2-3 month wall clock. No ML work is wasted if we decide to pivot after Phase 0.

---

## Scope Decisions

1. **Per-strategy classifiers, not a universal model.** Four separate LightGBM models, each trained on that strategy's own history. Simpler debugging, cleaner specialization, no cross-strategy leakage.

2. **LightGBM 4.x with isotonic calibration** via `sklearn.calibration.CalibratedClassifierCV`. Not Random Forest (AFML's example choice is ~a decade old).

3. **No `mlfinlab` dependency.** Hudson & Thames went commercial mid-2023. The ~400 LOC we need (triple-barrier labeling, purged K-fold with per-sample t1, uniqueness weights, feature builder) we write in `src/m3s/signal_filter/` and version-control ourselves.

4. **Filter sits BEFORE M3S in the signal chain.** The classifier was trained on feature distributions at signal time; if M3S runs first and shrinks `risk_pct`, the filter's features drift from training distribution.

5. **Filter never up-scales.** Like M3S, output is in `[0, 1]` and multiplies into `risk_pct`. Outputs: `Pass` / `Veto` (return None) / `Scale` (sigmoid-weighted multiplier).

6. **Shadow → advisory → live rollout** with explicit gates. No activation before Phase 4 gates pass.

7. **Weekly retraining** via the existing M3S scheduler cron tick. Hot-reload via file watcher — no engine restart.

---

## Phase Breakdown

### Phase 0 — Audit plumbing (~1 week) — SHIPS NOW

**Goal:** capture every signal's features at signal time + label each at trade close. No ML yet. This data is useful even if we never train a classifier (debugging, performance attribution, next-strategy feature engineering).

**Files:**

| File | Action | Description |
|---|---|---|
| `src/m3s/signal_filter/__init__.py` | CREATE | package scaffold |
| `src/m3s/signal_filter/features.py` | CREATE | `build_meta_features(signal, features, snapshot) -> dict` — the 24-feature builder |
| `src/m3s/signal_filter/audit.py` | CREATE | `audit_signal(signal, features, snapshot) -> int` + `audit_close(trade_id, exit_info)` SQLite write-through |
| `src/m3s/signal_filter/labels.py` | CREATE | `triple_barrier_label`, `meta_label_from_outcome` — pure math, no storage |
| `src/backtest/result_store.py` | MODIFY | add `signal_audit` table migration alongside `backtest_runs`/`backtest_results` |
| `src/backtest/engine.py` | MODIFY — +5 lines | after `strategy.process(...)` returns a non-None signal, call `audit_signal(...)`; in the SL/TP/CLOSE branches, call `audit_close(...)` |
| `src/main.py` | MODIFY — +5 lines | same hook points in `_on_features` and `_handle_carry_signal` |
| `tests/test_signal_filter/test_features.py` | CREATE | golden 24-feature dict for a known signal (deterministic) |
| `tests/test_signal_filter/test_labels.py` | CREATE | hand-calculated triple-barrier labels |
| `tests/test_signal_filter/test_audit.py` | CREATE | SQLite write + update round-trip |

**Gate:** all tests green; backtest of existing 4 strategies produces ≥1 audit row per signal; live engine writes audit rows without crash for 24h.

**Sub-phase count:** 4 (schema, features, labels, engine integration). **Estimated sessions:** 1.

### Phase 1 — Passive data collection (4-8 weeks wall clock)

**Goal:** accumulate ≥500 audit rows per strategy with populated labels. No code changes except bug fixes.

**What's actually running:**
- Existing 4 strategies on their normal cadence
- Audit writes on every signal emission + trade close
- No classifier, no filter

**Monitoring:**
- Week-2 checkpoint: verify `features_json` dict has exactly 24 keys, all populated, no NaN.
- Week-4 checkpoint: verify label population is working (rows have non-NULL `meta_label`).
- Event count dashboard: running count of `signal_audit` rows per strategy + label population rate.

**Gate to Phase 2:** each strategy has ≥500 rows with non-NULL `meta_label`. `bb_rsi_mr_opt` will hit this fastest (highest signal frequency). `donchian_ensemble_adx` slowest.

**Sub-phase count:** 0 (passive). **Estimated sessions:** 0 active work. Wall clock: 4-8 weeks depending on signal density.

### Phase 2 — Offline training + backtest evaluation (~1 week active work)

**Goal:** train one classifier per strategy, run backtest in 3 modes, report metrics.

**Files:**

| File | Action | Description |
|---|---|---|
| `src/m3s/signal_filter/cv.py` | CREATE | `PurgedKFoldT1(n_splits, embargo_pct, t1: ndarray)` — per-sample purge + embargo |
| `src/m3s/signal_filter/train.py` | CREATE | `fit_meta_classifier(audit_df, strategy_name) -> joblib.Path` — LightGBM + CalibratedClassifierCV + uniqueness weights |
| `src/m3s/signal_filter/filter.py` | CREATE | `MetaLabelFilter` — loads model, exposes `on_signal(sig, features, snapshot) -> (sig|None, FilterDecision)` |
| `scripts/train_meta_classifier.py` | CREATE | CLI: `python3 scripts/train_meta_classifier.py --strategy bb_rsi_mr_opt` |
| `src/backtest/engine.py` | MODIFY — 3 lines | optional `meta_filter: MetaLabelFilter \| None = None` parameter |
| `tests/test_signal_filter/test_cv.py` | CREATE | purged CV with per-sample t1 — disjoint sets, embargo, leakage canary |
| `tests/test_signal_filter/test_train.py` | CREATE | synthetic dataset → train → AUC > 0.9 on the training data (sanity) |
| `tests/test_signal_filter/test_filter.py` | CREATE | loaded filter produces expected decisions on golden inputs |

**Sub-phases:**
- 2.1: Purged K-fold with t1 (~0.5 sessions)
- 2.2: Feature extraction + LightGBM training pipeline (~1 session)
- 2.3: Backtest integration with filter + 3-mode comparison (~1 session)
- 2.4: Metrics + plots (calibration diagram, SHAP, permutation importance) (~0.5 sessions)

**Gate to Phase 3:**
- Each strategy's classifier beats logistic regression baseline by ≥0.03 AUC
- Calibration slope in [0.7, 1.3] on held-out data
- Permutation importance: top 5 features make intuitive sense
- SHAP signs on 200-sample holdout match intuition
- In-sample combined Sharpe uplift ≥ +0.05 vs baseline (the floor for "is there even an edge")

**Hard kill:** if the combined offline Sharpe uplift is ≤0 on the ~2-year backtest window with reasonable hyperparameters, the strategy set has no room for meta-labeling to help. Kill Phase 3c with an obituary and move on.

**Estimated sessions:** 3.

### Phase 3 — Shadow mode live (2 weeks wall clock)

**Goal:** run the filter in shadow, log decisions without altering signals, validate live behavior matches training.

**Changes:**
- `config/strategies.toml` — new `[meta_label]` section with `enabled = true`, `shadow_mode = true`, `veto_threshold = 0.30` (permissive)
- `src/main.py` — wire `MetaLabelFilter` into the signal chain BEFORE `M3S.on_signal` (similar pattern to how M3S itself was wired sub-phase 0.8)
- New shadow checker script `scripts/meta_label_shadow_check.py` (~50 LOC) that reads `signal_audit` and reports:
  - Rolling 100-signal AUC on live data
  - Calibration Brier score (live vs training)
  - p99 latency of `filter.on_signal`
  - Agreement rate (model decision vs. actual outcome)

**Gate to Phase 4:**
- Rolling live AUC ≥ 0.55 for ≥100 fresh signals
- Live Brier ≤ training Brier × 1.10
- p99 latency < 50ms
- Zero runtime exceptions in filter.on_signal over 2 weeks

**Estimated sessions:** 0.5 (wiring) + wall clock (2 weeks).

### Phase 4 — Advisory mode (2 weeks wall clock)

**Goal:** flip `shadow_mode=false` with permissive threshold. Filter actively vetoes the worst 10% of signals. First real production test.

**Changes:**
- `config/strategies.toml [meta_label] shadow_mode = false`
- `veto_threshold = 0.30` still (worst 10% filter)
- Restart engine

**Promotion gate to Phase 5:**
- Rolling OOS AUC ≥ 0.55
- Realized Brier ≤ training Brier × 1.10
- ≤15% of signals filtered (sanity — not too aggressive)
- Combined strategy Sharpe over 2-week window NOT worse than the corresponding 2-week baseline window (use Deflated Sharpe via `src/m3s/evaluation.py:_deflated_sharpe` as the honesty anchor)

**Estimated sessions:** 0.5 (config flip + monitoring cadence) + wall clock (2 weeks).

### Phase 5 — Live full (ongoing)

**Goal:** graduate to production thresholds.

**Changes:**
- `veto_threshold = 0.5` OR scale mode `scale = sigmoid(8 * (p - 0.5))` — pick based on Phase 4 data
- Weekly retraining via M3S scheduler cron tick
- Kill switch armed: rolling last-100 AUC < 0.50 for 3 consecutive weekly retrains → filter disabled, pass-through until manual review
- Rolling 30-day Deflated Sharpe uplift ≥ +0.10 required to keep the filter active

**Ongoing monitoring (automated):**
- Weekly retrain validation → kept vs. rejected decision
- Daily Brier drift check
- Weekly Deflated Sharpe attribution

**Estimated sessions:** 0.5 active + ongoing.

---

## Total Estimated Sessions

| Phase | Active sessions | Wall clock |
|---|---|---|
| 0 — Audit plumbing | 1 | 1 day |
| 1 — Passive data collection | 0 | 4-8 weeks |
| 2 — Offline training + eval | 3 | 1 week active |
| 3 — Shadow mode live | 0.5 | 2 weeks passive |
| 4 — Advisory mode | 0.5 | 2 weeks passive |
| 5 — Live full | 0.5 | ongoing |
| **Total** | **~5.5 sessions** | **~10-14 weeks wall clock** |

The ~5.5 active sessions are spread across ~3 months of wall clock. Most of the time is waiting for live data to accumulate and for rollout gates to clear.

---

## Resolved Design Decisions

From the research memo's open questions:

1. **Per-strategy classifiers**, not universal
2. **LightGBM 4.x** with `max_depth=4, num_leaves=15, min_child_samples=20`
3. **Isotonic calibration** via `CalibratedClassifierCV(method="isotonic")`; Platt fallback below 800 training events
4. **Binary veto mode** in Phase 4, **scale mode** possible in Phase 5 based on live data
5. **24-feature starter set** (3 signal + 6 microstructure + 4 regime + 4 recent-perf + 3 time + 4 book-level)
6. **`strategy_name`** NOT included in per-strategy classifiers (only for universal; we chose per-strategy)
7. **Baseline sanity check:** logistic regression must underperform LightGBM by ≥0.03 AUC, else reject LightGBM
8. **Sample uniqueness weighting** via AFML's `u_i = mean(1 / c_t)` formula, passed as `sample_weight` to LightGBM
9. **Weekly retraining** on Sunday 00:00 UTC via M3S scheduler
10. **Hot-reload** via file watcher on the joblib artifact

---

## Open Questions for Prince (none requiring immediate decision)

1. **Scope hack for Phase 0 shipping now:** do we ship the full `signal_audit` table schema + write path immediately (Phase 0 = ~1 session), or wait until after M3S 7/7 clock hits and funding_carry has stabilized (Phase 0 = later)?
   - **Recommended default:** ship Phase 0 NOW. The audit table is cheap, useful for debugging independent of meta-labeling, and has no runtime cost to the engine (~1ms per signal).
2. **What if Phase 2 offline eval shows no uplift?** Per the plan's hard kill, obituary and move on. Don't polish.
3. **What if Phase 2 shows great uplift (≥+0.3 Sharpe)?** Consider whether to accelerate Phase 3/4 cadence. Default: keep the 2-week wall clock for each rollout phase regardless.

---

## Critical Files

| Path | Action | Phase |
|---|---|---|
| `src/m3s/signal_filter/__init__.py` | CREATE | 0 |
| `src/m3s/signal_filter/features.py` | CREATE | 0 |
| `src/m3s/signal_filter/audit.py` | CREATE | 0 |
| `src/m3s/signal_filter/labels.py` | CREATE | 0 |
| `src/backtest/result_store.py` | MODIFY — add signal_audit table | 0 |
| `src/backtest/engine.py` | MODIFY — +5 lines audit hooks | 0 |
| `src/main.py` | MODIFY — +5 lines audit hooks for live | 0 |
| `src/m3s/signal_filter/cv.py` | CREATE — PurgedKFoldT1 | 2 |
| `src/m3s/signal_filter/train.py` | CREATE — LightGBM training pipeline | 2 |
| `src/m3s/signal_filter/filter.py` | CREATE — MetaLabelFilter live inference | 2 |
| `scripts/train_meta_classifier.py` | CREATE — CLI | 2 |
| `scripts/meta_label_shadow_check.py` | CREATE — rolling AUC/Brier monitor | 3 |
| `config/strategies.toml` | MODIFY — new `[meta_label]` section | 3 |
| `docs/planning/phase3c_meta_labeling_research.md` | DONE (Session 22) | — |
| `docs/planning/phase3c_meta_labeling_plan.md` | DONE (this file) | — |

**Test coverage target:** ~50 new tests across Phases 0 and 2.

---

## Verification (end-to-end at Phase 2 completion)

1. **Test suite green:** full suite passes including new `tests/test_signal_filter/`.
2. **Audit table populated:** SQL `SELECT strategy, COUNT(*) FROM signal_audit GROUP BY strategy` returns non-zero counts for all 4 strategies after 1 week of live running.
3. **Labels populated:** SQL `SELECT strategy, COUNT(*) FROM signal_audit WHERE meta_label IS NOT NULL GROUP BY strategy` reaches ≥500 per strategy.
4. **Training CLI works:** `python3 scripts/train_meta_classifier.py --strategy bb_rsi_mr_opt` produces a joblib artifact + metrics report.
5. **Purged CV leakage canary:** training with `future_return_1h` as a feature produces AUC >0.9; removing it drops AUC to <0.65. This validates the pipeline is correct and catches leakage.
6. **Backtest 3-mode comparison** on 2-year window shows a clear per-strategy and combined picture.
7. **Phase 2 report** written to `docs/strategy_obituaries/` or `docs/planning/phase3c_eval_report.md` with go/no-go decision.

---

## Out of Scope (v1)

Explicitly NOT doing in Phase 3c v1:

1. Combinatorial Purged CV (phase-2 concern)
2. Universal classifier across strategies (per-strategy only)
3. Neural-network-based filters (LightGBM only)
4. Online learning / streaming retraining (batch weekly only)
5. Multi-class meta-labels (binary only)
6. Feature store / feature registry (direct function call only)
7. Model explainability UI (SHAP plots are CLI-only)
8. Automated feature engineering via search (manual 24-feature set)
9. Meta-labeling-aware risk sizing beyond the existing filter→M3S multiplication chain
10. Hyperparameter tuning via Optuna (fixed hyperparameters from the research memo)

---

## Why Phase 0 should ship even if we never build the rest

The `signal_audit` table is useful independently:
- **Performance attribution:** "why did bb_rsi_mr_opt have a bad week?" → query audit rows, inspect feature distributions
- **Debugging:** every signal's feature vector is frozen at signal time → reproduce any bug exactly
- **Training data for other ML work:** regime classifier improvements, strategy correlation models, any future ML work reuses this data
- **Deflated Sharpe computation:** with audit data we can compute per-strategy Deflated Sharpe on the live book, not just backtest

Even if Phase 3c ends with "killed at Phase 2 eval — no uplift found," Phase 0's audit infrastructure survives as a permanent debugging win. Low regret.
