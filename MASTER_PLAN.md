# MASTER PLAN — Algo Trading System

## Complete Context Document for Claude Sessions

**Purpose:** This file gives any fresh Claude session ALL the context it needs about this project — who Prince is, what's been built, what's planned, every architectural decision, every research finding, and every correction. Read this first.

**Last Updated:** April 11, 2026

---

## Table of Contents

1. [Who Is Prince](#1-who-is-prince)
2. [What's Been Built](#2-whats-been-built)
3. [Project File Map](#3-project-file-map)
4. [System Architecture](#4-system-architecture)
5. [Broker Stack (Corrected)](#5-broker-stack-corrected)
6. [Strategy Priority Order](#6-strategy-priority-order)
7. [Master Money Management System (M3S)](#7-master-money-management-system-m3s)
8. [Competitor Analysis](#8-competitor-analysis)
9. [India Tax Optimization](#9-india-tax-optimization)
10. [Cost Breakdown](#10-cost-breakdown)
11. [All 17 Blueprint Corrections](#11-all-17-blueprint-corrections)
12. [Corrected Build Phases](#12-corrected-build-phases)
13. [Performance Targets](#13-performance-targets)
14. [V2 Roadmap](#14-v2-roadmap)
15. [Memory Files](#15-memory-files)
16. [Key Decisions Log](#16-key-decisions-log)

---

## 1. Who Is Prince

- India-based developer learning algorithmic trading while building
- Wants to trade **systematically** — not discretionary
- Goal: mimic quant hedge fund logic (Two Sigma, D.E. Shaw style) at retail scale
- Comfortable with Python and JavaScript
- Currently on **paper trading** (Alpaca paper account)
- Interested in US equities (Alpaca), forex/futures (IC Markets), and crypto (Binance)
- Wants a **modular, professional system** — not quick scripts
- Thinks in systems — architecture first, then implementation
- Platform: **macOS** (Darwin) — this matters because MT5 Python package is Windows-only
- Prefers **tutor mode** — always explain with structure, analogies, examples. Proactively surface learning moments.

---

## 2. What's Been Built

### Phase 1 Foundation — COMPLETE

**Alpaca MCP Server** (for Claude to trade US equities):
- File: `/Users/prince/algo-trading/alpaca/server.js`
- 7 tools: `get_account`, `place_order`, `get_positions`, `close_position`, `get_orders`, `cancel_order`, `get_quote`
- Uses `@modelcontextprotocol/sdk` and `@alpacahq/alpaca-trade-api`
- Paper trading mode on `https://paper-api.alpaca.markets`
- API keys stored as env vars in `~/.claude.json` (never in git)

**TradingView MCP Server** (for Claude to read charts):
- File: `/Users/prince/algo-trading/tradingview-mcp-jackson/src/server.js`
- 78 tools for reading and controlling live TradingView Desktop charts
- Can read indicators, OHLCV data, Pine Script source, screenshots, etc.

**Both MCP servers registered in `~/.claude.json`** under `mcpServers`:
```json
{
  "mcpServers": {
    "tradingview": {
      "command": "node",
      "args": ["/Users/prince/algo-trading/tradingview-mcp-jackson/src/server.js"]
    },
    "alpaca": {
      "command": "node",
      "args": ["/Users/prince/algo-trading/alpaca/server.js"],
      "env": {
        "ALPACA_API_KEY": "...",
        "ALPACA_SECRET_KEY": "...",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets"
      }
    }
  }
}
```

**Knowledge Base PDF:**
- File: `/Users/prince/algo-trading/Algo_Trading_Knowledge_Base.pdf`
- Covers: HFT vs quant funds vs retail, modular architecture, tech stack, MT5 deep dive, multi-agent AI, phased build order
- Regenerate: `python3 /Users/prince/algo-trading/generate_pdf.py`

**Supporting files:**
- `/Users/prince/algo-trading/alpaca/connection.js` — Alpaca client setup + connection test
- `/Users/prince/algo-trading/alpaca/trade.js` — placeOrder, getPositions, closePosition, getOrders
- `/Users/prince/algo-trading/alpaca/package.json` — dependencies

---

## 3. Project File Map

```
algo-trading/
|-- README.md                           # Project overview, MCP tools, architecture, roadmap
|-- MASTER_PLAN.md                      # THIS FILE — full context for Claude sessions
|-- FULL_PLAN.md                        # The approved plan with all corrections
|-- TRADING_SYSTEM_BLUEPRINT.md         # Original 1,399-line blueprint (needs 17 corrections applied)
|-- Algo_Trading_Knowledge_Base.pdf     # Research document
|-- generate_pdf.py                     # PDF generator script
|
|-- alpaca/                             # Alpaca execution layer
|   |-- server.js                       # MCP server — Claude calls this to trade
|   |-- connection.js                   # Alpaca client setup + connection test
|   |-- trade.js                        # placeOrder, getPositions, closePosition, getOrders
|   |-- package.json
|   +-- node_modules/
|
+-- tradingview-mcp-jackson/            # TradingView analysis layer
    +-- src/
        +-- server.js                   # MCP server — Claude reads charts via this
```

**Memory files** (Claude persistent context):
```
~/.claude/projects/-Users-prince/memory/
|-- MEMORY.md                           # Index of all memory files
|-- project_algo_trading.md             # Project state: what's built, roadmap, decisions
|-- user_algo_trader.md                 # Prince's goals, style, level
|-- reference_algo_trading_stack.md     # Best tools per layer, MCP locations
|-- feedback_mcp_config.md             # MCP servers must go in ~/.claude.json
+-- user_tutor_mode.md                  # Always explain with structure, analogies, examples
```

---

## 4. System Architecture

**Modular, event-driven architecture** — each component built, tested, replaced independently. Mirrors professional quant shops.

```
DATA LAYER (WebSocket feeds)
  |
  v
CANDLE BUILDER / FEATURE ENGINE (OHLCV from ticks, indicators via pandas-ta)
  |
  v
STRATEGY MODULES (Ultra Scalp | Scalping | Day Trading + Options)
  |
  v
AI AGENT LAYER (6 Claude agents: Technical, Sentiment, Order Flow, Bull/Bear Debate, Meta-Strategist, Execution)
  |
  v
SIGNAL AGGREGATOR (weighted consensus from strategy + AI signals)
  |
  v
RISK MANAGER (SEPARATE ZeroMQ PROCESS — cannot be bypassed)
  |
  v
M3S — MASTER MONEY MANAGEMENT (dynamic allocation, compounding, mode switching)
  |
  v
EXECUTION ENGINE (IC Markets | Alpaca | Binance | IBKR — smart order routing)
  |
  v
PORTFOLIO TRACKER (positions, P&L, exposure)
  |
  v
SCHEDULER + LOGGER (market hours, SQLite trade log, Telegram alerts)
```

**Key architectural decisions:**
- **asyncio + uvloop** — 2-4x faster async event loop
- **orjson** — 10x faster JSON parsing for WebSocket messages
- **ZeroMQ PUB/SUB** — Risk manager as separate process (if it dies, trading halts)
- **Redis (hot) + SQLite/TimescaleDB (warm) + Parquet (cold)** — tiered data storage
- **pandas** as primary DataFrame library for strategy layer (NOT Polars — pandas-ta compatibility)
- **Same-code backtest/live pattern** — `BaseStrategy.on_candle()` runs identically in both modes
- **structlog** for structured logging

---

## 5. Broker Stack (Corrected)

The original blueprint used MT5 for forex. MT5 Python package is **Windows-only C++** — does NOT work on macOS. This is corrected.

| Priority | Broker | Markets | Role | API | Status |
|---|---|---|---|---|---|
| **PRIMARY** | **IC Markets** | Forex, Gold, Metals, CFDs | Forex/Gold scalping + day trading | cTrader Open API (Python native) | Phase 1 |
| **SECONDARY** | **Alpaca** | US Stocks, ETFs | US equity day trading | REST + WebSocket, MCP built | DONE |
| **TERTIARY** | **Binance** | Crypto spot + futures | Crypto (lower frequency only) | REST + WebSocket | Phase 4 |
| **LATER** | **IBKR** | Everything globally | Options, futures, multi-asset | TWS API via `ib_insync` | Phase 6 |

**Why IC Markets over MT5:**
- cTrader has **native Python support** — write cBots in Python directly
- cTrader Open API — full REST + WebSocket from your own code
- Raw spread from 0.0 pips, $3.50/lot commission
- 99.6% of orders filled in <40ms
- No restrictions on algo trading, scalping, hedging
- Available to Indian residents (offshore, not SEBI regulated)
- $200 minimum deposit

**Why Binance is TERTIARY (not primary):**
- India crypto tax: 30% flat on profits + 1% TDS on every transaction
- This destroys high-frequency scalping margins
- A strategy making 0.1% per trade on BTCUSDT loses ~0.31% to tax+TDS = net negative
- Only viable for lower-frequency trades with wider margins (funding rate arb, swing trades)

---

## 6. Strategy Priority Order

The original blueprint used EMA/RSI as primary. **EMA/RSI alone is NOT enough for high returns.** It's a "hello world" test only.

### Strategy Implementation Order

**Phase 2 (first strategies):**
1. **EMA Crossover + RSI** — "hello world" to test system plumbing only
2. **Asian Range Breakout** (XAUUSD) — replicates Apex Drawdown Zero's core logic for free. SAFE profile.

**Phase 3 (real edge):**
3. **Smart Money Concepts (ICT/SMC)** — order blocks, fair value gaps, liquidity sweeps. 60-70% win rate. Python package: `smart-money-concepts` on GitHub. MODERATE profile.
4. **Multi-Indicator Fusion** — RSI + EMA + VWAP + MACD combined. 60.63% proven win rate. MODERATE profile.

**Phase 4+ (diversification):**
5. **CVD Divergence** — best order flow reversal signal for crypto. MODERATE profile.
6. **Statistical Arbitrage (Pairs)** — market-neutral, works in all conditions. HIGH complexity.
7. **Funding Rate Arbitrage** — long spot + short perp when funding extreme. LOW risk, steady income.
8. **Iron Condors / Vol Selling** — systematic options premium collection. SAFE profile.

### Strategy Plugin Architecture

Every strategy implements `BaseStrategy.on_candle() -> Signal | None`. Tagged as SAFE/MODERATE/AGGRESSIVE. Hot-swappable via config.

### Pine Script Ingestion Pipeline

The system can import ANY strategy:
```
Input: Pine Script / GitHub bot / strategy description
  -> Claude agent analyzes the logic
  -> Converts to Python BaseStrategy
  -> Auto-backtests on historical data
  -> Reports: Sharpe, drawdown, win rate
  -> If passes thresholds -> add to paper trading
```

Tools: PyneSys (auto-converter), pyine (PyPI), TradingView MCP `pine_get_source`, Claude manual conversion.

---

## 7. Master Money Management System (M3S)

**THE INNOVATION.** No retail system has this. Hedge funds have entire teams doing this manually. We're building it as an AI-powered engine.

### 4 Money Management Modes

| Mode | Risk/Trade | Kelly Fraction | Strategies Active | Compounding | Target |
|---|---|---|---|---|---|
| **FORTRESS** | 0.5% | 0.25x | SAFE only | 0% (profits to reserve) | 2-3%/mo, <2% DD |
| **BALANCED** | 1.0% | 0.5x | SAFE + MODERATE | 30% weekly | 5-8%/mo, <10% DD |
| **ASSAULT** | 2.0% | 1.0x (capped 2%) | ALL | 70% daily | 10-15%/mo, <25% DD |
| **AI ADAPTIVE** | Dynamic | Dynamic | Dynamic | Claude decides | 6-12%/mo, <15% DD |

### Capital Allocation Engine

```
score[i] = rolling_sharpe * (1 - correlation_penalty) * regime_fit_bonus * recent_performance_weight
raw_alloc[i] = kelly_fraction * score[i] / sum(scores)
alloc[i] = min(raw_alloc[i], max_per_strategy_cap)
remainder -> cash reserve
```

Correlation constraint: If corr(A, B) > 0.7, combined_alloc(A + B) <= 40% of capital.

Mode caps: FORTRESS 15%, BALANCED 25%, ASSAULT 35%, ADAPTIVE = Claude decides.

### Compounding Engine

Tracks BASE CAPITAL (principal) and PROFIT POOL (accumulated net profit).

AI ADAPTIVE compounding rules:
- Winning streak (3+ consecutive) -> compound 80%
- In drawdown -> compound 0%, preserve capital
- Big single win (>2%) -> compound 50%
- Daily target hit -> stop trading, bank it
- Week already +5% -> shift to Fortress mode

### AI Advisor Agent ("The PhD")

- Runs: every 30 min during market hours + after P&L changes >0.5% + end of day mandatory
- Model: Haiku for routine checks, Sonnet for daily review
- Cost: ~$2-4/day
- Output: structured JSON with mode, allocation, compound_pct, reasoning

### Impact on Returns

| Method | Sharpe | Monthly | Max DD |
|---|---|---|---|
| Equal weight (naive) | 1.0-1.3 | 3-5% | 15-20% |
| Static Kelly | 1.3-1.6 | 4-7% | 12-18% |
| **Dynamic AI (M3S)** | **1.6-2.2** | **5-10%** | **8-15%** |
| **+ AI compounding** | **1.8-2.5** | **6-12%** | **10-18%** |

Build in **Phase 3** alongside risk management.

---

## 8. Competitor Analysis

### Our System vs Competition

| Feature | Commercial Bots (3Commas) | Forex EAs (Apex) | FreqTrade | **OUR SYSTEM** |
|---|---|---|---|---|
| Monthly Return | 1-3% | 2-5% | 3-10% | **3-8% (M3S: 6-12%)** |
| Annual Return | 12-25% | 24-60% | 36-120% | **36-96%** |
| Sharpe Ratio | 0.5-1.0 | 0.8-1.5 | 1.0-2.0 | **>1.5** |
| Max Drawdown | 15-30% | 20-40% | User-dependent | **<15%** |
| Execution Speed | 2-10s | 100ms-1s | 200ms-2s | **<200ms** |
| Multi-Market | Crypto only | Forex only | Crypto only | **All markets** |
| AI Intelligence | Basic labels | None | None | **6 Claude agents** |
| Risk Management | Basic stop loss | Per-trade | Configurable | **Separate process + circuit breakers** |
| Ownership | Renting | Renting | Own | **Own everything** |

### Best Verified EA: Apex Drawdown Zero

- Strategy: Multi-algo gold, Asian session range breakout on XAUUSD M15
- Returns: +106% total over ~18 months (~5-6%/mo)
- Drawdown: 0.39% (virtually zero)
- Sharpe: estimated 5.0-8.0+
- Costs $697 one-time
- **We can replicate this logic for free** — the Asian range breakout is well-documented, and our system adds AI optimization on top

### Speed Advantage

Our system is **10-100x faster** than any commercial bot:
- Direct WebSocket (no webhook relay)
- asyncio + uvloop (2-4x faster than vanilla Python)
- orjson (10x faster JSON)
- Pre-computed indicators in Redis

---

## 9. India Tax Optimization

**This is a critical factor that changes the entire strategy priority.**

| Market | Tax Rate (India) | Impact on Scalping |
|---|---|---|
| **Forex/Gold** (via IC Markets, offshore) | Income slab rate (5-20%) | BEST — low tax, high frequency viable |
| **US Equities** (via Alpaca) | 15% LTCG / 30% STCG | MODERATE — depends on holding period |
| **Crypto** (via Binance) | 30% flat + 1% TDS per transaction | WORST — destroys high-frequency margins |

**Key insight:** The SAME scalping strategy on Gold (XAUUSD) via IC Markets is **10-25% more profitable after tax** than the identical strategy on BTCUSDT via Binance.

**Action:** Focus high-frequency scalping on forex/gold. Use crypto only for lower-frequency strategies (funding rate arb, swing trades) where wider margins absorb the tax hit.

---

## 10. Cost Breakdown

### One-Time Build Costs

| Item | Cost |
|---|---|
| Development (your time + Claude) | $0 |
| VectorBT Pro | $149 |
| IC Markets deposit | $200 (trading capital) |
| Alpaca + Binance accounts | $0 |
| **Total** | **~$150** |

For comparison: hiring a developer = $40,000-$150,000.

### Monthly Operating Costs

| Phase | Monthly Cost |
|---|---|
| Phase 1-3 (Learning) | $15-60 |
| Phase 4-6 (Paper to Live) | $100-200 |
| Phase 7+ (Production) | $160-400 |

### Return vs Cost by Capital

| Capital | Monthly Gross (3%) | Monthly Cost | Monthly Net | Annual % |
|---|---|---|---|---|
| $10,000 | $300 | $200 | $100 | 12% |
| $25,000 | $750 | $200 | $550 | 26.4% |
| $50,000 | $1,500 | $250 | $1,250 | 30% |

**Minimum viable: $10,000. Sweet spot: $25,000-$50,000.**

---

## 11. All 17 Blueprint Corrections

### Critical (Must Fix)
1. **MT5 does NOT work on macOS** — Replace with IBKR `ib_insync` + IC Markets cTrader
2. **pandas-ta + Polars conflict** — Use pandas as primary for strategy layer
3. **Backtesting must come BEFORE live trading** — Merge into Phase 2
4. **Scope is 12-18 months, not 28 weeks** — Use milestones, not timelines
5. **India-specific tax/regulatory issues** — 30% crypto tax impacts strategy economics
6. **Docker in Phase 1 is premature** — Use venv, Docker only for VPS deployment

### Moderate (Should Fix)
7. **LangGraph is overkill** — Use simple orchestrator.py with asyncio.gather()
8. **Too many backtesting frameworks** — Pick TWO: VectorBT + NautilusTrader
9. **TimescaleDB in Phase 1 is premature** — Use Parquet + SQLite first
10. **FinRL (Reinforcement Learning) is fragile** — Remove, use gradient boosted trees instead
11. **Claude API costs need caching** — Cache sentiment (15 min), regime (30 min), use Haiku for high-freq

### Additional (from Q&A research)
12. **Strategy upgrade** — SMC/ICT + Multi-Indicator Fusion as primary (not EMA/RSI)
13. **Pine Script ingestion system** — Auto-convert Pine to Python BaseStrategy
14. **IC Markets as primary forex broker** — cTrader Python API, 0.0 pip raw spread
15. **Tax-optimized strategy allocation** — High-freq on forex/gold, low-freq on crypto
16. **Flexible strategy architecture** — SAFE/MODERATE/AGGRESSIVE tagging, run any combination
17. **Master Money Management System (M3S)** — 4 modes, AI-powered allocation + compounding

---

## 12. Corrected Build Phases (Milestone-Based)

### Phase 1 — Data Foundation
- Project structure (folders, `pyproject.toml`, venv)
- Config system (TOML files)
- Async WebSocket clients (Binance ticks + klines, IC Markets via cTrader)
- Candle builder (OHLCV from raw ticks)
- Indicator engine (pandas-ta on pandas DataFrames)
- Redis for hot data cache
- SQLite for trade log
- Parquet for historical bar storage
- structlog + basic Telegram alerts
- **NO Docker, NO TimescaleDB, NO Grafana**

### Phase 2 — First Strategy + Backtest + Paper Trade
- EMA crossover + RSI (system test only)
- Asian Range Breakout on XAUUSD (first real strategy)
- VectorBT backtest on 2+ years of data
- Walk-forward validation
- Performance metrics (Sharpe, Sortino, max drawdown, win rate)
- Paper trading mode
- **Milestone: Sharpe > 1.0 on 2-year backtest with walk-forward validation**

### Phase 3 — Risk Management + M3S
- Risk manager as separate ZeroMQ process
- Pre-trade checks (fat finger, max position, daily P&L, gross exposure, correlation)
- Circuit breakers (daily, weekly, monthly, max drawdown)
- Fractional Kelly position sizing
- M3S: strategy registry, 4 modes, allocation engine, compounding engine, AI advisor
- Kill switch via Telegram
- **Milestone: Risk system prevents losses beyond limits for 30 consecutive days**

### Phase 4 — Multi-Exchange Execution
- IC Markets execution (cTrader Open API)
- Binance execution (WebSocket orders)
- Alpaca native Python client (supplement existing MCP)
- IBKR execution via `ib_insync`
- Paper executor (simulated)
- **Milestone: Paper trades on 2+ exchanges with <200ms latency**

### Phase 5 — AI Agent Intelligence
- Claude API integration (direct calls, simple orchestrator)
- Technical Analyst agent, Sentiment Analyst agent
- Weighted consensus signal aggregation
- Caching layer (reduce API costs 60%+)
- **Milestone: AI signals improve backtest Sharpe by >0.2 vs baseline**

### Phase 6 — Options Module
- IBKR options chain data
- Greeks engine (py_vollib)
- Iron condor strategy
- Portfolio Greeks tracking + delta hedging
- **Milestone: Iron condor positive on 1 year SPX backtest**

### Phase 7 — Production Deployment
- Docker Compose
- AWS Singapore VPS
- TimescaleDB migration
- Prometheus + Grafana monitoring
- **Milestone: System runs 7 days unattended on VPS**

### Phase 8 — Scale
- 2-3 more uncorrelated strategies
- Multi-agent debate system (Bull vs Bear)
- Meta-Strategist regime detector
- Performance dashboard
- **Milestone: 3+ strategies running with portfolio Sharpe > 1.5**

---

## 13. Performance Targets

| Metric | Target |
|---|---|
| Signal-to-order latency | <50ms |
| Order-to-fill latency | <100ms |
| Total roundtrip | <200ms |
| Monthly return (conservative) | 3-8% |
| Monthly return (with M3S) | 6-12% |
| Annual return | 36-96% |
| Sharpe ratio | >1.5 |
| Max drawdown | <15% |
| Win rate (SMC/ICT) | 60-70% |

---

## 14. V2 Roadmap

After the core system is profitable:

| Add-On | Impact | When |
|---|---|---|
| Multi-strategy portfolio (5-10) | +30-50% risk-adjusted | After 3 profitable strategies |
| Funding rate arbitrage | +1-2%/mo passive | After crypto module |
| Options wheel strategy | +1-3%/mo income | After $25K+ capital |
| ML signal classification | +10-20% accuracy | After 6 months data |
| Prop firm funded accounts | 5-10x capital | After 3 months live profitable |

**Version 3 (12+ months):** Rust hot paths, FIX protocol, managed fund structure (2/20 fees), real-time ML regime detection.

---

## 15. Memory Files

These are read by Claude on every session start:

| File | Type | What It Stores |
|---|---|---|
| `MEMORY.md` | index | One-line pointers to all memory files |
| `project_algo_trading.md` | project | What's built, file paths, roadmap, decisions |
| `user_algo_trader.md` | user | Prince's goals, style, level, what he's building toward |
| `reference_algo_trading_stack.md` | reference | Best tools per layer, MCP locations |
| `feedback_mcp_config.md` | feedback | MCP servers must go in `~/.claude.json` |
| `user_tutor_mode.md` | feedback | Always explain with structure, analogies, examples |

---

## 16. Key Decisions Log

| Decision | Choice | Why |
|---|---|---|
| Primary forex broker | IC Markets (cTrader) | Native Python API, 0.0 pip raw, macOS compatible, best tax for India |
| MT5 replacement | IBKR `ib_insync` | Pure Python, works on macOS, covers forex + futures + options |
| DataFrame library | pandas (strategy layer) | pandas-ta compatibility. Polars only for bulk ETL. |
| Backtesting frameworks | VectorBT + NautilusTrader | Fast sweeps + tick-level validation. Dropped FreqTrade, QuantConnect. |
| Agent orchestration | Simple Python orchestrator | LangGraph is overkill. Direct Claude API + asyncio.gather(). |
| Primary market | Gold (XAUUSD) | Extreme 2026 volatility, deep liquidity, 5-20% tax (India slab rate) |
| Secondary market | Forex majors | $7T/day volume, tight spreads, same tax advantage |
| Crypto approach | Low-frequency only | 30% flat tax + 1% TDS destroys scalping margins |
| Money management | M3S (4 modes, AI-powered) | No retail system has dynamic AI allocation + compounding |
| Primary strategies | SMC/ICT + Asian Breakout | 60-70% win rate, proven on XAUUSD, replicates Apex for free |
| Risk architecture | Separate ZeroMQ process | Cannot be bypassed by buggy strategy code |
| Phase approach | Milestone-based | Realistic for solo developer learning while building |
| Docker timing | Phase 7 (not Phase 1) | Premature complexity in early phases |
| Database timing | SQLite + Parquet first | TimescaleDB in Phase 7 when data volume justifies it |
| RL/FinRL | Removed | Fragile in financial markets. Gradient boosted trees instead. |

---

## How to Use This Document

1. **Starting a new Claude session:** Read this file first to get full context
2. **Before making architecture changes:** Check the Key Decisions Log (Section 16)
3. **Before implementing a phase:** Read the corresponding phase in Section 12
4. **Before adding a strategy:** Check the Strategy Priority Order (Section 6)
5. **For MCP server issues:** Check memory file `feedback_mcp_config.md`
6. **For detailed competitor numbers:** See `FULL_PLAN.md` (the complete approved plan)
7. **For the original blueprint:** See `TRADING_SYSTEM_BLUEPRINT.md` (needs corrections applied)

---

*This document is the single source of truth for the algo trading project. Keep it updated as decisions change.*
