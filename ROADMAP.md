# Algo Trading — Roadmap & Phase Status

**Last updated:** 2026-04-14 (Gold Phase G.2 COMPLETE on feat/gold-refactor worktree; ready for merge to main 2026-04-17)
**Current phase:** Phase G (Gold Leveraged Stack) on branch `feat/gold-refactor` @ `/Users/prince/algo-trading-gold`. All G.2 sub-phases shipped: G.2a (Book), G.2a.2 (costs), G.2a.3 (path), G.2a.4 (engine), G.2b (M3S request_leverage + geometric-mean blend + leverage_grants store), G.2c (inline gates + RCU portfolio view + AGGRESSIVE_RETAIL profile), G.2d (donchian_gold), G.2e (Tier 5 infra), G.2f (3 aggressive strategies), G.2g (split sweep + run_multi engine path). 920 tests passing. Main branch (3b-2 M3S shadow + 3c meta-labeling shadow) is untouched and still running the 4-day clock toward the 2026-04-16 cron promotion.
**Next action:** Wait for Day 4 sprint cron to complete 2026-04-17 ~09:30, then rebase feat/gold-refactor on main, run the full test suite, merge. Post-merge: parameter tuning for donchian_gold + M5 data acquisition for Tier 5 strategies (they run on 1h in the sweep and the aggressive sub-book wipes to -100% because M5-designed logic like candle_burst_hunter over-triggers on 1h bars). The Calmar-optimal split from the sweep (institutional_pct=0.95) is the conservative default until that tuning pass.

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
| **G** | **Gold Leveraged Stack** | Two-book (institutional + aggressive) engine + donchian_gold + 3 Tier 5 strategies + split sweep | ✅ **G.2 COMPLETE on `feat/gold-refactor`** (pending merge) | G.0-G.2g shipped (commits 54c0b62 → G.2g) | Merge to main 2026-04-17 ~09:30 after Day 4 sprint cron |

**Legend:** ✅ complete · ❌ not started · 🟡 in progress/partial

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
| G.2h.6 | Walk-forward donchian_gold retune | ✅ COMPLETE | `a7f0f11` | — | 6 folds (3mo train × 1mo test). Mean OOS Calmar +8.515 ± 9.5, Sharpe +2.001 ± 2.5. Gate PASSED (> 0.3). High variance: 4/6 folds profitable, fold 4 was -4.0 Calmar. 30 total OOS trades. |
| G.2h.6b | Walk-forward vol_momentum_gold retune | ✅ COMPLETE | `<pending>` | — | 6 folds, same schedule. Mean OOS Calmar +5.582 ± 9.4, Sharpe +0.912 ± 3.2. Gate PASSED. 4/6 folds profitable (losers: fold 2 -5.64%, fold 6 -1.17%). 47 total OOS trades. **Losing folds don't overlap with donchian_gold's losing fold** — real diversification. |
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
| **G.5** | Tick reconstruction for scalper validation | ❌ **GATE FAIL** | `<pending>` | — | M1 ground-truth validation against 141,173 M5 bars with 5 M1 sub-bars each. Timing PASS (mean error 0.2424, threshold 0.25). **Ordering FAIL** (agreement 71.89%, threshold 80%). Bridge model's 28% hit-ordering error applies to tight-SL strategies. Upgrade deferred to task #91 (M1-based path model). Backtest numbers for wide-SL strategies (donchian_gold, vol_momentum_gold) are unaffected. |
| **Tier 5 alpha research** | Task #80 — dedicated research pass | ❌ **ALL 3 KILLED** | `<pending>` | — | `news_spike_fade`: premise confirmed (95.7% retracement rate on >=30 pip spikes) but bar-level execution CANNOT capture tick-level fade timing. `hedged_structure_play`: design mismatched to gold's sustained uptrend, 386 trades / 2yr is over-trading, all configs wipe. `candle_burst_hunter`: requires tick-level mid-bar velocity detection, cannot work on bar data. Obituaries: #92, #93, #94. Aggressive sub-book remains empty until tick infrastructure lands. |
| **G.6** | Adaptive leverage governor ML model | ❌ NOT STARTED | — | — | Train on `leverage_grants` table once 500+ rows exist. Phase 5 tie-in |
| **G.7** | Alpha vs leverage attribution dashboard | ❌ NOT STARTED | — | — | Needs live trade history to be meaningful |

**Test counts:** 905 passing as of commit `19592de` (734 baseline + G.0/G.0c/G.1/G.2a-G.2f additions).

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
