> ⚠️ **ARCHIVED — SUPERSEDED (Session 21)**
>
> This is the original approved plan from April 11, 2026. It has been superseded by the new single-source-of-truth doc system:
> - Current phase status → [ROADMAP.md](../../ROADMAP.md)
> - M3S spec → [docs/M3S_SPEC.md](../M3S_SPEC.md)
> - Decision rationale → [MASTER_PLAN.md](../../MASTER_PLAN.md)
> - Live module registry → [ARCHITECTURE.md](../../ARCHITECTURE.md)
>
> Content preserved verbatim below for historical reference. Do not edit.

---

# Algo Trading System — Full Approved Plan

**Status:** APPROVED & PARTIALLY IMPLEMENTED | **Date:** April 11, 2026
**Author:** Prince | **AI Architect:** Claude

> **Implementation Note (April 12, 2026):** This plan was the approved build order. Phase 1 (Data Foundation) and Phase 2 (Strategy + Backtest) are complete. Key deviations: custom backtest engine instead of VectorBT, `ta` library instead of pandas-ta, BB+RSI Mean Reversion as validated strategy (EMA Crossover deprecated). Session 8 added institutional-grade backtesting (7 protocols, 14 charts, 6 format parsers, Streamlit dashboard). See `STATE.md` for current status.

---

## Context

Prince has a 1,399-line blueprint at `/Users/prince/algo-trading/TRADING_SYSTEM_BLUEPRINT.md` covering a full production-grade algo trading system with 9 phases, 3 strategy modules, options trading, multi-agent AI, and multi-exchange execution. This plan is the result of deep analysis — what's good, what's wrong, what needs correction, and how to actually pull this off.

---

## RESEARCH FINDINGS

### Q1: Performance — Is this system best-in-class?

**Speed comparison:**

| System | Signal-to-Order | Order-to-Fill | Total Roundtrip |
|---|---|---|---|
| HFT firms (Citadel, Jump) | <1 microsecond | <5 microseconds | <10 microseconds |
| Commercial bots (3Commas, Cryptohopper) | 500ms-2s | 1-5s | 2-10s |
| **Our system (blueprint target)** | **<50ms** | **<100ms** | **<200ms** |
| Average Pine Script on TradingView | 1-15s (webhook delay) | 1-5s | 3-20s |
| Average retail Python bot | 200ms-1s | 200ms-1s | 500ms-2s |

**Our system is 10-100x faster than any commercial bot or Pine Script**, because:
- Direct WebSocket connection (no webhook relay, no TradingView middleman)
- asyncio + uvloop event loop (2-4x faster than vanilla Python)
- orjson parsing (10x faster than stdlib JSON)
- Pre-computed indicators in Redis cache (no recomputation per tick)

**But 1000x slower than HFT** — which doesn't matter at our timeframes (seconds to minutes).

**Returns comparison:**

| System | Typical Annual Return | Sharpe Ratio |
|---|---|---|
| Commercial bots (3Commas, etc.) | 12-25% | 0.5-1.0 |
| Good custom Python bot | 20-50% | 1.0-2.0 |
| Professional hedge fund custom bot | 30-60%+ | 1.5-3.0 |
| **Our blueprint target** | **36-96%** (3-8%/mo) | **>1.5** |
| Pine Script strategies (typical) | 10-30% | Rarely measured |

**Verdict: Yes, this is better than any commercial bot or Pine Script available to retail.** The architecture is institutional-grade. The bottleneck is NOT the system — it's strategy quality and risk discipline.

---

### Q2: Strategy Quality — Is EMA/RSI Enough?

**Honest answer: No. EMA/RSI alone is NOT enough for high returns.**

EMA crossover + RSI is a *starter strategy* — a learning exercise to test the system plumbing.

**What actually makes money in 2026 (researched):**

**Tier 1 — Proven High-Return Strategies:**

