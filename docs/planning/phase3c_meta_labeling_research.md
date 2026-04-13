# Phase 3c — Meta-Labeling Research Memo

**Stage:** Phase 3c planning | **Status:** Research complete — plan next | **Date:** 2026-04-13

> Research gathered via super-planning process (Session 22). Principal Engineer + Senior Hedge Fund PM personas. Primary source: Lopez de Prado, *Advances in Financial Machine Learning*, chapters 3, 4, 7, 8, 12.

---

## 1. Executive Summary

1. **Meta-labeling is conceptually a good fit but empirically a small upgrade for our specific book.** Lopez de Prado's framing targets primary models with high recall and mediocre precision — "fire often, be right sometimes." Our four live strategies are the opposite: low-frequency, pre-filtered (ADX, BB extremes, Donchian, funding), already validated with purged CV, combined Sharpe ~2.32. The gap meta-labeling is designed to close is narrower here than in the AFML examples.

2. **The immediate blocker is sample size, not algorithm quality.** Our strategies emit 50-200 trades per strategy per month of live data. AFML's rule of thumb is "≥500 events minimum, and that's arbitrary." Weekly retraining would overfit until month 3-6. This is not a "build it in a week" project.

3. **The right build order is audit → offline train → shadow → advisory → live, on a 2-3 month clock.** The `signal_audit` table has to ship first and must record features **as seen at signal time**, not reconstructed later (reconstruction is the single most common way meta-labeling leaks).

4. **Per-strategy classifiers, not one universal model.** Our four strategies have different edge mechanisms (mean-reversion, breakout, momentum, carry). A universal classifier would have to learn an implicit "which strategy is this" embedding anyway; it's cleaner and more debuggable to train four small LightGBM models, each with ~500-2000 training events.

5. **Realistic Sharpe uplift: +0.10 to +0.25 on the combined book, not +0.3 to +0.6.** Lopez de Prado's claim applies to primary models in the Sharpe 0.3-1.0 range where precision is weak. Our primary models are already in the 1.5-2.3 Sharpe range with validated CV. The marginal contribution of a filter is correspondingly smaller. Bigger underquantified win: **max-drawdown reduction of 15-30%** because meta-labeling preferentially filters large losers.

---

## 2. The AFML Algorithm

### 2.1 Triple Barrier Method (ch. 3)

For every primary signal `t_i`:

```
span       = volatility(t_i) * horizon_bars      # ATR_14 * horizon
upper      = price(t_i) * (1 + pt_mult * span)   # profit barrier
lower      = price(t_i) * (1 - sl_mult * span)   # stop barrier
t_expiry   = t_i + horizon_bars                  # time barrier

for j in (t_i + 1, t_expiry]:
    if price(j) >= upper: tb_label = +1; break   # profit hit
    if price(j) <= lower: tb_label = -1; break   # stop hit
else:
    tb_label = sign(price(t_expiry) - price(t_i))  # timeout → residual

meta_label = 1 if tb_label == primary_side else 0
```

**Properties:**
- Volatility-adjusted barriers (per-signal, not fixed).
- Path-dependent labels (whichever barrier is touched first wins).
- Meta-label is binary: "did the primary's direction actually work within the envelope?" NOT "was PnL positive."

### 2.2 Meta-Labeling Flow

```
Stage 1 — primary model emits Signal
Stage 2 — triple_barrier_label(signal, future_prices) → meta_label
Stage 3 — train LightGBM on (features_at_signal_time, meta_label)
          with PurgedKFold CV + sample uniqueness weights
Stage 4 — live inference:
          p = model.predict_proba(features)[0, 1]
          if p < veto_threshold: return None
          else: sig.risk_pct *= sigmoid(p)
```

**Two things most open-source ports get wrong:**
- Primary model must NOT learn size. Meta-labeling only works when primary decides direction and secondary decides conviction (AFML §17: "The 10 Reasons Most ML Funds Fail").
- Secondary model uses **Random Forest** in the book's example but modern practice is **LightGBM with isotonic calibration** (Hudson & Thames 2023 JFDS).

### 2.3 Purged K-Fold CV with `t1` array

