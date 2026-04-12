# Algo Trading — Roadmap & Phase Status

**Last updated:** 2026-04-13 (Session 21 — doc consolidation)
**Current phase:** 3b part 2 — M3S construction (AI compounding system)
**Next action:** Build `src/m3s/` — start with Allocation Engine skeleton + mode resolver hook into existing RiskManager. See `docs/M3S_SPEC.md` for full spec.

> **Authority note:** This file is the **only** authoritative source for phase status. If any other file contradicts this, that other file is wrong — fix it to link here. See `CLAUDE.md` § Autonomous Workflow Protocol.

---

## Phase Table

| # | Phase | Milestone | Status | Evidence | Blockers |
|---|---|---|---|---|---|
| 1 | Data Foundation | WS feed → candle → indicators → storage working | ✅ COMPLETE | Sessions 1-5 | — |
| 2 | Strategy + Backtest + Paper | Sharpe >1.0 on 2-year walk-forward | ✅ MILESTONE MET | Session 9 (1.307 baseline), Session 11 (optimized 2.318) | — |
| 3a | Risk Manager — core | 6 pre-trade checks + circuit breakers + kill switch + ZMQ isolation | ✅ COMPLETE | Session 12 | — |
| 3b-1 | Adaptive Risk Modes | 4 modes (AGGRESSIVE/BALANCED/DEFENSIVE/CUSTOM) + per-strategy profiles + mode backtest verification | ✅ COMPLETE | Session 13 | — |
| **3b-2** | **M3S — AI Compounding System** | **Allocation Engine + Compounding Engine + AI Advisor (The PhD) + Correlation limits + Telegram bot** | ❌ **NOT STARTED — NEXT PHASE** | See `docs/M3S_SPEC.md` | — |
| 3-milestone | Paper run — no losses beyond limits for 30 days | 30 consecutive days clean operation | 🟡 IN PROGRESS (passive clock) | Engine launched Session 16; 289 tests pass Session 20 | Operational stability + M3S integration |
| 4 | Multi-Exchange Execution | Paper trades on 2+ exchanges, <200ms latency | ❌ NOT STARTED | — | IC Markets cTrader is gold-only and **deferred** until a gold strategy exists |
| 5 | AI Agent Intelligence (Meta-Strategist, News, Risk Sentinel) | Sharpe uplift >0.2 vs baseline | ❌ NOT STARTED | — | M3S must land first (The PhD is the foundation) |
| 6 | Options Module | Iron condor SPX positive 1-year | ❌ NOT STARTED | — | — |
| 7 | Production Deployment | 7 days unattended on VPS | ❌ NOT STARTED | — | Phase 3-milestone + M3S |
| 8 | Scale | 3+ strategies portfolio Sharpe >1.5 | 🟡 PARTIAL — 3 strategies live, Sharpe 2.318 in backtest only | Session 10-11, 14-16 | OOS portfolio validation on live paper data |

**Legend:** ✅ complete · ❌ not started · 🟡 in progress/partial

---

## Next Phase Detail — 3b Part 2 (M3S)

M3S = **Master Money Management System**. The single highest-value component in the roadmap — no retail system has this, hedge funds use human teams, we're building it as an AI-powered engine.

**Scope** (all 5 sub-components):
1. **Allocation Engine** — Kelly-weighted, correlation-aware, per-mode capped
2. **Compounding Engine** — BASE/PROFIT pool tracking, per-mode compound rules
3. **AI Advisor "The PhD"** — Claude Haiku routine (every 30 min) + Sonnet daily review. Outputs structured JSON (mode blend + allocations + compound pct)
4. **Correlation Limits** — Pairwise corr > 0.7 → combined allocation capped at 40%
5. **Telegram Bot** — Alerts + mode override commands

**Expected impact:** Sharpe 1.6-2.2 (baseline M3S), 1.8-2.5 (with AI compounding). See `docs/M3S_SPEC.md` for full spec including mode definitions, 4-step allocation math, compounding rules, advisor prompt structure, and expected-return table.

**Why this is next (and not IC Markets or the 30-day validation run):**
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