| Strategy | Expected Edge | Complexity | Why It Works |
|---|---|---|---|
| **Smart Money Concepts (ICT/SMC)** | High — 60-70% win rate reported | Medium | Tracks institutional order flow, liquidity sweeps, order blocks, FVGs. A Python bot for XAUUSD achieved 70% win rate and 5,381% gross return in backtest. Python package exists: `smart-money-concepts` on GitHub |
| **Statistical Arbitrage (Pairs)** | Medium-High — market-neutral | High | Exploits mean-reversion between correlated assets. Hedge fund staple. Works in all conditions. |
| **CVD Divergence** | High for crypto | Medium | Price vs volume divergence = reversal signal. One of the most reliable order flow signals. |
| **Multi-Indicator Fusion** | Better than single — 60.63% win rate proven | Medium | RSI + EMA + VWAP + MACD combined outperforms any single indicator. Research-backed on NSE. |
| **Funding Rate Arbitrage** | Low risk, steady | Low | Long spot + short perp when funding is extreme. Market-neutral income. |
| **Volatility Selling (Options)** | Consistent income | High | Systematic iron condors/strangles. Premium collection. Tastytrade-style. |

**Tier 2 — Advanced (add later):**
- Market structure breaks + supply/demand zone trading
- Volume profile + naked POC strategies
- ML-enhanced signal classification (gradient boosted trees)
- Macro regime detection via LLM (Claude agent)

**Strategy import system — Can we import Pine Scripts and trading bots?**

YES, and this should be a core feature:

1. **Pine Script to Python conversion pipeline:**
   - Use Claude (our AI layer) to analyze any Pine Script strategy
   - Convert logic to Python that fits our `BaseStrategy` interface
   - Tools exist: PyneSys (auto-converter), pyine (PyPI package), or Claude can do manual conversion
   - TradingView MCP can read Pine Script source via `pine_get_source`

2. **Strategy plugin architecture:**
   - Every strategy implements `BaseStrategy.on_candle() -> Signal | None`
   - Hot-swap strategies via config file (no code restart)
   - A/B test strategies in parallel on same data

3. **"Strategy Ingestion" flow:**
   ```
   Input: Pine Script / GitHub bot / strategy description
     -> Claude agent analyzes the logic
     -> Converts to Python BaseStrategy
     -> Auto-backtests on our historical data
     -> Reports: Sharpe, drawdown, win rate
     -> If passes thresholds -> add to paper trading
   ```

**Correction:** Replace "EMA crossover" as the primary strategy with **Smart Money Concepts (ICT) + Multi-Indicator Fusion** as Phase 2 strategies. Keep EMA crossover as the "hello world" test, but move to SMC/ICT quickly.

---

### Q3: Execution — Better Brokers with Less Restrictions/Tax/Fees?

**The Broker Ranking for Our System:**

| Broker | Markets | Spreads | Algo API | Tax Situation (India) | Best For |
|---|---|---|---|---|---|
| **IC Markets** (RECOMMENDED) | Forex, CFDs, Crypto, Metals | 0.0 pips Raw (from $3.50/lot commission) | cTrader + Python native, MT5, Open API | Offshore — forex profits = "business income" (slab rate, NOT 30% flat) | Forex/Gold scalping, lowest cost |
| **XM** | Forex, CFDs, Metals | 0.0 pips Zero account | MT4/MT5 + MQL5 EAs | Same offshore treatment as IC Markets | Good if using MQL5, wide instrument range |
| **Alpaca** (already have) | US Stocks, ETFs | Commission-free | REST + WS, MCP built | US equity gains: different tax treaty rules | US equity day trading |
| **Binance** | Crypto spot + futures | 0.02/0.04% maker/taker | REST + WS, best crypto API | 30% flat + 1% TDS (worst tax) | Crypto only, accept the tax or avoid high-frequency |
| **IBKR** | Everything globally | Varies | TWS API, FIX protocol | Depends on instrument + entity structure | Options, futures, multi-asset |

**Key insight: Forex/Gold via IC Markets has MUCH better tax treatment than crypto in India.**
- Crypto: 30% flat tax on every profit + 1% TDS = brutal for scalping
- Forex (offshore): Classified as "business income" = taxed at your income slab rate (likely 5-20% for most)
- This means the SAME scalping strategy on Gold (XAUUSD) via IC Markets is **10-25% more profitable after tax** than the same strategy on BTCUSDT via Binance