Standard K-fold leaks because a label at `t_i` can bleed into training features at `t_i + k` when `k < horizon`. AFML fix:

1. Split by time into K contiguous folds
2. **Purge** training samples whose label interval `[t0, t1]` overlaps the test fold
3. **Embargo** ~1% of observations after the test fold

Our `src/m3s/evaluation.py:68` already implements scalar-overlap purged CV. For meta-labeling we need the **per-sample t1 array variant** — each signal has its own label lifespan. Add a `purged_kfold_splits_t1(t1, n_splits, embargo_pct)` function.

**Combinatorial Purged CV** (AFML ch. 12) is phase-2 — yields many more backtest paths (`C(N, k)`) but is only worth it for hyperparameter tuning, not MVP.

### 2.4 Sample Uniqueness Weights (ch. 4)

Meta-labels are NOT IID. Overlapping label intervals share information.

```
u_i = mean(1 / c_t) for t ∈ [t0_i, t1_i]
where c_t = number of concurrent open labels at time t
```

Clustered signals (regime change, high-vol event) have low uniqueness but carry the most information. Without weighting, the model learns "cluster events are unusual" instead of "features at clusters predict failure."

`sample_weight=u_i` in `LGBMClassifier.fit()`. Skip SMOTE (it interpolates across time → synthetic leakage). Sequential bootstrap is only relevant for RF — LightGBM doesn't use bootstrap bagging by default.

### 2.5 Library Landscape

- **mlfinlab (Hudson & Thames)** — the reference implementation. **Now commercial/proprietary** as of mid-2023; GitHub repo frozen at v1.5. Use as reference, not as a dependency.
- **hudson-and-thames/meta-labeling** — research repo supporting 2022-2023 JFDS papers. Open access.
- **skfolio** — maintained open-source CPCV via `skfolio.model_selection.CombinatorialPurgedCV`.
- **sklearn** — `CalibratedClassifierCV` with isotonic/sigmoid calibration.

**Decision:** no mlfinlab dependency. The surface we need (triple-barrier labeling, purged K-fold with t1, LightGBM training, isotonic calibration) is ~400 LOC of in-house Python.

---

## 3. Feature Set Design

24 features, grouped by category. Every feature is computable from data available at signal close (no lookahead).

**Primary-signal-derived (3):**
- `strategy_name` — categorical (universal classifier only, drop for per-strategy)
- `signal_confidence` — `Signal.confidence` from the strategy
- `signal_direction` — ±1 (must be a feature because features interact with direction)

**Market microstructure (6):**
- `atr_14_pct` — ATR_14 / close
- `bb_width_pct` — Bollinger band width / close
- `volume_zscore_20` — z-score vs 20-bar mean
- `close_over_ema_50` — momentum vs mid-horizon trend
- `rsi_14` — mean reversion gauge
- `macd_hist_zscore_50` — MACD histogram normalized

**Regime (4):**
- `adx_14` — trend vs range
- `vol_regime_idx` — {0,1,2} from `src/m3s/regime.py`
- `funding_rate_abs` — absolute funding rate
- `btc_corr_60d` — rolling 60-day BTC correlation

**Strategy-recent-performance (4):**
- `strategy_win_rate_last_20`
- `strategy_pnl_z_last_20`
- `hours_since_last_signal`
- `bars_since_last_trade_close`

**Time-of-day (3):**
- `hour_of_day_sin`, `hour_of_day_cos`
- `day_of_week`

**Book-level (4):**
- `portfolio_exposure_pct`
- `current_drawdown_pct`
- `open_position_count`
- `m3s_alloc_weight_now`

