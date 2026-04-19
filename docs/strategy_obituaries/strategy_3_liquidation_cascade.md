# 🪦 liquidation_cascade_reversion — Stage 3 kill

**Killed:** 2026-04-19 (Session 23 Day 2, Strategy 3 of the 3-strategy diversification cycle)
**Category:** NOISE — the edge exists in isolation but is consumed by realistic transaction costs
**Sibling:** swift_alma_v2 obituary (same family — cost drag eats the edge, but via slippage here rather than commission)

## The claim

Fade forced-liquidation cascades on BTCUSDT. CFTC 2022 + Coinbase Institutional 2024 research both report that liquidation cascades produce 3-7% overshoots with 60% retracement within 30 min. Expected Sharpe 1.5-2.5 with episodic P&L distribution; functions as a drawdown hedge on the rest of the portfolio because it fires during chaos.

## What the Stage 1 research showed

`scripts/research_liquidation_cascade.py` on 6.5mo of BTC 1m data (288k bars, 2025-10 → 2026-04) with price-based cascade proxy (z-score > 4σ AND |ret_1m| > 50 bps):

- **71 cascade events detected** (10.7/month — good statistical power)
- **66.2% retraced ≥ 50% within 30 min** ✅
- **100.0% fully retraced within 30 min** (eventually)
- **Per-trade Sharpe +0.138** with best-found SL=-200/TP=+100/30m config
- **Mean PnL +14 bps gross / trade**, +97 bps gross over 71 trades = ~+10% over 6.5mo

All 3 Stage 1 gates passed 3/3. Commit `2ff0fda` shipped this as the Stage 1 result.

## What Stage 3 showed (the kill)

Ran the actual strategy through the production `scripts.backtest run` engine on the same 6.5mo window:

- **60 cascade trades** (close to the 71 from research; detection logic is consistent)
- **Final equity −3.84% to −5.87% across parameter variations**
- **Sharpe −1.37 to −2.31 across an 18-cell grid** (cascade_sigma ∈ {4, 5, 6} × tp ∈ {100, 150, 200} bps × max_hold ∈ {30, 60} min)
- **Max drawdown 5-8%** on a $10k account

18 cells tested, **zero profitable**. The spread between best (sigma=6, tp=200, hold=60: -3.84%, Sharpe -1.37) and worst (sigma=4, tp=100, hold=30: -5.27%, Sharpe -2.31) is noise.

## Root cause — cost assumptions were wrong

Stage 1 research modeled commission (0.08% RT = 8 bps) but **missed two other real-world costs**:

1. **Slippage 0.02%/side = 0.04% RT = 4 bps** (default in `src/backtest/engine.py::BacktestConfig.slippage_pct`). Production orders suffer fill-price degradation; the research simulation assumed fills exactly at the cascade bar's close.

2. **Next-bar-open execution = ~2-3 bps drag on fast recoveries**. The backtest engine emits entry signals at bar N close but fills at bar N+1 open (line 251 of `engine.py`). During cascade RECOVERY (which is exactly when we want to enter LONG), the bar after the cascade often opens 10-30 bps higher than the cascade close — which the research simulation captured as "edge" but which the engine gives back at entry.

| Cost | Stage 1 research | Production engine | Delta |
|---|---:|---:|---:|
| Commission (Binance perp taker) | 0.08% RT | 0.08% RT | 0 |
| Slippage | 0% | 0.04% RT | −4 bps |
| Entry fill lag | 0 (next-bar-open == close) | Realistic drag on cascade recovery | −2 to −5 bps |
| **Total round-trip cost** | **8 bps** | **~14-17 bps** | **−6 to −9 bps** |

Stage 1 gross edge was +14 bps per trade. Real-world cost is ~14-17 bps per trade. **Net edge = 0 to negative**. Every grid cell confirms this pattern.

## The lesson — for future research

**Always bake slippage into Stage 1 research, not just commission.** The IC Markets fee system we use for gold strategies defaults to `atr_vol_mult=0.0` + `normal_slip_pips=0.3` in its SpreadSlippageConfig — slippage is a first-class cost there. The Stage 1 research script for Funding MR (`scripts/validate_funding_mr.py`) also skipped slippage, but that strategy has per-trade expectancy so much higher than cost that the gap didn't kill it.