**IC Markets details (why it's the top pick):**
- **cTrader with native Python support** — write cBots in Python directly, no MQL5 needed
- **cTrader Open API** — full REST + WebSocket, connect from your own Python code
- **Raw spread from 0.0 pips** — $3.50/lot commission (among the lowest globally)
- **Execution**: 99.6% of orders filled in <40ms
- **No restrictions on algo trading, scalping, hedging**
- **Available to Indian residents** via offshore entity (not SEBI regulated)
- **$200 minimum deposit**
- Supports: 60+ forex pairs, Gold, Silver, Oil, Indices, Crypto CFDs

**Upgrade path:**
```
Phase 1-3:  IC Markets (forex/gold) + Alpaca (US equities) -- paper trading
Phase 4-5:  Add Binance (crypto) -- only for strategies where 30% tax still leaves profit
Phase 6+:   Add IBKR (options) -- when capital justifies it ($110K+ for portfolio margin)
Later:      Direct exchange access via FIX protocol (if volume justifies it)
```

---

### Q4: Which Market is Best for Automated Bots?

**Ranked by profitability potential for Prince's situation (India-based, learning, moderate capital):**

| Rank | Market | Avg Bot Returns | Volatility | Liquidity | Tax (India) | Hours | Best For |
|---|---|---|---|---|---|---|---|
| **#1** | **Gold (XAUUSD)** | 3-8%/mo verified | High (2026: extreme volatility) | Deep | Slab rate (5-20%) via offshore broker | 23/5 | Scalping + Day trading |
| **#2** | **Forex majors** (EUR/USD, GBP/USD) | 2-6%/mo verified | Medium | Deepest ($7T/day) | Slab rate (5-20%) via offshore broker | 24/5 | Scalping + Swing |
| **#3** | **Crypto futures** (BTC, ETH perps) | 5-15%/mo (higher risk) | Highest | Good (Binance) | 30% flat + 1% TDS | 24/7 | Volatile plays, funding arb |
| **#4** | **US Equities** | 1-5%/mo | Medium | Excellent | 15% LTCG / 30% STCG | 6.5hrs/day (9:30-4 ET) | Day trading, options |
| **#5** | **Crypto spot** | 1-5%/mo | High | Varies | 30% flat + 1% TDS | 24/7 | Grid bots, DCA |

---

### Q5: What Systems Exist in the Market?

**CATEGORY 1: Commercial No-Code Bots**

| System | Type | Market | Monthly Returns (verified) | Cost | Strategy Control |
|---|---|---|---|---|---|
| **3Commas** | DCA + Grid + Signal | Crypto | 1.5-3%/mo (15-30% annual) | $40/mo Pro | Medium |
| **Pionex** | 16 built-in bots | Crypto | 1-4%/mo (grid bots in ranging) | Free | Low |
| **Cryptohopper** | AI + marketplace | Crypto | 1-3%/mo | $50-100/mo | Medium |
| **WunderTrading** | TradingView signal execution | Crypto + Stocks | 1-2.5%/mo | $25-50/mo | High |
| **Bitsgap** | Grid + DCA + Arbitrage | Crypto | 2-4%/mo (grid in sideways) | $30-90/mo | Medium |

**Verdict:** 12-25% annual. Safe but mediocre. You're renting someone else's edge.

**CATEGORY 2: Forex Expert Advisors (EAs)**

| System | Strategy | Verified Returns | Drawdown | Track Record |
|---|---|---|---|---|
| **Apex Drawdown Zero** | Multi-algo gold | +106% total, ~3%/mo | 0.39% (!!) | Verified MyFxBook |
| **Forex Fury** | Scalping, low frequency | 93% win rate | 15-20% | 57+ consecutive profitable months |
| **Forex Gold Investor** | Gold scalp + trend | +297% total, ~3.19%/mo | 42% (high) | 4+ years verified MyFxBook |
| **WallStreet Forex Robot** | Multi-pair scalp | 1.5-3%/mo | 20-30% | 10+ year track record |
| **Gold Scalper Pro** | XAUUSD scalping | 5-15%/mo (backtest) | 15-30% | Backtested 2004-2026 |

**Verdict:** 2-8% monthly for the best verified EAs. But drawdowns can be 20-40%. And you don't own the logic.

**CATEGORY 3: Open-Source/Custom Frameworks**

| System | Type | Verified Returns | Cost | Strategy Quality |
|---|---|---|---|---|
| **FreqTrade** (open source) | Crypto bot framework | User-dependent (some report 3-10%/mo) | Free + $10/mo VPS | Full control |
| **QuantConnect** (LEAN) | Multi-asset quant platform | Leaderboard Sharpe 2-4+ | Free tier, $20-40/mo | Professional-grade |
| **NautilusTrader** (open source) | Event-driven engine | No public leaderboard | Free | Institutional-grade, Rust core |
| **Jesse** (open source) | Crypto algo framework | User-dependent | Free | Simple, good for beginners |

**CATEGORY 4: Professional/Institutional Systems**

| System | Returns | Cost | Access |
|---|---|---|---|
| **Renaissance Medallion Fund** | ~66% annual (before fees) | Closed to outside investors | Employee-only |
| **Two Sigma** | 15-25% annual net | $10B+ AUM | Accredited investors only |
| **Prop firm funded accounts** | Traders keep 80-90% of profits | $100-500 challenge fee | Pass evaluation |
| **Custom hedge fund bot** | 20-60%+ | $40K-$400K development | Build it yourself |

---

### Q6: Our System vs The Competition — Head-to-Head

| Feature | 3Commas / Pionex | Forex EAs (Apex, Fury) | FreqTrade | **OUR SYSTEM** |
|---|---|---|---|---|
| **Expected Monthly Return** | 1-3% | 2-5% | 3-10% (user skill) | **3-8% target** |
| **Expected Annual Return** | 12-25% | 24-60% | 36-120% (high variance) | **36-96% target** |
| **Expected Sharpe Ratio** | 0.5-1.0 | 0.8-1.5 | 1.0-2.0 | **>1.5 target** |
| **Max Drawdown** | 15-30% | 20-40% | User-dependent | **<15% (circuit breakers)** |
| **Execution Speed** | 2-10s | 100ms-1s (MT5) | 200ms-2s | **<200ms** |
| **Multi-Market** | Crypto only | Forex only | Crypto only | **Forex + Crypto + Stocks + Options** |
| **AI/LLM Intelligence** | Basic AI labels | None | None | **Multi-agent Claude (6 specialist agents)** |
| **Strategy Quality** | Grid/DCA (simple) | Fixed EAs (no customization) | Full custom | **SMC/ICT + Multi-indicator + Pine import** |
| **Risk Management** | Basic stop loss | Per-trade only | Configurable | **Separate process, circuit breakers, Kelly, Greeks** |
| **Strategy Import** | TradingView signals | Buy from MQL5 market | Code yourself | **Pine Script auto-convert + any bot analysis** |
| **Monthly Cost** | $40-100 | $100-500 (EA purchase) | $10 VPS | **$100-250** |
| **Ownership** | Renting | Renting logic | Own everything | **Own everything** |

**Where our system genuinely wins:**
1. Multi-market (one system for forex + crypto + stocks + options)
2. AI layer (no commercial bot has multi-agent LLM intelligence)
3. Risk management (separate process with circuit breakers)
4. Strategy flexibility (import ANY strategy from Pine Script or other bots)
5. Speed (10-100x faster than commercial bots)
6. No recurring strategy fees (you own the code)

---

### Q7: Exact Cost Breakdown

**ONE-TIME BUILD COSTS:**

| Item | Cost | Notes |
|---|---|---|
| Development time | $0 (your time + Claude) | You're building it, not hiring |
| VectorBT Pro | $149 | One-time backtesting license |
| IC Markets account | $200 | Minimum deposit (trading capital) |
| Binance account | $0 | Free, KYC required |
| Alpaca account | $0 | Free, already have |
| IBKR account (later) | $0 | No minimum, but $110K for portfolio margin |
| **Total build cost** | **~$150** | + your time and learning |

For comparison: hiring a developer to build this = $40,000-$150,000.

**MONTHLY OPERATING COSTS:**

| Item | Phase 1-3 (Learning) | Phase 4-6 (Paper to Live) | Phase 7+ (Full Production) |
|---|---|---|---|
| VPS (AWS Singapore) | $0 (run locally) | $30-60 | $60-100 |
| IC Markets data | $0 (free with account) | $0 | $0 |
| TradingView Pro | $15-60 (already have) | $15-60 | $15-60 |
| Polygon.io (US stocks) | $0 (not needed yet) | $29 | $29-79 |
| Claude API (AI agents) | $0 (use Claude Code) | $30-50 | $50-150 |
| Redis/TimescaleDB | $0 (self-hosted) | $0 (self-hosted) | $0 (self-hosted on VPS) |
| Telegram Bot | $0 | $0 | $0 |
| **Total Monthly** | **$15-60** | **$100-200** | **$160-400** |

**EXPECTED RETURNS vs COSTS (conservative 3%/month):**

| Trading Capital | Monthly Gross (3%) | Monthly Cost | Monthly Net | Annual Net Return % |
|---|---|---|---|---|
| $1,000 | $30 | $100 | -$70 (loss) | NOT viable |
| $5,000 | $150 | $150 | $0 (breakeven) | 0% |
| $10,000 | $300 | $200 | $100 | 12% |
| $25,000 | $750 | $200 | $550 | 26.4% |
| $50,000 | $1,500 | $250 | $1,250 | 30% |
| $100,000 | $3,000 | $300 | $2,700 | 32.4% |

**Minimum viable capital: $10,000.** Sweet spot: $25,000-$50,000.

---

### Q8: V2 Roadmap — Improve Profits in Later Phases

| Add-On | Expected Improvement | When | Complexity |
|---|---|---|---|
| **Multi-strategy portfolio** (5-10 uncorrelated) | +30-50% risk-adjusted return | After 3 profitable strategies | Medium |
| **Funding rate arbitrage** (crypto) | +1-2%/month passive, market-neutral | After crypto module | Low |
| **Options wheel strategy** (IBKR) | +1-3%/month income on positions | After $25K+ capital | Medium |
| **ML signal classification** (gradient boosted trees) | +10-20% signal accuracy | After 6 months of data | High |
| **Cross-exchange arbitrage** | +0.5-1%/month | After 2+ exchange connections | Medium |
| **Prop firm funded accounts** | 5-10x capital without own money | After 3 months live profitable | Low |
| **Copy trading income** | Passive income from followers | After verified track record | Low |

**Prop firm path:** Once profitable for 3+ months, apply to FTMO/MyForexFunds, pass challenge with our bot ($100-500 fee), get funded $10K-$200K, keep 80-90% of profits.

**Version 3 (12+ months out):** Rust hot paths, FIX protocol, managed fund structure (2/20 fees), multi-exchange smart order routing, real-time ML regime detection.

---

## FLEXIBLE STRATEGY ARCHITECTURE

The system supports ALL risk profiles simultaneously:

```
STRATEGY REGISTRY

Each strategy registers with:
- name: "asian_range_breakout"
- risk_profile: SAFE | MODERATE | AGGRESSIVE
- expected_monthly: 5%
- expected_drawdown: 2%
- expected_sharpe: 2.0
- markets: ["XAUUSD"]
- min_capital: $500
- correlation_group: "gold_scalping"

SAFE strategies:        MODERATE strategies:    AGGRESSIVE:
- Asian range breakout  - SMC/ICT liquidity    - Quantum-style
- EMA crossover (1m)   - Multi-indicator       - Momentum burst
- Grid (ranging mkt)   - Pairs trading         - Full Kelly size
- Iron condors          - CVD divergence       - Leveraged perps
                        - Vol selling           - 0DTE options

System can run ANY combination simultaneously
AI Money Manager decides allocation per strategy
```

**Strategy Optimization Engine:**
- Each strategy has tunable parameters (lookback, threshold, TP/SL ratio)
- VectorBT runs overnight parameter sweeps on latest 30 days of data
- Walk-forward validation prevents overfitting
- Strategies that degrade below Sharpe 0.5 get auto-paused
- Strategies that improve get more capital allocated

---

## MASTER MONEY MANAGEMENT SYSTEM (M3S)

This is the single most valuable component. No retail system has this. Hedge funds have entire teams doing this manually. We're building it as an AI-powered engine.

### Money Management Modes

**MODE 1: FORTRESS (Ultra-Safe)**
- Max 0.5% risk per trade
- Quarter Kelly (0.25x) sizing
- Only SAFE-tagged strategies active
- No compounding (profits go to reserve)
- Target: 2-3%/mo, <2% drawdown
- Like Apex Drawdown Zero

**MODE 2: BALANCED**
- Max 1% risk per trade
- Half Kelly (0.5x) sizing
- SAFE + MODERATE strategies active
- Compound 30% of weekly profit
- Target: 5-8%/mo, <10% drawdown

**MODE 3: ASSAULT (Aggressive)**
- Max 2% risk per trade
- Full Kelly (capped at 2%) sizing
- ALL strategies active including AGGRESSIVE
- Compound 70% of profits immediately
- Target: 10-15%/mo, <25% drawdown

**MODE 4: AI ADAPTIVE (The Innovation)**
- Claude agent analyzes ALL inputs in real-time
- Dynamically blends Mode 1-3 based on conditions
- THIS IS WHAT HEDGE FUNDS DO

### Capital Allocation Engine

Step 1: Score each strategy
  score = rolling_sharpe * (1 - correlation_penalty) * regime_fit_bonus * recent_performance_weight

Step 2: Kelly-weighted allocation
  raw_alloc[i] = kelly_fraction * score[i] / sum(scores)
  alloc[i] = min(raw_alloc[i], max_per_strategy)
  remainder goes to cash reserve

Step 3: Correlation constraint
  If corr(strategy_A, strategy_B) > 0.7: combined_alloc(A + B) <= 40% of capital

Step 4: Apply mode caps
  FORTRESS: no single strategy > 15% of capital
  BALANCED: no single strategy > 25% of capital
  ASSAULT:  no single strategy > 35% of capital
  ADAPTIVE: Claude decides caps based on conditions

### Compounding Engine

Tracks two pools: BASE CAPITAL (trading principal) and PROFIT POOL (accumulated net profit)

**FORTRESS:** Compound 0% (linear growth, maximum safety)
**BALANCED:** Compound 30% of weekly profit to base capital. Rebalance every Monday.
**ASSAULT:** Compound 70% of daily profit to base capital. Rebalance after each profitable trade.
**AI ADAPTIVE:** Claude decides compounding % based on:
- Winning streak (3+ consecutive) -> compound 80%
- In drawdown -> compound 0%, preserve capital
- Big single win (>2% in one trade) -> compound 50%
- Daily target already hit -> stop trading, bank it
- Week already +5% -> shift to Fortress for rest

### AI Advisor (Claude Agent — "The PhD")

System prompt: "You are a PhD-level quantitative portfolio manager with expertise in Kelly criterion, risk parity, dynamic allocation, and compounding."

Runs every: 30 minutes (during market hours) + After every trade that changes P&L by >0.5% + At end of day (mandatory daily review)

Output (structured JSON):
```json
{
  "mode": "ADAPTIVE",
  "mode_blend": {"fortress": 0.2, "balanced": 0.6, "assault": 0.2},
  "allocation": {
    "asian_breakout": 0.25,
    "smc_liquidity": 0.20,
    "ema_crossover": 0.15,
    "iron_condor": 0.10,
    "cash_reserve": 0.30
  },
  "compound_pct": 0.50,
  "reasoning": "Strong week (+4.2%), but FOMC tomorrow. Reduce aggressive exposure, keep cash buffer high. Compound 50% of profit -- save rest as event hedge."
}
```

Uses Haiku for routine checks, Sonnet for daily review. Cost: ~$2-4/day.

### M3S Impact on Returns

| Allocation Method | Expected Sharpe | Expected Monthly | Max Drawdown |
|---|---|---|---|
| Equal weight (naive) | 1.0-1.3 | 3-5% | 15-20% |
| Static Kelly | 1.3-1.6 | 4-7% | 12-18% |
| **Dynamic AI allocation (M3S)** | **1.6-2.2** | **5-10%** | **8-15%** |
| + AI compounding on top | **1.8-2.5** | **6-12%** | **10-18%** |

### M3S vs Competition

| Feature | Commercial Bots | Forex EAs | Hedge Funds | **Our M3S** |
|---|---|---|---|---|
| Multiple strategies | 1-3 (preset) | 1 per EA | 50+ | **5-10 (flexible)** |
| Dynamic allocation | None | None | Human team | **AI-powered (Claude)** |
| Mode switching | Manual (user) | None | Manual (CIO) | **Auto + Manual override** |
| Compounding | Basic auto-compound | None | Sophisticated | **AI-adaptive compounding** |
| Correlation-aware | No | No | Yes | **Yes (auto-penalizes)** |
| Regime-adaptive | No | No | Yes | **Yes (meta-strategist agent)** |
| Risk budgeting | Basic % | Fixed lot | Kelly variants | **Fractional Kelly + AI** |
| Cost | $40-100/mo | $500-700 one-time | Millions | **~$150 build + $200/mo** |

---

## ALL 17 CORRECTIONS TO BLUEPRINT

### CRITICAL ISSUES (Must Fix)

**1. MetaTrader 5 Does NOT Work on macOS**
The `MetaTrader5` PyPI package only ships Windows x64 wheels. Fix: Drop MT5 entirely. Use IBKR for forex via `ib_insync` (pure Python, works on macOS, gives forex + futures + options from one broker).

**2. Pandas-ta + Polars Conflict**
pandas-ta only works with pandas DataFrames. Fix: Use pandas as the primary DataFrame library for the strategy layer. Use Polars only for bulk data processing (backtest parameter sweeps, historical data ETL).

**3. Backtesting Should Come BEFORE Live Trading**
Blueprint had Phase 2 = First Strategy + Execution, Phase 4 = Backtesting. Fix: Merge backtesting into Phase 2. Never deploy a strategy you haven't validated historically.

**4. Scope is 12-18 Months, Not 28 Weeks**
Fix: Don't put timelines on phases. Use milestones instead.

**5. India-Specific Regulatory Issues**
Binance legal in India but 30% crypto tax + 1% TDS. Deribit restricted for Indian residents. Fix: Add India tax impact per strategy. Focus high-frequency on forex/gold where tax is 5-20% slab rate.

**6. Docker in Phase 1 is Premature**
Fix: Phase 1 uses Python venv or uv. Docker only when deploying to VPS (Phase 7).

### MODERATE ISSUES (Should Fix)

**7. LangGraph is Overkill for the Agent Layer**
Fix: Build simple `orchestrator.py` with direct Claude API calls + `asyncio.gather()`. Add LangGraph later only if needed.

**8. Too Many Backtesting Frameworks**
Fix: Pick TWO — VectorBT (fast parameter sweeps) + NautilusTrader (tick-level validation + live bridge). Drop FreqTrade and QuantConnect.

**9. TimescaleDB in Phase 1 is Premature**
Fix: Phase 1-3 use Parquet files + SQLite. Migrate to TimescaleDB in Phase 4+.

**10. FinRL (Reinforcement Learning) is Fragile**
Fix: Remove from roadmap. If ML needed, start with gradient-boosted trees for signal classification.

**11. Claude API Cost Needs Caching Strategy**
Fix: Cache sentiment analysis (15 min), regime detection (30 min). Use Haiku for high-frequency calls (10x cheaper). Use Sonnet only for complex reasoning.

### ADDITIONAL CORRECTIONS (from Q&A)

**12. Strategy upgrade** — Replace EMA/RSI as primary with SMC/ICT + Multi-Indicator Fusion. Add `smart-money-concepts` Python package.

**13. Pine Script ingestion system** — Add strategy import pipeline: Claude analyzes Pine/bot -> converts to Python -> auto-backtest -> deploy.

**14. IC Markets as primary forex broker** — cTrader Python API, 0.0 pip raw spread, better tax treatment than crypto for Indian residents.

**15. Tax-optimized strategy allocation** — Focus high-frequency scalping on forex/gold (IC Markets) not crypto (Binance) due to 30% vs slab rate tax difference.

**16. Flexible strategy architecture** — Every strategy tagged SAFE/MODERATE/AGGRESSIVE. System runs any combination simultaneously.

**17. Master Money Management System (M3S)** — AI-powered dynamic capital allocation with 4 modes, compounding engine, and Claude "PhD Advisor" agent. Build in Phase 3.

---

## WHAT'S EXCELLENT (Keep As-Is)

1. **Risk as separate ZeroMQ process** — Best design decision. If risk process dies, trading halts. Cannot be bypassed by buggy strategy code.
2. **Same-code backtest/live pattern** — `on_candle()` runs identically in backtest and live. Only data source and executor change.
3. **Circuit breakers with escalating severity** — Daily (halt), Weekly (reduce size), Monthly (halt + review), Max drawdown (emergency close all).
4. **Pre-trade risk checks** — Fat finger protection, max position, daily P&L limit, gross exposure, correlation check.
5. **Strategy selection matrix based on market regime** — Meta-Strategist agent switches modules on/off per conditions.
6. **Multi-agent Bull vs Bear debate** — Adversarial reasoning reduces overconfident signals.
7. **Options Greeks risk limits** — Portfolio-level delta, vega, theta, gamma limits.
8. **Break-even analysis** — Realistic cost assessment.
9. **Data pipeline architecture** — Hot (Redis) + Warm (TimescaleDB) + Cold (Parquet files).
10. **Project file structure** — Clean, well-organized, follows Python packaging conventions.

---

## CORRECTED BUILD ORDER (Milestone-Based)

### Phase 1 — Data Foundation
- Project structure (folders, `pyproject.toml`, venv)
- Config system (TOML files)
- Async Binance WebSocket client (tick + kline streams)
- Candle builder (OHLCV from raw ticks)
- Indicator engine (pandas-ta on pandas DataFrames)
- Redis for hot data cache
- SQLite for trade log (upgrade to TimescaleDB later)
- Parquet for historical bar storage
- structlog + basic Telegram alerts
- **NO Docker, NO TimescaleDB, NO Grafana yet**

### Phase 2 — First Strategy + Backtest + Paper Trade
- EMA crossover + RSI scalp strategy (Module 2)
- VectorBT backtest on 2+ years of Binance 1m data
- Walk-forward validation (in-sample to out-of-sample split)
- Performance metrics (Sharpe, Sortino, max drawdown, win rate)
- Paper trading mode (simulated fills from live data)
- Basic P&L tracking
- **Milestone: Strategy shows positive Sharpe > 1.0 on 2-year backtest with walk-forward validation**

### Phase 3 — Risk Management + M3S
- Risk manager as separate ZeroMQ process
- Pre-trade checks (all 6)
- Circuit breakers (daily, weekly, monthly, max drawdown)
- Fractional Kelly position sizing
- Portfolio heat monitor
- Kill switch via Telegram
- **Master Money Management System (M3S):**
  - Strategy registry with risk profiles
  - 4 money management modes (Fortress, Balanced, Assault, AI Adaptive)
  - Capital allocation engine (Kelly-weighted, correlation-aware)
  - Compounding engine (pool tracking, mode-dependent rules)
  - AI Advisor agent (Claude, runs every 30 min + end of day)
  - Mode switching: manual override + AI recommendation
- **Milestone: Risk system prevents paper losses beyond defined limits for 30 consecutive days**

### Phase 4 — Multi-Exchange Execution
- Binance execution (WebSocket orders)
- Alpaca execution (already have MCP, add native Python client)
- IBKR execution via `ib_insync` (replaces MT5)
- IC Markets execution via cTrader Open API
- Paper executor (simulated)
- Order state tracking
- **Milestone: Successfully execute paper trades on 2+ exchanges with <200ms latency**

### Phase 5 — AI Agent Intelligence
- Claude API integration (direct calls, no LangGraph)
- Technical Analyst agent (reads indicators)
- Sentiment Analyst agent (news)
- Simple orchestrator with weighted consensus
- Caching layer (reduce API costs by 60%+)
- **Milestone: AI-enhanced signals improve backtest Sharpe by >0.2 vs non-AI baseline**

### Phase 6 — Options Module
- IBKR options chain data
- Greeks engine (py_vollib)
- Iron condor strategy (automated)
- Portfolio Greeks tracking
- Delta hedging
- **Milestone: Iron condor strategy shows positive backtest on 1 year of SPX data**

### Phase 7 — Production Deployment
- Docker Compose for full stack
- AWS Singapore VPS for crypto strategies
- TimescaleDB migration (replace SQLite + Parquet)
- Prometheus + Grafana monitoring
- Automated daily backtest re-runs
- **Milestone: System runs 7 days unattended on VPS without intervention**

### Phase 8 — Scale
- Additional strategies (2-3 more, uncorrelated)
- Ultra scalping module (if latency targets achievable)
- Multi-agent debate system (Bull vs Bear)
- Meta-Strategist regime detector
- Performance dashboard
- **Milestone: 3+ strategies running with portfolio Sharpe > 1.5**

---

## FILES TO MODIFY (Implementation)

1. `/Users/prince/algo-trading/TRADING_SYSTEM_BLUEPRINT.md` — Apply all 17 corrections
2. `/Users/prince/algo-trading/README.md` — Update roadmap to match corrected phases
3. `/Users/prince/.claude/projects/-Users-prince/memory/project_algo_trading.md` — Update with corrected architecture decisions

---

*Plan approved: April 11, 2026*