**Deliberately NOT included:** raw OHLCV levels (don't generalize), any forward-looking data, other strategies' real-time signals, other strategies' recent meta-labels (leakage).

---

## 4. Model Architecture

**LightGBM 4.x binary classifier with isotonic calibration.**

```python
base = lgb.LGBMClassifier(
    n_estimators=400, max_depth=4, num_leaves=15,
    min_child_samples=20, learning_rate=0.03,
    reg_alpha=0.1, reg_lambda=0.1,
    subsample=0.8, colsample_bytree=0.8,
    objective="binary", class_weight="balanced",
)
calibrated = CalibratedClassifierCV(
    base, method="isotonic",
    cv=PurgedKFoldT1(n_splits=5, embargo_pct=0.01, t1=t1_array),
)
calibrated.fit(X, y, sample_weight=uniqueness_weights)
```

**Why LightGBM over RF:** 10-50× faster, better on tabular data with 1k-10k rows, native categorical support, shallower trees with explicit regularization (the right inductive bias when signal is weak).

**Baseline sanity check:** train logistic regression alongside LightGBM. If LightGBM doesn't beat LR AUC by ≥0.03, reject the LightGBM and use LR.

**Calibration:** isotonic via `CalibratedClassifierCV(method="isotonic")`. Below ~800 events fall back to Platt. Reference: Hudson & Thames 2023 "Calibration and Position Sizing" — isotonic beats Platt in 4 of 6 sizing methods because meta-label miscalibration is typically not sigmoid-shaped.

**Training cadence:** weekly Sunday 00:00 UTC. Minimum 500 events per strategy before live activation. Rolling out-of-sample AUC ≥ 0.55 gate.

**Kill switch:** if rolling last-100 AUC drops below 0.50 for 3 consecutive weekly retrains, disable the filter for that strategy and revert to pass-through.

**Latency:** <50ms end-to-end (feature assembly <5ms, predict_proba <1ms, calibration <1ms, logging <1ms). Not a bottleneck.

---

## 5. Training Data Pipeline — `signal_audit` Table

New SQLite table in `data/trades.db`:

```sql
CREATE TABLE IF NOT EXISTS signal_audit (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    strategy          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    signal_ts_ms      INTEGER NOT NULL,
    signal_action     TEXT NOT NULL,
    signal_confidence REAL,
    entry_price       REAL NOT NULL,
    stop_loss         REAL,
    take_profit       REAL,
    risk_pct_original REAL,
    features_json     TEXT NOT NULL,
    primary_model_ver TEXT,

    -- populated at trade close
    trade_id          INTEGER,
    exit_ts_ms        INTEGER,
    exit_price        REAL,
    realized_pnl      REAL,
    realized_pnl_pct  REAL,
    barrier_hit       TEXT,
    triple_barrier_label INTEGER,
    meta_label        INTEGER,
    label_t1_ms       INTEGER,

    -- meta-classifier audit (populated if filter was active)
    meta_proba        REAL,
    meta_decision     TEXT,
    meta_size_mult    REAL,
    meta_model_ver    TEXT
);
```

**Two invariants:**
1. `features_json` is written at signal time, **before** the trade opens. Never reconstructed. Reconstruction is how leakage slips in.
2. The row is written atomically at signal emission. Label columns are UPDATEd later when the trade closes.

**Integration points:**
- `src/backtest/engine.py:169` — after `strategy.process(...)`, write audit row with features + NULL outcome
- `src/backtest/engine.py:144` and CLOSE branch — on trade close, UPDATE the row with exit + triple-barrier label
- Live path in `src/main.py` — same hooks via a new `src/m3s/signal_filter/audit.py` module

**Label generation:**
- **Actual-trade label** — from the realized trade (pt/sl/time barrier)
- **Replay label** (training only) — for vetoed signals, replay triple-barrier against the historical tape

Both sources must agree. Replay uses the same `pt_mult`, `sl_mult`, `horizon` as the strategy's actual SL/TP — read from strategy config.

---

## 6. Integration Architecture

### New module layout

```
src/m3s/signal_filter/
    __init__.py              # exports MetaLabelFilter
    features.py              # build_meta_features(signal, features, snapshot) -> dict
    labels.py                # triple_barrier, meta_label_from_outcome
    cv.py                    # PurgedKFoldT1 splitter
    train.py                 # fit_meta_classifier(audit_df) -> joblib artifact
    audit.py                 # SQLite writes
    filter.py                # MetaLabelFilter — live inference class
```

### Handoff contract (filter sits BEFORE M3S)

```
candle → FeatureEngine → features
       → strategy.process(...) → Signal | None
       → (if not None)
          → MetaLabelFilter.on_signal(sig, features, snapshot)
             → returns (sig_or_None, FilterDecision)
          → (if sig still not None)
             → M3S.on_signal(sig)         # existing risk_pct scaling
             → (if risk_pct > 0)
                → RiskClient → Executor
```

**Rules:**
- Filter runs BEFORE M3S. Reason: if M3S runs first and shrinks `risk_pct`, the filter's features drift from training distribution.
- Filter never up-scales. Scale in `[0, 1]`. Outputs: `Pass` (scale=1.0), `Veto` (return None), `Scale` (`sigmoid((p - 0.5) * k)` multiplied into risk_pct).
- Filter is shadow-able. `shadow_mode=True` computes + logs the decision but returns the original signal unchanged.
- Filter reads model version from a file watcher. Weekly retrain → new artifact → hot-reload without engine restart.

### Composition with M3S

Filter and M3S compose **multiplicatively**. Example:
- Filter says "70% sure, size at 0.7×"
- M3S says "allocator 0.4 × compounder 0.8"
- Final `risk_pct = original × 0.7 × 0.4 × 0.8 = 0.224 × original`

---

## 7. Rollout Strategy (5 phases)

**Phase 0 — Audit plumbing (1 week):** ship `signal_audit` schema + write path in engine.py + live main.py. Ship `build_meta_features`. No classifier yet. This is the only step that must complete before anything else makes sense.

**Phase 1 — Passive data collection (4-8 weeks):** run the current 4 strategies live with audit writes on. No filter. Accumulate ≥500 events per strategy. Week-2 checkpoint: validate features_json is being written with the expected 24-key dict and label updates populate correctly.

**Phase 2 — Offline training + backtest eval (1 week):** train one classifier per strategy. Run backtest in 3 modes:
1. Baseline (no filter)
2. Binary veto mode, threshold=0.5
3. Continuous scaling mode, `scale = sigmoid(8 * (p - 0.5))`

Report per strategy and combined:
- Sharpe delta
- Trade count delta
- Max drawdown delta
- Calibration reliability diagram, Brier, AUC-ROC
- Permutation feature importance (top 10)
- SHAP values on 200-sample holdout

**Phase 3 — Shadow mode live (2 weeks):** `shadow_mode=True`. Filter logs decisions but doesn't alter signals. Watch:
- Agreement rate (model decision vs actual outcome)
- Calibration drift (Brier on live vs training)
- p99 latency of `filter.on_signal`

**Phase 4 — Advisory mode (2 weeks):** `shadow_mode=False` with permissive `veto_threshold=0.30` (filters only the worst 10%). Gate for promotion:
- Rolling out-of-sample AUC ≥ 0.55
- Brier ≤ training Brier × 1.10
- ≤15% of signals filtered
- Strategy-level Sharpe over 2-week window not worse than pre-advisory baseline (Deflated Sharpe as honesty anchor)

**Phase 5 — Live full:** graduate to `veto_threshold=0.5` or scale mode. **Rolling 30-day Deflated Sharpe uplift ≥ +0.10 required to keep the filter active.**

---

## 8. Failure Modes

1. **Overfitting small training windows.** Mitigation: `max_depth=4`, `min_child_samples=20`, purged CV, LightGBM must beat LR by ≥0.03 AUC.
2. **Concept drift.** Couple filter to `src/m3s/edge_decay.py`: if strategy is halved, halve veto threshold; if paused, filter is moot.
3. **Data leakage via features that indirectly reveal outcome.** Add a "leakage canary" feature = `future_return_1h` during Phase 2. If AUC doesn't jump to >0.9, pipeline is correct. Then remove canary permanently.
4. **Vanishing edge (classifier converges to constant).** Detect via calibration slope on held-out data. Slope <0.3 → kill and retrain.
5. **Distribution shift backtest vs live.** Every feature goes through `src/data/feature_engine.py` rolling machinery; live↔backtest parity test already green (`project_live_backtest_parity` memory). Add CI test that feature vectors are bit-equal across paths.
6. **Class imbalance drift.** If `mean(y) > 0.85` or `< 0.15`, skip retrain and alert.
7. **Correlated strategies compound filter errors.** Mitigation: filter features include `portfolio_exposure_pct` and `m3s_alloc_weight_now` so the model sees cross-strategy state.
8. **Model staleness on engine restart.** Validate model's training window ends ≤7 days ago; else shadow for 24h and retrain.
9. **Backtest/live feature drift on book-level features** (`current_drawdown_pct`, `open_position_count`). Compute these from a unified `PortfolioSnapshot` interface — already exists via `M3S.snapshot()`.
10. **The meta-labeling paradox.** If primary models are already good, filter adds little. Early warning = Phase 4 gates.

---

## 9. Realistic Sharpe Uplift

**Estimated uplift: +0.10 to +0.25 on the combined book.**

Derivation:
- **Lopez de Prado's +0.3 to +0.6** applies to Sharpe 0.3-1.0 primary models. Ours are 1.0-1.8. **Haircut: 50%** → +0.15 to +0.30 per strategy.
- **Diversification already captures part of the uplift.** Book Sharpe 2.3 vs per-strategy avg ~1.5 says strategy correlations are doing heavy lifting. Meta-labeling helps per-strategy precision, but some uplift is redundant with existing diversification benefit. **Haircut: 40-50%.**
- **Sample-size haircut.** Months 3-4 will be 50-70% of asymptotic uplift.
- **Net:** `+0.20 × 0.55 × 0.6 ≈ +0.066` in months 3-4; `+0.20 × 0.55 × 0.85 ≈ +0.093` in months 6-9; plateau around **+0.11**.

**Bigger win: drawdown reduction.** Hudson & Thames 2023 case studies consistently show max-DD reduction of 15-30% even when Sharpe uplift is modest, because meta-labeling preferentially filters large losers.

**Recommendation: BUILD IT, BUT SEQUENCED.**
- Phase 0 (audit table) ships immediately — useful for debugging + performance attribution even without meta-labeling
- Defer classifier training until ≥500 events per strategy accumulated
- Set realistic expectations: +0.10-0.15 Sharpe, −15-25% max drawdown
- Be willing to run the filter in shadow indefinitely if Phase 4 gates don't pass

**DO NOT BUILD IT IF:**
- Phase 1 reveals audit features have drift/bugs we can't fix cheaply
- Phase 2 backtests show no uplift even in-sample with reasonable hyperparameters
- Phase 4 (funding_carry live stabilization + 5th strategy research) has higher marginal Sharpe per engineering-week

**Honest take:** in a world where a new strategy ships +0.3-0.5 Sharpe in 2-3 weeks, meta-labeling's +0.1-0.15 in 2-3 months is a **second-priority project**. It's a durable investment in the pipeline (reusable for future strategies, reusable for tuning) but it is not the highest-ROI thing we could build next.

---

## 10. Sources

- Lopez de Prado — *Advances in Financial Machine Learning* (Wiley, 2018), ch. 3, 4, 7, 8, 12
- Lopez de Prado — *Machine Learning for Asset Managers* (2020)
- Lopez de Prado — "The 10 Reasons Most ML Funds Fail" (SSRN 3104816)
- Bailey & Lopez de Prado — "The Deflated Sharpe Ratio" (SSRN 2460551)
- Hudson & Thames — "Does Meta Labeling Add to Signal Efficacy?" (2022)
- Hudson & Thames — "Meta-Labeling: Calibration and Position Sizing" JFDS 5(2), 2023
- Hudson & Thames — "Meta-Labeling Architecture" JFDS 2022
- Hudson & Thames — "Meta-Labeling: Theory and Framework" JFDS 2022
- Hudson & Thames — "Bagging in ML: Sequential Bootstrapping"
- MlFinLab commercial licensing page — Hudson & Thames (post-2023)
- Wikipedia — "Meta-Labeling", "Purged cross-validation"
- skfolio — CombinatorialPurgedCV documentation
- QuantConnect — "Why Meta-Labeling Is Not a Silver Bullet" forum discussion
- Quantreo — "The Triple Barrier Labeling of Marco Lopez de Prado" + meta-labeling tutorial
- BlackArbs — "Labeling and Meta-Labeling Returns for ML Prediction"
- Quang Khai Nguyen — "Meta labeling in Cryptocurrencies Market" (Medium, 2024)
- sklearn — Probability calibration (CalibratedClassifierCV, isotonic, Platt)
- LightGBM parameters reference
