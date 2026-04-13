# STATE — Session Continuity Tracker

**Last updated:** 2026-04-13 (Session 22 — M3S complete + shadow checker + 7-day accelerated clock)

---

## Current Position

**Active phase:** 3b part 2 — M3S code complete + shadow checker built. Committed `d8d146f`. Ready for activation.
**Next session target:** Run `./scripts/m3s_activate_shadow.sh` — flips `[m3s] enabled=true`, installs the `com.algo-trading.m3s-shadow-check` launchd agent (fires every 6h), seeds the first report to `data/m3s_shadow_report.md`. Prince can `cat` the report or `python3 scripts/m3s_shadow_check.py --verbose` any time to see status. Clock is **7 days** (accelerated from 4 weeks) — any ERROR exit resets to day 0. After 7 clean days, flip `shadow_mode=false` for authoritative mode.
**Engine status:** Paper trading running via launchd (risk-server + engine + watchdog), HEALTHY. 9 symbols × 1h, $10,000 equity, 0 open positions as of last check. M3S is wired but currently disabled.
**Test suite:** 525 passing, 0 skipped (289 baseline + 227 M3S sub-phases 0.1–0.10 + 9 shadow-checker tests).
**Portfolio:** bb_rsi_mr_opt 40% / donchian_ensemble_adx 30% / vol_momentum 30% — Sharpe 2.318 (backtest, 2yr walk-forward).

> See `ROADMAP.md` for phase table. See `SESSIONS_ARCHIVE.md` for Sessions 8-17.

---

## Session 22 — M3S Phase 3b-2 Design & Sub-phase 0.1 (2026-04-13)

**Super-planning cycle for M3S.** Built research prompt (`docs/planning/m3s_research_prompt.md`) with dual Principal Engineer + Senior Hedge Fund PM persona, 49 design questions, 18-section output format. Plan subagent returned `docs/planning/m3s_plan_v1.md` v1 (629 lines): 4 modes collapsed to 2, LLM advisor deferred, HWM-gated vol-targeted compounding, HRP-lite allocator, rejected BASE/PROFIT pool and per-trade compounding as retail pathologies.

**Prince pushback — retail reality.** Rejected hedge-fund purity on capacity grounds: no LP redemption risk, no market impact, no quarterly scrutiny. Asked for retail-aggressive compounding mode + fully configurable CUSTOM mode + sub-phase breakdown with test+backtest gates between each.

**Resolution — v1.1 ADDENDUM to plan (in-place override).** 4 modes: CONSERVATIVE / STANDARD / **GROWTH** (renamed from RETAILER) / **CUSTOM**. GROWTH = vol 22%, daily compound, DD 10% auto-demote. CUSTOM = user drives every dial with 5 safety rails (opt-in flag, compound_every_n≥1, demote-before-freeze, kelly≤1.0, ceiling≥floor). HWM gate locked on in every mode (variance-drag math is universal — loader force-enables even in CUSTOM with WARN).

**Kelly vs intuition split (documented).** Capital allocation follows Kelly/HRP (risky=less capital). Compounding pace follows Prince's intuition (risky=slower compound) via rolling-Sharpe pace dial clamped per mode. Two separate decisions, two different math paths.

**Tier 1 research sweep — 7 additions.**
1. Regime detection → auto mode switching (sub-phase 0.9, ~300 LOC)
2. Strategy edge-decay detection (sub-phase 0.2 ext, ~50 LOC)
3. Signal conviction-weighted sizing (sub-phase 0.5 ext, ~80 LOC)
4. **Ledoit-Wolf covariance shrinkage** (Ledoit-Wolf 2004/2020, sub-phase 0.4 ext, ~50 LOC)
5. **CVaR tail-risk position scaling** (Rockafellar-Uryasev 2000, sub-phase 0.3 ext, ~80 LOC)
6. **Purged & Embargoed K-Fold CV** (Lopez de Prado AFML ch.7, sub-phase 0.10, ~200 LOC)
7. **Bayesian fractional Kelly from parameter uncertainty** (Baker-McHale 2013, sub-phase 0.10, ~60 LOC)

Meta-labeling (biggest single upside, +0.2-0.5 Sharpe) deferred to Phase 3c — 2-week standalone sprint with LightGBM training pipeline, gated on sub-phase 0.10. Plus Tier 2/3 backlog (10 items) and strategy archetype backlog (10 items) stored in `project_m3s_backlog.md` memory.

**Sub-phase 0.1 — Scaffolding + state layer (COMPLETE).**

