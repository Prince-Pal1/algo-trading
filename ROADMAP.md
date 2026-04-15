# Algo Trading — Roadmap & Phase Status

**Last updated:** 2026-04-15 (Session 22 Day 2 — SWIFT matrix + leverage validation + walk-forward on `feat/gold-refactor`; 6 new commits since yesterday's merge; shadow orchestrator regression caught + fixed during merge-prep)
**Current phase:** TWO parallel work streams:
- **3b-2 M3S** + **3c meta-labeling** in shadow mode on `main` — clock-blocked toward the 2026-04-16 auto-promotion cron. Unchanged from yesterday. M3S shadow clock day 2/4 (compressed from 7).
- **Phase G Gold Leveraged Stack** on `feat/gold-refactor` — yesterday's post-merge state extended by tasks #109 (240-cell SWIFT matrix + `book.open_position` float-precision fix), #110 (leverage simulation deep-dive validation — matrix is mathematically correct, engine `leverage` is max-margin cap NOT position multiplier), and #111 (SWIFT walk-forward OOS — **gate FAIL** under continuous-run metric Calmar 0.488, SWIFT stays research-only). Institutional sub-book stays at 2 strategies (`donchian_gold` + `vol_momentum_gold`). 1183 tests passing.
**Next action:** 2026-04-17 ~09:30 merge window — fast-forward `feat/gold-refactor` → `main` (6 new commits: `241092f` + `ebbcea9` + `b2ed46b` + `45b17b1` + `fe9384d` + `37d5695`). Rehearsal on 2026-04-15 auto-resolved cleanly with zero conflicts against `origin/main` (main's watchdog-patch + forensic-audit commits already pulled in via `37d5695`). Interim: `cat data/project_status.md` for main's single-pane status; `./scripts/promote_m3s_authoritative.sh --dry-run` re-runs all 4 gates on main.

> **Authority note:** This file is the **only** authoritative source for phase status. If any other file contradicts this, that other file is wrong — fix it to link here. See `CLAUDE.md` § Autonomous Workflow Protocol.

---

## Phase Table

| # | Phase | Milestone | Status | Evidence | Blockers |
|---|---|---|---|---|---|
| 1 | Data Foundation | WS feed → candle → indicators → storage working | ✅ COMPLETE | Sessions 1-5 | — |
| 2 | Strategy + Backtest + Paper | Sharpe >1.0 on 2-year walk-forward | ✅ MILESTONE MET | Session 9 (1.307 baseline), Session 11 (optimized 2.318) | — |
| 3a | Risk Manager — core | 6 pre-trade checks + circuit breakers + kill switch + ZMQ isolation | ✅ COMPLETE | Session 12 | — |
| 3b-1 | Adaptive Risk Modes | 4 modes (AGGRESSIVE/BALANCED/DEFENSIVE/CUSTOM) + per-strategy profiles + mode backtest verification | ✅ COMPLETE | Session 13 | — |
| **3b-2** | **M3S — Master Money Management System** | 10 sub-phases; meta-backtest Sharpe ≥ 2.35 | ✅ **CODE COMPLETE — SHADOW MODE PENDING** | Session 22 (all 10 sub-phases, 227 new M3S tests, 516 total) | Prince review + BT gates + 4-week shadow run |
| 3-milestone | Paper run — no losses beyond limits for 30 days | 30 consecutive days clean operation | 🟡 IN PROGRESS (passive clock) | Engine launched Session 16; 289 tests pass Session 20 | Operational stability + M3S integration |
| 4 | Multi-Exchange Execution | Paper trades on 2+ exchanges, <200ms latency | ❌ NOT STARTED | — | IC Markets cTrader is gold-only and **deferred** until a gold strategy exists |
| 5 | AI Agent Intelligence (Meta-Strategist, News, Risk Sentinel) | Sharpe uplift >0.2 vs baseline | ❌ NOT STARTED | — | M3S must land first (The PhD is the foundation) |
| 6 | Options Module | Iron condor SPX positive 1-year | ❌ NOT STARTED | — | — |
| 7 | Production Deployment | 7 days unattended on VPS | ❌ NOT STARTED | — | Phase 3-milestone + M3S |
| 8 | Scale | 3+ strategies portfolio Sharpe >1.5 | 🟡 PARTIAL — 3 strategies live, Sharpe 2.318 in backtest only | Session 10-11, 14-16 | OOS portfolio validation on live paper data |
| DISCOVERED 2026-04-14 | Watchdog data-staleness patch | Kill engine when `last_candle_age_s > 1800s` even if file mtime is fresh | ✅ COMPLETE | `7c5d000` main / `fcb13b9` gold; ARCHITECTURE.md Known Gotchas 2026-04-14 | — |
| **G** | **Gold Leveraged Stack** | Two-book (institutional + aggressive) engine + donchian_gold + vol_momentum_gold + 3 Tier 5 strategies + split sweep + cost-fix sweep + walk-forward retunes | ✅ **G.2 + G.5b + tasks #101-108 COMPLETE — merged to main 2026-04-14** (3 days early) | 44 commits 54c0b62 → a7703e9 | — |

**Legend:** ✅ complete · ❌ not started · 🟡 in progress/partial · **DISCOVERED** = unplanned work surfaced mid-session (Rule 2)

---

## Phase G — Gold Leveraged Stack (feat/gold-refactor branch)

Master plan: `~/.claude/plans/parallel-noodling-goblet.md`. Branch: `feat/gold-refactor` in worktree `/Users/prince/algo-trading-gold`. Main dir untouched through the in-flight sprint wakeups.

### Sub-phase Table

| # | Name | Status | Commit | Tests | Notes |
|---|---|---|---|---|---|
| G.0 | M3S forex-readiness refactor (`periods_per_year`) | ✅ COMPLETE | `9a78fd7` | — | Unblocks gold Sharpe math |
| G.0b | Instrument metadata registry + market hours | ✅ COMPLETE | `20707f2` | — | XAUUSD registered, is_market_open |
| G.0c.1 | Signal.leverage + BaseStrategy.leverage_range | ✅ COMPLETE | `374cab9` | — | Leverage first-class schema |
| G.0c.2 | config/risk.toml [leverage] + RiskManager hard gates | ✅ COMPLETE | `5bef185` | — | 3 gates (per-position, aggregate, liquidation buffer) |
| G.0c.3 | Compounder.target_leverage regime picker | ✅ COMPLETE | `3793dd8` | — | Mode-aware range interpolation |
| G.1 | Dukascopy XAUUSD 1h download + existing strategies | ✅ COMPLETE | `54c0b62` | — | donchian_ensemble_adx: +35.65% / Sharpe 1.357 / 147 trades (2yr) |
| G.2a | Leveraged Book + LeveragedPosition + 50% stop-out | ✅ COMPLETE | `f514862` | 32 | CFD-accurate margin accounting, institutional + aggressive sub-books |
| G.2a.2 | ICMarketsMetalFeeModel (spread + commission + news) | ✅ COMPLETE | `a803529` | 20 | 0.13 pip spread + $3/lot/side commission |
| G.2a.3 | Brownian bridge intrabar path reconstruction | ✅ COMPLETE | `4b6edd0` | 27 | Deterministic via (run_id, bar_idx) |
| G.2a.4 | LeveragedBacktestEngine (fresh file) | ✅ COMPLETE | `90fc6c7` | 10 | New engine, zero risk to existing crypto engine |
| G.2b | M3S.request_leverage + geometric-mean blend + grants store | ✅ COMPLETE | `ae6213e` | 30 | 5 reason codes, leverage damping, data/trades.db::leverage_grants |
| G.2d | donchian_gold (session-filtered, leverage_range=(10,50)) | ✅ COMPLETE | `4907e7c` | 10 | London/NY session gate; L∈{1,5,10,25,50} sweep script |
| G.2e | Tier 5 infra (structure_levels, aggressive compounder, news calendar) | ✅ COMPLETE | `d847e4c` | 27 | 49 news windows 2025-01→2026-04 + sub-book config |
| G.2f | Tier 5 strategies (candle_burst, news_fade, hedged_structure) | ✅ COMPLETE | `19592de` | 13 | All 3 aggressive strategies + state machine |
| G.2c | Inline leverage gates + RCU portfolio view + AGGRESSIVE_RETAIL profile | ✅ COMPLETE | `f891e57` | 14 | InlineLeverageGates + VersionedPortfolioView + profiles.aggressive_retail |
| G.2g | Split sweep: Calmar-optimal institutional_pct | ✅ COMPLETE | `05494c8` | 1 | run_multi engine path + run_split_sweep.py; optimal = 0.95 (1h data, untuned aggressive) |
| Merge | feat/gold-refactor → main | 📋 SCHEDULED | — | — | 2026-04-17 ~09:30 after Day 4 sprint cron |
| Tuning | donchian_gold tuning + XAUUSD M5 download + Tier 5 flagged not-alpha-ready | ✅ COMPLETE | `c575fef` | — | donchian_gold tuned: +38.16% / 12% DD / Calmar 1.675 / Sharpe 1.275 on 1h. Tier 5 strategies need dedicated alpha research (infrastructure correct, entry triggers unviable). Split sweep post-tuning: institutional_pct=0.95 → +14.55% / 20% DD / Calmar +0.382 |
| Phase 4 skeleton | IC Markets cTrader feed + executor + OAuth helper (pip install ctrader-open-api) | ✅ COMPLETE | `083c50a` | 15 | `icmarkets_feed.py` + `icmarkets_executor.py` + `ctrader_token_helper.py` + `.env.example` all built. Unit tests with mocks green. App registered at openapi.ctrader.com; credentials in hand; sandbox scopes require "Active" KYC status (up to 3 business days). |
| **G.2h** | **Parallel KYC-wait sprint** (7 sub-items, plan in `parallel-noodling-goblet.md`) | 🟡 IN PROGRESS | — | — | During Spotware KYC wait, ship: shadow orchestrator, M1 download, vol_momentum_gold, LeverageBudgetAllocator, walk-forward retune, M5 donchian retune, Tier 5 infra validation, preflight smoke |
| G.2h.2 | Shadow orchestrator + ParquetReplayFeed | ✅ COMPLETE | `cf23979` | 10 | Matches LeveragedBacktestEngine bit-exact on 2yr XAUUSD 1h (+38% / 12% DD / 69 trades / 0 stop-outs). 945 tests passing. |
| G.2h.5a | XAUUSD M1 download (2 years, 706,212 bars) | ✅ COMPLETE | `cf23979` | — | Kicked off as background task; Dukascopy returned 706,212 M1 bars 2024-04-14 → 2026-04-13 (16.5 MB parquet). No code changes. |
| G.2h.1 | vol_momentum_gold (second institutional strategy) | ✅ COMPLETE | `a7f0f11` | 6 | Tuned (mw=240, vl=240, vt=0.20, sl=3.0, long_only=True, session=True): +20.36% / 7.86% DD / Calmar 1.365 / Sharpe 1.103 / 120 trades. Correlation with donchian_gold = +0.2263 (well under 0.5 gate). Both gates PASSED. Tier enum added to utils/types.py; BaseStrategy.__init__ now takes `tier` kwarg. |
| G.2h.4 | LeverageBudgetAllocator (G.4 pulled forward) | ✅ COMPLETE | `a7f0f11` | 11 | `src/m3s/leverage_budget.py` — floor + dynamic pool allocator, per-tier budgeting, Sharpe-weighted dynamic split, requests cap grants. 11 unit tests across empty-floors, floors-only, mixed, cold-start, multi-strategy-per-tier, empty-requests. |
| G.2h.6 | Walk-forward donchian_gold retune | ✅ COMPLETE | `a7f0f11` | — | 6 folds (3mo train × 1mo test). **Original (broken cost model):** Mean OOS Calmar +8.515 ± 9.5, Sharpe +2.001 ± 2.5, 30 trades. **Re-run 2026-04-14 task #108 under corrected cost model:** Mean OOS Calmar **+13.678 ± 11.5** (+60% margin widening), Sharpe **+2.930 ± 2.7** (+47%), 33 OOS trades, mean OOS return +6.83%/mo, mean OOS DD 5.12%. 5/6 folds profitable (only fold 4 losing). Gate PASSED by 45×. |
| G.2h.6b | Walk-forward vol_momentum_gold retune | ✅ COMPLETE | `<pending>` | — | 6 folds, same schedule. **Original (broken cost model):** Mean OOS Calmar +5.582 ± 9.4, Sharpe +0.912 ± 3.2, 47 trades. **Re-run 2026-04-14 task #108 under corrected cost model:** Mean OOS Calmar **+11.219 ± 13.0** (+101% margin widening), Sharpe **+1.975 ± 3.2** (+117%), 47 OOS trades, mean OOS return +3.99%/mo, mean OOS DD 3.70%. 4/6 folds profitable (losers: fold 2 −4.84%, fold 6 −0.42%). **Losing folds don't overlap with donchian_gold's** — real diversification. Gate PASSED by 37×. |
| DISCOVERED 2026-04-15 | SWIFT 240-cell matrix (task #109) + book.py float-precision fix | 4 windows × 4 TFs × 5 leverages × 3 fees = 240 backtests on SWIFT + HTML/PDF/CSV report. Phase 1-5 validation found a critical engine bug: `book.open_position` rejected positions where `entry_margin == free_margin` exactly (1x leverage with risk-based sizing where notional == equity). Pine_zero produced 77 trades but cTrader produced 307 on the same data. Fixed with 1-cent epsilon in book.py:386. 2 regression tests added. **Best deployable cell: 15m × MT4 × 1y → +25.57% / 11.96% DD / Calmar 2.14**. The 5m TF (which task #107 used) is unviable on full year (-2.22% MT4, -17.61% cTrader). | ✅ COMPLETE | `ebbcea9` | — |
| DISCOVERED 2026-04-15 | SWIFT matrix leverage validation deep-dive (task #110) | Re-validated matrix #109's identical-P&L-across-leverages claim with hand-trace + 7 unit tests + 1536-assertion data audit + alternative-sizing demo. Verdict: matrix is mathematically correct. Leverage = max margin cap, NOT position multiplier. P&L invariance is a property of SWIFT's `risk_pct == sl_pct` sizing. Counter-demo shows framework produces leverage variance when strategy opts in (scale 1×=+13.55%, 5×=+73%, 50×=−90% wipeout). Locked invariants in `test_leverage_invariance.py`. Report at `reports/swift_leverage_validation_2026-04-15.md`. | ✅ COMPLETE | `b2ed46b` | — |
| DISCOVERED 2026-04-15 | SWIFT walk-forward OOS validation (task #111) | 7 non-overlapping 90d OOS folds, fixed Pine params, 15m × MT4. **Per-fold mean OOS Calmar +1.124 ± 3.126** (technically passes 0.5 gate but std 2.8× mean → regime-dependent). **Continuous 630d run: +15.78% / 18.73% DD / Calmar 0.488 — FAILS 0.5 gate.** 4/7 profitable folds. Fold 3 catastrophic (−7.89% / 17.40% DD in Q1 2025 chop regime), fold 5 outstanding (+7.24% / 4.21% DD, Q3 2025 trend regime). Matrix #109's +25.57% / Calmar 2.14 on 1y was a favorable-window cherry-pick. Order of magnitude weaker than donchian_gold (Calmar 13.73) and vol_momentum_gold (Calmar 11.22). **Verdict: SWIFT stays research-only; institutional sub-book stays at 2 strategies for the 2026-04-17 merge.** Report at `reports/swift_walk_forward_2026-04-15.md`. | ❌ GATE FAIL (continuous) | `45b17b1` | — |
| DISCOVERED 2026-04-15 | Deep Backtest framework — generic SWIFT-style pipeline (task #112) | Distilled the SWIFT deep-backtesting process (tasks #109/#110/#111) into a reusable generic pipeline invokable as `python3 scripts/deep_backtest.py <strategy>`. 6 phases: preflight → matrix (windows × TFs × leverages × fees cells) → sanity checks → **auto-triggered leverage validation** (fires when matrix shows P&L invariance across leverages, runs hand-trace comparison between 1x and max lev + confirms margin scales inversely) → walk-forward OOS (dt-based fold slicing, optional per-fold retune via `wf_param_grid`, continuous-run cross-check) → verdict (DEPLOYABLE/RESEARCH_ONLY/NEEDS_WF/FAILED). Generates SWIFT-style report: CSV + PNG heatmaps + HTML + landscape-A4 PDF. New files: `src/backtest/deep_backtest.py` (~800 LOC), `src/backtest/deep_backtest_report.py` (~500 LOC), `scripts/deep_backtest.py` (~200 LOC CLI), `tests/test_backtest/test_deep_backtest.py` (11 tests, 107s runtime). Registered `swift_alma`, `donchian_gold`, `vol_momentum_gold` in STRATEGY_REGISTRY for name-based lookup. | ✅ COMPLETE | `0d67908` + `512b84d` (indicators+Phase3 guard bugfix) | — |
| DISCOVERED 2026-04-15 | Deep Backtest cross-strategy validation + production baselines | Ran the framework with full matrix (4 windows × 1h × 5 leverages × 3 fees = 60 cells) + WF retune (task #108 param grids, 6 folds × 30d test / 90d train) against both deployable gold strategies. **donchian_gold**: verdict DEPLOYABLE, best cell 1mo × 10x × pine_zero → +5.06% / Calmar 16.33, WF mean Calmar +29.0 ± 29.4, continuous Calmar **+10.64**, 5/6 profitable folds. **vol_momentum_gold**: verdict DEPLOYABLE, best cell 6mo × 10x × pine_zero → +14.20% / Calmar 16.47, WF mean +13.29 ± 26.8, continuous Calmar **+6.28**, 3/6 profitable (more marginal than donchian). Continuous-run Calmars land close to task #108's mean-OOS numbers (donchian +13.68, vol_momentum +11.22) — framework is at feature-parity with the bespoke WF scripts. Added 4 new gold-path unit tests locking in the indicator-precompute code path (`TestGoldStrategyIndicatorPrecompute`: trade-firing assertions for both strategies, explicit indicators override, wf_retune + wf_param_grid end-to-end). 15/15 deep_backtest tests passing (126s). Reports at `reports/deep_backtest_{donchian_gold,vol_momentum_gold}_2026-04-15_0829*/`. | ✅ COMPLETE | `d80c683` | — |
| DISCOVERED 2026-04-15 | Leverage + position-sizing strategy design research — super plan (task #113) | Super planning workflow with **principal engineer + senior hedge money manager** persona (return-maximizing, drawdown-tolerant, leverage-aggressive). Traced current leverage infrastructure (`book.py` margin gate, `leveraged_engine.py:536-550` sizing math, `costs.py` notional-based commission, `m3s/hooks.py:145-236` request_leverage gating, `compounder.py:237-263` leverage damping). Key finding: **all 3 gold strategies are leverage-passive** — none scale position size with the engine leverage parameter. Current deployment leaves 2-4× return headroom on the table. Proposed 6-mode `LeverageMode` enum schema (INVARIANT/MARGIN_CAPPED/VOL_TARGETED existing + RISK_SCALED/KELLY_FRACTIONAL/DRAWDOWN_BUDGETED new) with per-mode Phase 2 sanity checks + return-biased verdict gates + CLI flags. Deployment recommendations: donchian_gold Mode RISK_SCALED at L=15 (half-Kelly, 2.5× returns at 2× DD), vol_momentum_gold VOL_TARGETED+RISK_SCALED composed at L=20 with vol_target bumped 0.20→0.30, swift_alma stays research-only (fold 3 catastrophe makes scaling unsafe). External research: Kelly criterion literature (Chan, Downey, IBKR), prop firm leverage norms (Alpha Capital, Tradeify 2026). **Research + design doc written; NO code changes.** Implementation is a separate later plan (Step 2 in the queue). | ✅ COMPLETE (research) | `f2ae471` | — |
| DISCOVERED 2026-04-15 | leverage_mode feature + interactive TUI in deep_backtest (task #114) | Implemented 5 of 6 leverage modes from task #113 research as `LeverageMode` enum in `src/backtest/deep_backtest.py`. `_apply_leverage_mode()` transforms `strategy_params["max_risk_per_trade"]` BEFORE strategy instantiation — Option A hook point means zero changes to `LeveragedBacktestEngine` or any strategy class. RISK_SCALED: `risk_pct = base × (L/baseline)` (end-to-end validated: donchian_gold L=10→+21.62%, L=20→+45.87%, 2.12× linear). KELLY_FRACTIONAL: `f* = (bp-q)/b` with hard-cap at 0.25. Mode-aware Phase 2 sanity checks (linearity scoring for RISK_SCALED, invariance expectation for KELLY_FRACTIONAL) + mode-aware Phase 5 verdict gates (return-biased for RISK_SCALED / growth-biased for KELLY_FRACTIONAL). Extended timeframes: **30m, 4h, 1d** via `_resample_ohlcv()` generic helper + extended `_load_timeframe()`. New windows: 2y (730d) + 4y (1460d, graceful "not available" handling via `_check_window_availability()`). **Interactive TUI** via `src/backtest/deep_backtest_interactive.py` (NEW, questionary-based multi-select tick boxes for all dimensions, Prince-requested UX). Multi-mode runs write to separate report dirs. Tests: +15 new (6 LeverageModes + 4 ExtendedTimeframes + 4 WindowAvailability + 5 InteractiveTUI). DRAWDOWN_BUDGETED deferred (needs per-bar state). | ✅ COMPLETE | `b1b2ee2` | — |
| DISCOVERED 2026-04-15 | Zero-tolerance leverage_mode validation — Phase 2.5 (task #115) | Follow-up to task #114: Prince asked whether the pipeline guarantees 100% accuracy for RISK_SCALED / KELLY_FRACTIONAL runs. Closed the gaps with (1) engine-level `open_rejected_count` field in `LeveragedBacktestResult` tracking margin-gate rejections, (2) `CellResult.rejected_positions` + Phase 2 `margin_rejection_warnings` for cells > 30% rejection rate, (3) Phase 0 read-back probe that raises `RuntimeError` when a strategy's `__init__` silently overrides `max_risk_per_trade`, (4) new `_phase_2_5_leverage_mode_validation` with `_validate_risk_scaled` (9 hard-fail assertions: trade count / side / entry price / quantity scaling ±15% / P&L scaling ±15% / aggregate P&L ±10% / read-back / margin ratio / **commission scaling** — Prince caught this one) and `_validate_kelly_fractional` (6 hard-fail assertions + 2 warnings for hard-cap clamp and unrealistic priors), (5) Phase 5 verdict override forcing `verdict = FAILED` on any Phase 2.5 failure, (6) HTML report section for Phase 2.5 with pass/fail banner + assertion table. Option B (engine-level signal-emission intercept) deferred to task #116 — Option A + read-back probe + Phase 2.5 already make silent failures IMPOSSIBLE (they fail LOUDLY at preflight or Phase 2.5). Tests: +9 new (`TestLeverageModeValidation` — passthrough skip, risk_scaled pass, readback probe catches override, kelly pass, kelly hard-cap warn, kelly unrealistic warn, kelly math unit, rejected_positions field, rejection warnings). Full suite: 44 deep_backtest tests passing + 9 new. End-to-end smoke: `deep_backtest donchian_gold --leverage-mode risk_scaled` shows "Phase 2.5 — RISK_SCALED validation ✓ PASS" inline in pipeline output. | ✅ COMPLETE | `<pending>` | — |
| G.2h.5b | M5 donchian retune (scaled periods) | ❌ **GATE FAIL** | `<pending>` | — | 32 configs across (240,660,1440), (120,330,720), (60,165,360), (200,400,800) × sl × adx × risk. ALL configs losing. Best: -21.58% / 32.68% DD / Calmar -0.350. Decision: M5 donchian is not profitable at any tested scaling. **Keep 1h only.** M5 data retained for Tier 5 use. |
| G.2h.3 | Tier 5 infrastructure validation | ✅ COMPLETE | `<pending>` | — | Each of 3 aggressive strategies ran end-to-end through leveraged engine with experimental entry signals (EMA-trend filter / post-news scan / prior-N breakout). ALL 3 confirmed NOT ALPHA-READY: candle_burst -100% / 3451 trades, news_fade -99% / 145, hedged_structure -123% / 92. Infrastructure is correct; alpha is missing. Baselines written to `data/tier5_baseline.json`. Full research deferred to task #80. |
| G.2h.0 | G.3 preflight smoke test | ✅ COMPLETE | `<pending>` | — | `scripts/g3_preflight.py` runs shadow orchestrator + donchian_gold + vol_momentum_gold + LeverageBudgetAllocator end-to-end. Verifies: ≥1 trade, state parquet with expected columns, allocator grants fit the cap. **Combined two-strategy institutional book: $10k → $16,522.77 (+65.23%) / 188 trades / 0 stop-outs over 2yr XAUUSD 1h.** PASSED — G.3 Day 1 is safe. |
| **G.3** | **30-day paper clock — IC Markets cTrader demo, 5-step ramp** | ❌ NOT STARTED | — | — | Needs Phase 4 (IC Markets connection) un-deferred + tuning pass |
| G.3 Day 1-7 | Shadow mode, L=1 institutional only, aggressive disabled | ❌ NOT STARTED | — | — | — |
| G.3 Day 8-14 | Institutional L=5, aggressive 1% sizing | ❌ NOT STARTED | — | — | — |
| G.3 Day 15-21 | Institutional L=10, aggressive 2% sizing | ❌ NOT STARTED | — | — | — |
| G.3 Day 22-28 | Institutional L=25, aggressive 3% sizing | ❌ NOT STARTED | — | — | — |
| G.3 Day 29-30 | Institutional full cap 80×, aggressive full 5% | ❌ NOT STARTED | — | — | Gate to live: 0 forced liquidations, inst DD<5%, aggr DD<30%, 100+ inst trades, 50+ aggr trades |
| Live ramp | Real-money gold on IC Markets, weeks 5+ | ❌ NOT STARTED | — | — | Week 1-4 @ 10× max, Week 5-12 @ 20-30×, Month 4+ @ 40-60× on best tier |
| **G.4** | Dynamic budget allocator (floor + dynamic hybrid) | ❌ NOT STARTED | — | — | Post-live. 1 week of work once tier history exists |
| **G.5** | Tick reconstruction for scalper validation | ❌ **GATE FAIL** | `1560e56` | — | M1 ground-truth validation against 141,173 M5 bars with 5 M1 sub-bars each. Timing PASS (mean error 0.2424, threshold 0.25). **Ordering FAIL** (agreement 71.89%, threshold 80%). Bridge model's 28% hit-ordering error applies to tight-SL strategies. Superseded by G.5b. Backtest numbers for wide-SL strategies (donchian_gold, vol_momentum_gold) are unaffected. |
| **G.5b** | M1-based path model upgrade | ✅ COMPLETE | `<pending>` | 20 tests | `M1PathModel` in `src/backtest/path.py` uses 1-minute sub-bars as intrabar ground truth. Validated 100% ordering (vs 71.89% bridge) + 0.0000 mean timing error (vs 0.2427 bridge) on the same 2000-bar samples. Cached module-level factory `get_or_build_m1_path_model()` loads parquet once per process. Opt-in via `LeveragedBacktestEngine(path_model=...)`; default stays `BrownianBridgeModel` to preserve baselines. Unlocks tight-SL scalper strategies when the aggressive sub-book comes back (task #80 follow-up). |
| **Tier 5 alpha research** | Task #80 — dedicated research pass | ❌ **ALL 3 KILLED** | `1560e56` | — | `news_spike_fade`: premise confirmed (95.7% retracement rate on >=30 pip spikes) but bar-level execution CANNOT capture tick-level fade timing. `hedged_structure_play`: design mismatched to gold's sustained uptrend, 386 trades / 2yr is over-trading, all configs wipe. `candle_burst_hunter`: requires tick-level mid-bar velocity detection, cannot work on bar data. Obituaries: #92, #93, #94. |
| **Tier 5 M1 revival** | Task #100 — revisit candle_burst + news_spike_fade at M1 cadence | ❌ **BOTH FAILED** | `be4e2d2` | — | 2026-04-14 post-G.5b attempt. **candle_burst @ M1**: all 6 configs -100% over 350k M1 bars (1k-17k trades per config — ATR_20 on M1 too small, filter degenerates). **news_spike_fade @ M1** with `require_peak_reversal=True`: all 7 configs -21% to -47% (reversal trigger fires on normal M1 noise inside news windows, not genuine peak stalls). Kills made more definitive: M1 resolution alone does NOT rescue these strategies; they need fundamentally different signal architectures (multi-bar velocity, volume confirmation, or real tick-level). Aggressive sub-book stays empty. Obituaries amended in the strategy files. Results in `data/tier5_m1_revival.json`. |
| **Strategy Graveyard** | Task #101 — structured registry for killed strategies | ✅ COMPLETE | `69cc09b` | 20 tests | New `strategy_graveyard` SQLite table inside `data/trades.db` (reuses canonical DB, no new file). `src/strategies/graveyard.py` exposes `GraveyardEntry` dataclass + `record_kill()` / `append_revival_attempt()` / `list_graveyard()` / `get_graveyard_entry()`. `scripts/ingest_obituaries.py` parses the 3 killed aggressive strategies' obituary docstrings and populates the table (idempotent upsert on `(name, version)`). Query patterns: `list_graveyard(category='NOISE')`, `list_graveyard(tag='tick-blocked')`. Solves the "kill info scattered across docstrings / JSON / task records / ROADMAP / Known Gotchas" problem — graveyard is a cached projection of those sources. |
| **Scalping Architecture** | Task #102 — primitives to unblock next-gen scalpers | ✅ COMPLETE | `072e81c` | 54 tests | 6 new feature_engine indicators (`roc_N`, `velocity_N`, `body_pct`, `close_in_range`, `volume_zscore_N`, `dollar_volume_N`) + parser extended to handle multi-underscore names. `M1PathModel._m1` upgraded to store full OHLCV via `M1SubBar` named tuple. `LeveragedBacktestEngine` attaches `intrabar_sub_bars` to row when running with M1PathModel. New `ScalperStrategy` base class with rolling history buffer + velocity/volume/structural/intrabar helpers. Opt-in design — default engine behavior unchanged, existing strategies untouched. |
| **SWIFT Port + Research** | Task #103 — TradingView Pine Script port + backtest matrix + report | ✅ **RESEARCH DONE — GRAVEYARD** | `f831001` | 28 swift + 7 zero-cost = 35 tests | Ported TradingView's "SWIFTALGO" Pine Script (~700 lines → ~50 lines of real trade logic). Implementation includes **3-tier TP ladder via virtual leg accounting** (50/30/20 at 1/1.5/2%) + `ZeroCostFeeModel` for apples-to-apples TV comparison + `AlternateTimeframeBuilder` for Pine's `request.security(higherTF, ...)` pattern + `ALMA` as first-class feature engine indicator. Matrix: 5 TFs × 6 leverages × 2 modes = 60 cells. **Pine-faithful** (0 commission) best: 4h×5x **+52.40% / 4.90% DD / PF 1.60** — validates user's TV observation. **Realistic** (IC Markets Raw) best: 4h×1x **-12.47% / 14.88% DD** — cost drag destroys all gross edge. Verdict: **MIXED — gross positive, net negative**. Added to `strategy_graveyard` as `swift_alma v1 / PREMISE / cost-killed`. Report at `reports/swift_alma_gold_research.md`. Bar-by-bar TV validation reached **100.00%** match on 107-day Vantage export (1228/1228 entries) at commit `e529e56`. |
| **TV Parity Framework** | Task #104 — reusable Pine port validation | ✅ COMPLETE | `<pending>` | 20 tests | Generalized the one-off SWIFT validation into a reusable framework. Library `src/backtest/tv_parity.py` exports CSV loaders, DST-aware anchor detection, lookahead ALMA crossover helper, reversal simulator, bar-by-bar + trade-by-trade matchers, and Markdown report writer. CLI `scripts/tv_parity_validate.py` dynamically imports any `BaseStrategy` subclass that exposes a `detect_signals_lookahead` classmethod and reports pass/fail at ≥99% bar match threshold. Re-validated SwiftAlmaStrategy through the new framework → 100.00% match. Workflow documented in `docs/PINE_SCRIPT_PORT_WORKFLOW.md` — becomes Stage 0 of `STRATEGY_DEVELOPMENT_PROCESS.md` for any future Pine port. |
| **G.6** | Adaptive leverage governor ML model | ❌ NOT STARTED | — | — | Train on `leverage_grants` table once 500+ rows exist. Phase 5 tie-in |
| **G.7** | Alpha vs leverage attribution dashboard | ❌ NOT STARTED | — | — | Needs live trade history to be meaningful |

**Test counts:** 1126 passing on `feat/gold-refactor` as of task #104 TV parity framework (1106 + 20 tv_parity tests).

---

## Next Phase Detail — 3b Part 2 (M3S)

M3S = **Master Money Management System**. The single highest-value component in the roadmap — no retail system has this, hedge funds use human teams, we're building it as a 10-sub-phase institutional-grade + retail-aggressive system.

**Authoritative plan:** `docs/planning/m3s_plan_v1.md` § v1.1 ADDENDUM (Session 22).

**Modes (4 total):** CONSERVATIVE / STANDARD / GROWTH / CUSTOM. GROWTH is retail-aggressive (vol 22%, daily compound, DD 10% demote). CUSTOM is fully user-configurable with 5 safety rails.

**7 Tier 1 additions** layered onto the base allocator+compounder+modes stack:
1. Regime detection → auto mode switching (sub-phase 0.9)
2. Strategy edge-decay detection (sub-phase 0.2)
3. Signal conviction-weighted sizing (sub-phase 0.5)
4. Ledoit-Wolf covariance shrinkage (sub-phase 0.4)
5. CVaR tail-risk position scaling (sub-phase 0.3)
6. Purged & Embargoed K-Fold CV (sub-phase 0.10)
7. Bayesian fractional Kelly from parameter uncertainty (sub-phase 0.10)

**Deferred to Phase 3c:** Meta-labeling (Lopez de Prado AFML ch.3) — biggest Sharpe upside but its own 2-week sprint with LightGBM training pipeline. Gated on sub-phase 0.10.

### Sub-phase Table

| # | Name | Status | Tests | Backtest gate |
|---|---|---|---|---|
| 0.1 | Skeleton + state layer (types, state, modes) | ✅ COMPLETE | 27 | — |
| 0.2 | Portfolio tracker + edge-decay monitor (+Tier 1 #2) | ✅ COMPLETE | 33 | — |
| 0.3 | Compounder + 4 modes + CVaR scaling (+Tier 1 #5) | ✅ COMPLETE | 35 | BT #1 ✅ |
| 0.4 | Allocator HRP-lite + Ledoit-Wolf shrinkage (+Tier 1 #4) | ✅ COMPLETE | 24 | BT #2 ✅ |
| 0.5 | Hooks + conviction scorer (+Tier 1 #3) | ✅ COMPLETE | 33 | — |
| 0.6 | Scheduler + SQLite persistence + migrations | ✅ COMPLETE | 12 | — |
| 0.7 | Meta-backtest simulator | ✅ COMPLETE | 11 | BT #3 ✅ |
| 0.8 | Wire into main.py DISABLED | ✅ COMPLETE | 8 | — |
| 0.9 | Regime detector + auto mode switching (+Tier 1 #1) | ✅ COMPLETE | 21 | BT #4 ✅ |
| 0.10 | Purged CV + Bayesian fractional Kelly (+Tier 1 #6,#7) | ✅ COMPLETE | 23 | BT #5 ✅ |

**Totals actual:** ~3,400 LOC, 227 new M3S tests, 5 backtest gates all green, 516 total tests (from 289 baseline).

**Why this ordering:** 0.1 is pure scaffolding (no wiring). 0.2-0.4 build the three core engines (portfolio/compounder/allocator) in parallel-compatible isolation. 0.5 wires them behind a single `M3S.on_signal` hook. 0.6 makes it restartable. 0.7 proves it beats fixed-weight before 0.8 wires it (disabled) into main.py. 0.9 and 0.10 are the Tier 1 additions that don't fit inside existing sub-phases.

**Why this is in progress (and not IC Markets or the 30-day validation run):**
- IC Markets cTrader is **only suitable for gold** — there is no gold strategy yet, so Phase 4 is deferred
- The 30-day paper validation is a passive clock — it runs in parallel, it doesn't block development
- M3S is the last component needed before the system can genuinely compound profits across multiple strategies with correlation-awareness. Everything downstream (AI agents, options, production scale) assumes M3S exists.

---

## Recently Completed — Phase 3b Part 1 (Adaptive Risk Modes, Session 13)

- 4 operating modes (AGGRESSIVE/BALANCED/DEFENSIVE/CUSTOM) via multipliers on base RiskConfig
- Per-strategy risk profiles override mode defaults (donchian_ensemble_adx, vol_momentum)
- EV-gate confidence check for trend strategies (fixes donchian at conf 0.667 passing)
- `skip_kelly_vol_scaling` flag for self-regulating strategies (vol_momentum)
- Risk-based position limits (instead of notional) for wide-stop strategies
- 26 new tests (91 total), mode backtest verification: AGGRESSIVE ≈ NONE (0 rejections)

**Non-negotiable (never scaled by mode):** max_drawdown (15%), max_monthly_loss (10%), kill_switch, duplicate_cooldown_s.

---

## Completed Milestone Evidence

| Phase | Milestone met | Sessions |
|---|---|---|
| 1 — Data Foundation | Binance WS → CandleBuilder → FeatureEngine → Storage operational | 1-5 |
| 2 — Strategy + Backtest | BB+RSI 5-symbol portfolio Sharpe 1.307 (>1.0 target) | 9 |
| 2 — Optimization | Optimized portfolio Sharpe 2.318 (40/30/30, bb_rsi_mr_opt + donchian_adx + vol_momentum) | 11 |
| 3a — Risk Manager | 7-check chain, ZMQ isolation, kill switch, 3-level circuit breakers | 12 |
| 3b-1 — Adaptive Modes | 4 modes + per-strategy profiles + backtest verification | 13 |
| — Paper trading go-live | launchd 3-service deployment (risk server + engine + watchdog) | 16 |
| — Correctness sweep | 289 tests pass, TradingView bit-exact, parity live↔backtest pinned | 20 |

---

## Phase Dependency Graph

```
1 → 2 → 3a → 3b-1 → 3b-2 (M3S, NEXT) → 3-milestone → 4 → 5 → 6 → 7 → 8
                                ↘
                                 (M3S unlocks: AI agents, options sizing, production scale)
```

**Parallel tracks:**
- Phase 3-milestone (30-day paper clock) runs *passively* alongside 3b-2 and beyond
- Phase 8 (scale) is partially met (3 strategies live in backtest); full completion requires OOS live validation after M3S is wired in
