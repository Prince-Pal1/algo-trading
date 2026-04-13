# Algo Trading — Roadmap & Phase Status

**Last updated:** 2026-04-13 (Session 22 — Phase 3b-3 strategy expansion: A shipped, B killed at B.3)
**Current phase:** 3b part 3 — strategy expansion COMPLETE. Strategy A (funding_carry) shipped 5/5 sub-phases with Sharpe 2.584. Strategy B (clenow_momentum) killed at B.3 with avg Sharpe -0.124; obituary filed. M3S shadow clock continues running in parallel.
**Next action:** Monitor M3S shadow report (`cat data/m3s_shadow_report.md`). When 7/7 days hit, flip `shadow_mode=false` for authoritative mode. Optionally: download historical BTC funding-rate Parquet via `BinanceFundingDownloader` and flip `[funding_carry] enabled = true` in `config/strategies.toml` for paper trading.

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

**Legend:** ✅ complete · ❌ not started · 🟡 in progress/partial

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