New files:
- `src/m3s/__init__.py` — public exports
- `src/m3s/types.py` — msgspec frozen structs: `PortfolioSnapshot`, `StrategySnapshot`, `AllocationDecision`, `CompoundState`, `M3SEventType` enum
- `src/m3s/modes.py` — `M3SMode` enum (CONSERVATIVE/STANDARD/GROWTH/CUSTOM), `ModeConfig` frozen struct, `MODE_PRESETS` for the three fixed modes, `load_mode_from_dict()` with all 5 CUSTOM safety rails + cross-mode rails (cadence, halt>freeze, kelly≤1)
- `src/m3s/state.py` — `M3SStore` SQLite wrapper for `m3s_state` (KV per namespace) and `m3s_events` (append-only log with indexes on ts_ms and event_type), WAL journaling, msgspec JSON encoding
- `tests/test_m3s/{__init__.py,test_types.py,test_state.py,test_modes.py}` — 27 tests (5 types, 9 state, 13 modes)

Verification: **316 tests pass** (289 → 316, +27). Sub-phase 0.1 ships disconnected — nothing in `src/main.py` or `src/risk/` touched. Subsequent sub-phases build portfolio tracker, compounder, allocator, hooks, scheduler, meta-backtest, regime detector, and evaluation layer on top.

**Known gotcha added:** `M3SMode.CUSTOM` is a *different* enum from `RiskMode.CUSTOM`. M3S modes live in `src/m3s/modes.py`, risk modes in `src/risk/modes.py`. Never cross-import. Architectural separation: M3S sits *in front of* the risk server and only shrinks signals; risk modes govern the ZMQ-gated risk checks behind it.

