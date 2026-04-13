# Compressed Roadmap — Session 22 Aggressive Sprint

**Goal:** finish the project main structure in 4-5 days of wall-clock time.

**Core insight:** we don't have to wait for live audit data. 2 years of historical
OHLCV + bit-exact backtest-live parity (Session 20) lets us **harvest training
data from backtests in hours, not weeks.** Live data continues accumulating in
parallel and is used for validation only.

**Key tradeoffs vs. the conservative plan:**
- Training data is backtest-derived initially → slightly higher risk of
  train/live distribution drift. Mitigation: the shadow-mode phase validates
  AUC on live data before advisory/live promotion.
- M3S shadow clock compressed from 7 → 4 days (our active shadow checker fires
  every 6h, so 4 days = 16 audits — still defensible).
- Phase 3c Phase 1 (passive data collection) replaced with Phase 1' (backtest
  harvest) for the initial training run. Live collection continues in parallel
  and feeds the weekly retrain job.

---

## Wall-clock schedule (4 days + buffer)

| Day | Today / +N | What executes | Gate at end |
|---|---|---|---|
| 0 (today) | 2026-04-13 | Phase 0.5 live wiring + backtest harvest + Phase 2 training pipeline + Phase 2 offline training run + Phase 3 shadow filter integration + launchd retrain job | Meta-label filter LIVE in shadow mode, M3S shadow clock advances from day 1 |
| +1 | 2026-04-14 | Automatic: launchd weekly retrain, shadow checker 6h ticks, live audit accumulates | M3S day 2, filter logs proba for live signals |
| +2 | 2026-04-15 | Automatic: same + Claude morning wake-up reviews status | M3S day 3, filter calibration drift check |
| +3 | 2026-04-16 | Manual decision point: flip meta-label shadow → advisory if AUC holds | M3S day 4 → **M3S AUTHORITATIVE FLIP** |
| +4 | 2026-04-17 | Meta-label advisory mode (permissive veto) | Filter actively shrinks signals |
| +5 | 2026-04-18 | Project wrap: final docs, phase 5 meta-label full activation if advisory clean | **PROJECT COMPLETE** |

---

## Parallel work plan (today, Session 22)

### Lane A — Phase 0.5 live audit wiring (30 min)
- `src/main.py._on_features` writes `signal_audit` row on every non-None signal
- `src/main.py._handle_carry_signal` same for funding carry
- `paper_executor.on_trade_close_hook` lookup per-symbol audit_id dict + UPDATE row
- New field: `TradingEngine._audit_ids_by_symbol: dict[str, int]`
- New field: `TradingEngine._audit_conn: sqlite3.Connection | None`

### Lane B — Backtest harvest (2 hours compute)
- New script `scripts/harvest_audit_from_backtest.py`:
  - Iterate over strategies × their symbol universes
  - Download or load historical 1h OHLCV (already have 11 symbols × ~2 years)
  - Run `BacktestEngine` with `audit_db_path=data/trades.db` + `audit_run_id="harvest_YYYYMMDD"`
  - Produces thousands of audit rows per strategy
- Expected yield (backtest on 9 altcoins × 2 years × 4 strategies):
  - bb_rsi_mr_opt: ~500-1500 signals
  - donchian_ensemble_adx: ~200-600 signals
  - vol_momentum: ~400-1200 signals
  - funding_carry: run separately on the 4690-epoch BTCUSDT synthetic series → ~60-80 signals

### Lane C — Phase 2 training infrastructure (3-4 hours)
- `src/m3s/signal_filter/cv.py` — `PurgedKFoldT1` splitter (per-sample t1 array variant)
- `src/m3s/signal_filter/train.py` — `fit_meta_classifier(audit_df, strategy_name) → model_path`
  - LightGBM binary classifier with shallow trees
  - Sample uniqueness weights from t1 intervals
  - `CalibratedClassifierCV` with isotonic
  - Train/test split on time (no random shuffle)
  - Return: fitted model + metrics (AUC, Brier, calibration slope)
- `src/m3s/signal_filter/filter.py` — `MetaLabelFilter` live inference class
  - `__init__(model_dir, strategy_name, shadow_mode=True, veto_threshold=0.3)`
  - `on_signal(signal, features, snapshot) → (signal|None, FilterDecision)`
  - Lazy model load + file watcher for hot reload
- `scripts/train_meta_classifier.py` — CLI wrapper for `fit_meta_classifier`

### Lane D — Phase 3 filter integration (1 hour)
- Wire `MetaLabelFilter` into `src/main.py` BEFORE `M3S.on_signal`
- New config section `[meta_label]` in `config/strategies.toml` with `enabled = true`, `shadow_mode = true`
- Shadow filter runs in-line with each strategy's `on_features` → strategy_router call path

### Lane E — Automation (30 min)
- `scripts/launchd/com.algo-trading.m3s-retrain.plist`:
  - Schedule: Sunday 00:03 UTC (`StartCalendarInterval`)
  - Runs `python3 scripts/train_meta_classifier.py --all-strategies`
  - Outputs new model artifacts + metrics report
- Reduce M3S shadow clock threshold from 7 → 4 days
- CronCreate durable wake-ups for Claude decision points

---

## Schedule stored in memory

Persisted via CronCreate (durable mode) + project memory file:

1. **Daily morning status wake-up** — check project progress, report any blockers
2. **M3S authoritative flip wake-up** — at day 4 (2026-04-17 morning)
3. **Weekly meta-classifier retrain** — via launchd, Sundays 00:03 UTC
4. **Shadow checker** — already via launchd, every 6h

---

## Dependencies between phases (parallel-safe)

```
TODAY:
  Lane A (live audit) ─┐
  Lane B (harvest)    ─┤→ Lane C (training)
                       │     ↓
                       │   Lane D (filter integration)
                       │     ↓
                       └─→ Lane E (automation + M3S clock adjust)

DAY 1-3 (automatic):
  launchd m3s_shadow_check   (every 6h)
  launchd m3s_retrain        (weekly)
  engine live running        (continuous)
  CronCreate wake-ups        (daily status)

DAY 4:
  M3S clock hits 4/4 → flip authoritative
  Meta-label shadow → advisory

DAY 5:
  Meta-label advisory → live (if gates pass)
  Project structure DONE
```

---

## Kill switches (maintain safety)

1. **M3S rollback:** `./scripts/m3s_deactivate_shadow.sh` — flips `enabled=false`, restarts engine
2. **Meta-label kill:** set `[meta_label] enabled = false` in config, restart engine
3. **funding_carry kill:** set `[funding_carry] enabled = false`, restart engine
4. **Full engine kill:** `launchctl unload ~/Library/LaunchAgents/com.algo-trading.engine.plist`
5. **Kill file:** `touch data/KILL` (existing watchdog mechanism)

Any CI failure, test suite regression, or risk-server exception during the
compressed sprint triggers an immediate rollback to the last-known-good state
(last commit on origin/main). Deflated Sharpe must not regress.