For high-frequency event-driven strategies with thin edges (like cascade reversion), **slippage + execution lag are make-or-break**. They should be modeled in Stage 1 or the research script will produce false positives — as happened here.

**Action item for STRATEGY_DEVELOPMENT_PROCESS.md:** update Stage 1 gate definition to require total-cost-inclusive Sharpe, not just commission-inclusive. Add a "slippage + next-bar-open drag check" to the Stage 1 template. Prevents the specific failure mode that killed this strategy from recurring.

## Revival conditions

1. **MEV/latency-aware execution** — if we build a "fill at bar close" execution mode (treating cascades as aggressively as a HFT fund would), the +14 bp gross edge might survive. Requires: intrabar-fill backtest engine mode + co-located live execution infrastructure. Not on our roadmap.
2. **Tighter entry filter** combined with wider exit — only enter on cascades where both (a) z-score > 8σ AND (b) there are matching liquidation events on the `!forceOrder@arr` stream within the same minute. Binary "cascade confirmed by liquidation feed" reduces noise trades. But we'd need live-event history matched with historical price bars to backtest this.
3. **Different underlying** — cascade retracement may be stronger on less liquid symbols (ETHUSDT, SOLUSDT) where each liquidation creates bigger impact and bigger retracement. Not tested. Would need Stage 1 research on multiple symbols to find one where the edge survives 14+ bps of cost drag.
4. **Shorter hold + tighter bounds** — instead of 30-60 min with ±100-200 bp brackets, trade 2-5 minutes with ±30 bp brackets. Requires the intrabar-fill execution mode; bar-close-only won't work at that timescale.

## Do NOT re-attempt without

- Fixing the cost model FIRST (include slippage + execution lag) + re-running Stage 1 with the stricter setup.
- If Stage 1 still shows Sharpe > 0.3 under realistic costs: proceed to Stage 2+ as normal.
- Otherwise don't waste cycles on Stage 2 build.

## Files (kept for reuse)

- Strategy code: `src/strategies/event_driven/liquidation_cascade.py` — keep. The class is well-factored and the deque-based rolling std pattern is reusable for any future event-driven strategy. Also: if revival happens with different exits, the class's entry logic is still correct.
- Stage 0 feed: `src/data/feeds/binance_liquidation_feed.py` — keep. Live-validated. Useful standalone for any future event-driven work.
- Research script: `scripts/research_liquidation_cascade.py` — keep but ADD a note at the top flagging the slippage omission.
- Unit tests: `tests/test_strategies/test_liquidation_cascade.py` — keep, they still pass (6/6). They validate correctness; the strategy works, it just isn't profitable net-of-cost.
- Config section: `config/strategies.toml [liquidation_cascade]` — leave `enabled = false`. Do not delete; it's documentation of the attempt.
- Router + BACKTEST_PRESETS registration: keep. No cost to leaving them in; registered strategies that aren't enabled don't run.
- Stage 0 `LiquidationEvent` type: keep in `src/utils/types.py`.

## Session 23 Day 2 final scorecard

After 8+ hours of focused work across 4 strategy candidates:

| Strategy | Stage reached | Verdict |
|---|---|---|
| 1. funding_mean_reversion | 7 stages + 5.5 regime filter | ✅ **SHIPPED to shadow** (Sharpe 0.60, Calmar 6.36) |
| 2. stat_arb_eth_sol + BTC/LTC + ETH/BNB | 1 | ❌ KILLED (cointegration broken on all 3 pairs) |
| 2'. asian_range_breakout | 1 | ❌ KILLED (no edge in gold's trend regime) |
| 3. liquidation_cascade_reversion | 0 + 1 (passed) + 2 (class + tests) + 3 (killed) | ❌ KILLED (Stage 1 missed slippage/execution drag) |

**1/4 shipped.** Worse than the planned 50% mortality rate — but the 3 kills each produced documented revival conditions + at least one reusable component (research script / stage-1 cointegration test / liquidation feed / strategy class template). The process worked; reality was harder than the plan prior.

**Process improvement captured** in the "lesson" section above for the STRATEGY_DEVELOPMENT_PROCESS.md living document.
