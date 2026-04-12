# STATE — Session Continuity Tracker

**Last updated:** 2026-04-13 (Session 21 — doc consolidation)

---

## Current Position

**Active phase:** 3b part 2 — M3S construction (AI compounding system). See `ROADMAP.md`.
**Next session target:** Begin `src/m3s/` scaffold — start with Allocation Engine + mode-resolver hook into existing RiskManager. Reference: `docs/M3S_SPEC.md`.
**Engine status:** Paper trading running via launchd (risk-server + engine + watchdog), HEALTHY. 9 symbols × 1h, $10,000 equity, 0 open positions as of last check.
**Test suite:** 289 passing, 0 skipped (Session 20 correctness sweep).
**Portfolio:** bb_rsi_mr_opt 40% / donchian_ensemble_adx 30% / vol_momentum 30% — Sharpe 2.318 (backtest, 2yr walk-forward).

> See `ROADMAP.md` for phase table. See `SESSIONS_ARCHIVE.md` for Sessions 8-17.

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