**Sub-phase 0.2 — Portfolio tracker + Edge-decay monitor (COMPLETE, +33 tests).**
`src/m3s/portfolio.py` (PortfolioTracker with HWM/DD/rolling Sharpe via daily-resampled returns + pairwise signal correlation from exposure timelines) and `src/m3s/edge_decay.py` (Tier 1 #2: rolling Sharpe decay vs lifetime + pnl proxy + 14d persistence gate + auto-halve → auto-pause escalation + recovery clearing).

**Sub-phase 0.3 — Compounder + 4 modes + CVaR tail-risk (COMPLETE, +35 tests, BT #1 green).**
`src/m3s/compounder.py` — HWM-gated base advance + 4-factor scalar (vol target × mode rolling-Sharpe pace dial × CVaR tail scalar × DD freeze/halt) × hard-clamped [0, 1.5]. Per-trade cadence implemented for CUSTOM mode with `compound_every_n_trades` counter. Ledoit-Wolf-free CVaR estimator from tracker trade log (Tier 1 #5). BT #1 = 180-day synthetic stream through all 4 modes, verifies HWM monotonicity and scalar de-levering after fat-tail injection.

**Sub-phase 0.4 — HRP-lite allocator + Ledoit-Wolf shrinkage (COMPLETE, +24 tests, BT #2 green).**
`src/m3s/allocator.py` — pure-numpy Ledoit-Wolf linear shrinkage to scaled-identity target (Ledoit-Wolf 2004 closed form, no sklearn dep, Tier 1 #4). Cold-start equal-weight path + mature HRP-lite path (cluster by signal correlation threshold, inverse-vol intra-cluster + inter-cluster). Mode caps (per-strategy + per-cluster) with iterative clip-and-redistribute, excess residual as cash buffer. Audit-ready `inputs_hash` derived from mode + per-strategy metrics + correlation matrix. BT #2 verifies turnover bounded + caps respected across 90-day rolling reallocation.

**Sub-phase 0.5 — Hooks facade + conviction scorer (COMPLETE, +33 tests).**
`src/m3s/hooks.py` — `M3S` composition class wiring tracker + compounder + allocator + edge-decay + conviction into a single interface (`on_signal`, `on_fill`, `on_trade_close`, `on_bar`, `snapshot`, `rebalance`). Hard safety rails: `on_signal` never upscales (clamps `final ≤ original`), never rejects (edge-decay pause sets risk to 0.0), respects shadow mode (logs proposed scaling without mutating). `src/m3s/conviction.py` — multiplicative combination of confidence + volume_z + trigger_distance_atr + mtf_aligned → clamp (Tier 1 #3). Decision log capped at 500 for memory bound.

**Sub-phase 0.6 — Async scheduler + SQLite persistence (COMPLETE, +12 tests).**
`src/m3s/scheduler.py` — `save_state` / `load_state` roundtrip for compound state, latest allocation, mode, edge-decay flags. Graceful boot on missing/corrupt namespace (STARTUP_DEGRADED log, continues with fresh state — never fail closed). `M3SScheduler` async task wrapping `M3S.rebalance()` + `save_state` + event log append on fixed cadence, with error isolation (tick failures increment error counter but don't stop the loop). `make_scheduler` factory for main.py integration.

**Sub-phase 0.7 — Meta-backtest simulator (COMPLETE, +11 tests, BT #3 green).**
`src/m3s/meta_backtest.py` — replay per-strategy trade logs through live M3S instance, apply PnL via `scaled_risk_pct / original_risk_pct` multiplier (linear under backtest engine, matches the Session 20 bit-exact TV match). Baselines: fixed-weight / inverse-vol / M3S CONSERVATIVE / STANDARD / GROWTH side-by-side. Metrics: daily-resampled Sharpe, max drawdown, Calmar, average turnover. BT #3 verifies all baselines finish positive on +EV stream and CONSERVATIVE DD ≤ GROWTH DD on a synthetic crash.

**Sub-phase 0.8 — Wire into main.py DISABLED (COMPLETE, +8 tests).**
`src/main.py` — imports M3S modules, adds `self.m3s / m3s_store / m3s_scheduler / _m3s_scheduler_task` fields, `_maybe_init_m3s(cfg)` builds the composition facade if `cfg.m3s.enabled` (default false), `on_signal` hook before `risk_client.check_signal` with try/except isolation, `on_trade_close_hook` wired via new `PaperExecutor.on_trade_close_hook` attribute, scheduler task started in `start()`, state saved on `stop()`. New `[m3s]` section in `config/settings.toml` with `enabled = false` + `shadow_mode = true` defaults. Smoke tests verify: imports clean, disabled-default is no-op, enabled path constructs live M3S, invalid mode falls back to STANDARD, paper-executor hook invokes on close + survives exceptions.

**Sub-phase 0.9 — Regime detector + auto mode switching (COMPLETE, +21 tests, BT #4 green).**
`src/m3s/regime.py` — `RegimeClassifier` with pure rule-based classification from (BTC realized vol, BTC ADX, max portfolio correlation) → `Regime` (LOW_VOL_TREND / NORMAL / HIGH_VOL / CRISIS) with crisis-first precedence. `AutoModeSwitcher` wraps classifier + M3S, applies transitions with 3 gates: cooldown (no back-to-back auto changes), manual override (respect human), and promote-up blocker (CONSERVATIVE→STANDARD/GROWTH auto-promotion disabled by default per v1 plan rule — "re-upping after a drawdown is the most emotional decision"). BT #4 verifies regime→mode mapping sequence and cooldown enforcement. Tier 1 #1.

**Sub-phase 0.10 — Purged CV + Bayesian fractional Kelly (COMPLETE, +23 tests, BT #5 green).**
`src/m3s/evaluation.py` — `purged_kfold_splits` generator with purging (removes training samples whose labels overlap test window) + embargoing (gap after test to kill serial-correlation leak). Per-fold Sharpe annualized to √365, deflated Sharpe (Bailey & Lopez de Prado 2014 simplified), `bayesian_fractional_kelly` (Baker-McHale 2013): `f* = μ̂ / (σ² + σ_μ²)`, clamped [0.20, 0.50]. New strategies with high σ_μ² land near ⅕-Kelly automatically; mature strategies approach ½-Kelly. `evaluate_strategy` ties everything together into a `StrategyEvaluation` record. Tier 1 #6 + #7.

**M3S totals:** 10 sub-phases, ~3,400 LOC across `src/m3s/` (13 modules + `__init__.py`), 227 new M3S tests, 5 backtest gates all green, wired into `src/main.py` with `enabled=false` default. Nothing in `src/risk/` was modified. Committed as `d8d146f feat(m3s): Phase 3b-2 complete — 10 sub-phases, 7 Tier 1 additions`, pushed to `origin/main`.

**Shadow checker + 7-day accelerated clock (post-commit Session 22 addendum).**

Prince rejected the 4-week passive clock as too slow; replaced with an active checker that runs every 6h via launchd.

New files:
- `scripts/m3s_shadow_check.py` — 6h validator that reads `data/m3s.sqlite` (m3s_events + m3s_state) and `data/trades.db` (paper_equity), runs 7 checks: `state_loadable`, `scheduler_health`, `hwm_monotonic`, `no_dd_frozen_compound`, `cluster_caps`, `allocation_churn`, `shadow_vs_actual` (M3S base vs paper equity divergence). Writes human-readable `data/m3s_shadow_report.md`, machine-readable `data/m3s_shadow_status.json`, append-only `data/m3s_shadow_alerts.log`, and a `data/m3s_shadow_clock.json` day counter. Exit codes 0/1/2 for healthy/warning/error.
- `scripts/launchd/com.algo-trading.m3s-shadow-check.plist` — launchd StartInterval=21600 (6h), RunAtLoad=true, logs to `data/logs/m3s_shadow_check{,_err}.log`. Cron-style periodic (no KeepAlive).
- `scripts/m3s_activate_shadow.sh` — 5-step activation helper: flips `enabled=true`, kickstarts the engine via launchd, copies the plist to `~/Library/LaunchAgents/`, loads it, fires the first check, prints report paths.
- `scripts/m3s_deactivate_shadow.sh` — reverse of activate: flips `enabled=false`, unloads + removes the plist, restarts engine. Leaves report files intact for audit.
- `tests/test_m3s/test_shadow_check.py` — 9 tests covering disabled state, healthy path, each invariant violation, divergence detection, promotion clock increment + reset.

Rollout change:
- **4 weeks → 7 days.** The clock increments by 1 per calendar day when the checker exits 0 (HEALTHY) or 1 (WARNING). Any exit 2 (ERROR — hard invariant violation) resets the clock to 0. After 7 consecutive clean days, Prince can flip `shadow_mode=false` for authoritative mode. Philosophy: trust an active checker that runs 28 times across 7 days instead of passive wall-clock time.
- **Promotion criteria:** zero hard violations, divergence < 1%, scheduler ticks within 20% of expected cadence, 7 consecutive clean days.

Verification: 516 → **525 tests passing** (+9 shadow-checker tests). Shadow checker runs cleanly in DISABLED state (expected, since M3S hasn't been activated yet). Ready to fire.

Usage:
- Activate: `./scripts/m3s_activate_shadow.sh`
- Check status any time: `cat data/m3s_shadow_report.md` or `python3 scripts/m3s_shadow_check.py --verbose`
- Deactivate: `./scripts/m3s_deactivate_shadow.sh`
- Rollback from ERROR: run `m3s_deactivate_shadow.sh` or just flip `[m3s] enabled = false` and restart

---

---

## Session 21 — Doc System Consolidation (2026-04-13)

**Problem:** Two wrong "next phase" recommendations in one session (IC Markets / 30-day validation) traced to stale phase status duplicated across 4+ files (`MASTER_PLAN`, `FULL_PLAN`, `BLUEPRINT`, `README`, stale `STATE` sub-table).

**Action:** Restructured docs to single-source-of-truth:
- CREATE: `ROADMAP.md` (canonical phase tracker), `docs/M3S_SPEC.md`, `SESSIONS_ARCHIVE.md`, `docs/archive/`
- MODIFY: `STATE.md` slimmed to current-position + Sessions 18-20, `MASTER_PLAN.md` phase list removed, `README.md` phase table removed, `BLUEPRINT.md` renamed + frozen, `CLAUDE.md` gained Autonomous Workflow Protocol section
- CREATE: `scripts/doc_lint.py` regression guard
- DELETE (after verification): `FULL_PLAN.md`, stale `project_phase2_decision_point.md` memory
- Memory: rewrote `project_algo_trading.md`, added `project_doc_system.md` + `feedback_autonomous_docs.md`

**Outcome:** Each fact now lives in exactly one file. Phase status drift is structurally impossible — all other files link to ROADMAP. Autonomous Workflow Protocol codifies the update cadence so this regression cannot recur.

---

## SESSION 20: Deep Correctness Testing — Match Every Number Against Reality — COMPLETE

> *Phase numbering note: Phases 4 and 5 were planned as additional TradingView indicator cross-checks but were skipped after TV wasn't available in the next session. Their coverage intent is subsumed by Phase 3 (bit-exact TV indicator cross-val) + Phase 6 (848/848 trade match against TV Strategy Tester). No residual work.*

### Phase 0: PaperExecutor Commission Bug Fix — COMPLETE

**Bug:** Opening commission computed but never deducted from equity in `_open_position()` and `execute_order()`. BacktestEngine charges both sides at close — silent P&L divergence.

**Fix:** Added `self._equity -= commission` after commission calculation in both methods.

### Phase 1: PaperExecutor Exact Math Tests — COMPLETE (19 tests)

| Test Class | Tests | What's Pinned |
|---|---|---|
| `TestPositionSizingExact` | 6 | 5 parametrized stop-loss cases + no-stop fallback, exact qty to 10+ decimals |
| `TestCommissionExact` | 4 | Open commission, close commission, roundtrip total, equity deduction on open (bug fix verification) |
| `TestRoundtripPnLExact` | 5 | Long win, short win, long loss, short loss, breakeven — ALL intermediates checked |
| `TestExecutorMathEdgeCases` | 4 | HOLD noop, multi-symbol concurrent, zero entry rejected, rapid open-close-open |

**Key:** Every test asserts exact intermediate values (fill_price, qty, commission, equity_after_open, gross_pnl, net_pnl, final_equity) — not just "positive" or "greater than zero."

### Phase 2: Indicator Hand-Calculated Tests — COMPLETE (28 tests)

Hand-calculated golden values for SMA, EMA, Donchian, RSI (Wilder), ATR (Wilder), BBands (ddof=0), MACD, ADX, Stochastic, VWAP. Each test embeds known-value math derived from the formulas directly, independent of the `ta` library.

### Phase 3: TradingView Cross-Validation — COMPLETE (16 tests, bit-exact)

Pivoted from live-bar drift tolerance-based testing to the **closed-bar reference method**: golden values pinned to an immutable historical bar (`reference_bar_index=197`, timestamp 2026-04-12 15:00 UTC), captured manually from TV Data Window via one-indicator-at-a-time workflow.

| Indicator | TV value | Ours | Tolerance |
|---|---|---|---|
| EMA_9 | 71,217.74 | 71,217.7449 | 0.01 |
| RSI_14 | 28.74 | 28.7419 | 0.01 |
| BBU/M/L_20 | 73559.98 / 71871.05 / 70182.12 | match | 0.01 |
| MACD / signal / hist | -481.01 / -379.70 / -101.31 | match | 0.01 |
| ATR_14 (RMA) | 341.59 | 341.5946 | 0.01 |
| ADX_14 | 40.4512 | 40.4512 | 0.0001 |
| DI+_14 / DI-_14 | 8.5340 / 37.6365 | match | 0.0001 |
| STOCHd_14 (=TV %K @ default 14,3,3) | 7.96 | 7.9566 | 0.01 |

**Zero skips, bit-exact at TV display precision.** MCP silent failures on `indicator_set_inputs` / pane-isolated indicators resolved by asking Prince to operate TV manually. Workflow + gotchas saved to memory (`feedback_manual_tv_testing.md`, `reference_indicator_verification_setup.md`) and ARCHITECTURE.md Known Gotchas.

**Total tests: 246 passing, 0 skipped → grew to 289 by end of session.**

### Phase 6: Backtest Engine vs TradingView Strategy Tester — COMPLETE (bit-exact, 848/848)

**Strategy:** SMA(10)/SMA(20) long-only crossover, BTCUSDT 1H, 2023-01-01 → 2026-04-13, commission=0, slippage=0, 100% of equity sizing, no pyramiding, process_orders_on_close=false.

**Pipeline:** `scripts/verify_strategy_vs_tv.py backtest` runs our engine and dumps trades to `tests/fixtures/our_strategy_trades.json`. Pine strategy `SMA Crossover Verify` ran in TV Strategy Tester; CSV exported and converted to `tests/fixtures/tv_strategy_trades.json` (848 rows). `... compare` compares trade-by-trade.

All 848 trades matched within $0.07 final equity (rounding-level divergence).

---

## SESSION 19: Institutional-Grade Pipeline Hardening — COMPLETE

### Phase 1: SOLUSDT Validation — COMPLETE

| Symbol | Sharpe | PF | Trades | Verdict |
|---|---|---|---|---|
| SOLUSDT | -1.424 | 0.0 | 1 (losing) | **FAIL** — SOL trends too hard for mean reversion |
| APTUSDT | 1.424 | ∞ | 1 (winning) | **PASS** — replaced SOLUSDT in bb_rsi_mr |

**Action taken:** Replaced SOLUSDT → APTUSDT in `config/strategies.toml` bb_rsi_mr markets. Engine restarted (PID 61310), APTUSDT warmup successful, all 9 symbols online.

**Note:** Both symbols generated only 1 trade in 180d — bb_rsi_mr is highly selective with strict RSI 25/75 + ADX < 20 filters. Known limitation.

### Phase 2-5: Pipeline Tests — COMPLETE

Installed `pytest-asyncio` (v1.3.0) for proper async test infrastructure — fresh event loop per test, no state leakage.

| File | Tests | What's covered |
|---|---|---|
| `tests/test_data/test_candle_builder.py` | 8 | Tick→candle, OHLCV correctness, kline passthrough, dedup suppression, multi-TF independence, boundary alignment, zero-tick guard |
| `tests/test_data/test_feature_engine.py` | 7 | Feature emission, buffer trimming, indicator freshness (RSI changes on new data), min-rows guard, COMPUTE_WINDOW convergence (<0.01% error), no FutureWarning, NaN handling |
| `tests/test_execution/test_paper_executor.py` | 7 | Open long/short, close P&L (long+short), duplicate rejection, close nonexistent, slippage direction |
| `tests/test_data/test_warmup.py` | 4 | needed_pairs filtering, cartesian fallback, callback restoration (normal + error path) |
| `tests/test_strategies/test_router.py` | 3 | Symbol/TF routing, empty route, exception isolation |
| `tests/test_risk/test_client_numpy.py` | 5 | numpy float64/int64 enc_hook, non-numpy raises, Signal serialization, metadata with numpy |

**Total new tests: 34. Previous: 123. New total: ~157.**

**Key finding from tests:** `parquet.load()` exception in warmup propagates (not caught by download fallback) — but `finally` block correctly restores the callback. Not a bug, but worth noting: if Parquet storage is corrupted, warmup fails hard rather than falling back to download.

### Phase 6: Heartbeat Signal Metrics — COMPLETE

Added 3 new fields to `data/heartbeat.json`:

| Field | Source | Purpose |
|---|---|---|
| `signal_count` | `main.py:_signal_count` | Total signals fired since startup |
| `rejection_count` | `main.py:_rejection_count` | Signals rejected by risk server |
| `strategy_exceptions` | `router.py:_exception_count` | Strategy crashes (exception isolation) |

**New status level:** `WARNING` — triggers when `strategy_exceptions > 5`. Priority: KILLED > STALE > WARNING > HEALTHY.

**Files modified:** `src/main.py` (signal/rejection counters), `src/strategies/router.py` (exception counter), `src/monitoring/heartbeat.py` (new fields + WARNING status).

Engine restarted (PID 61932), 9/9 symbols online. **157 tests pass (123 existing + 34 new).**

---

## SESSION 18: Pipeline Optimization — 5 Fixes — COMPLETE

### What was done

Deep analysis of the live pipeline revealed 5 performance/correctness issues. All fixed without over-engineering.

| Fix | Files | What |
|---|---|---|
| 1. Duplicate candle dedup | `src/data/candle_builder.py` | Both tick-aggregation AND Binance klines emitted candles to FeatureEngine. Added `_kline_pairs` set — auto-detects exchange kline pairs and suppresses tick-built candles for them. |
| 2. Strategy-driven subscriptions | `src/main.py`, `src/data/feeds/binance_ws.py`, `src/data/warmup.py` | Added `_get_needed_pairs_from_config()` → `needed_pairs` set. WS only subscribes to kline streams strategies actually use (9 vs potential 18+ cartesian). Warmup only downloads needed (symbol, tf) combos. |
| 3. In-place DataFrame append | `src/data/feature_engine.py` | Replaced `pd.concat([df, new_row])` with `df.loc[len(df)] = row_dict`. No more full-buffer copy + GC churn per candle. Eliminates FutureWarning. |
| 4. Compute window optimization | `src/data/feature_engine.py` | Added `COMPUTE_WINDOW=250`. Indicators computed on tail slice instead of full 500-row buffer. 2x less work, identical results (all indicators converge within 250 rows). |
| 5. msgspec numpy enc_hook | `src/risk/client.py` | Added `enc_hook=_numpy_enc_hook` to msgspec Encoder. Prevents crash when Signal fields contain numpy.float64 from strategy calculations. |

### Verified

1. Engine restarted (PID 60353) — all 9/9 symbols warmed up, signals fire without crash
2. Heartbeat: HEALTHY, 1,959 ticks in 60s, CPU 1.3% (stable)
3. `kline_streams: 9` logged — only needed pairs subscribed (not cartesian product)
4. No FutureWarning in logs (pd.concat removed)
5. Risk client serialization: `_numpy_enc_hook` tested with numpy.float64 → works
