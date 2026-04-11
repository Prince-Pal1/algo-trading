# Algo Trading System

A modular, professional algorithmic trading system built to trade systematically — like quant hedge funds, at retail scale. The edge is not speed but discipline, backtested strategies, proper risk management, and continuous iteration.

---

## Project Goal

Build an autonomous, multi-strategy trading bot that:
- Uses **TradingView** for chart analysis and signal generation
- Uses **Alpaca** (US equities) and **MetaTrader 5** (forex/futures) for execution
- Employs **multi-agent AI** (Claude) to replicate quant analyst roles
- Runs on a **modular architecture** so each layer can be swapped independently

---

## Directory Structure

```
algo-trading/
├── README.md                          # This file
├── Algo_Trading_Knowledge_Base.pdf    # Full research doc — architecture, HFT vs retail, tech stack
├── generate_pdf.py                    # Regenerates the PDF knowledge base
│
├── alpaca/                            # Alpaca execution layer
│   ├── server.js                      # MCP server — Claude calls this to trade
│   ├── connection.js                  # Alpaca client setup + connection test
│   ├── trade.js                       # placeOrder, getPositions, closePosition, getOrders
│   ├── package.json
│   └── node_modules/
│
└── tradingview-mcp-jackson/           # TradingView analysis layer
    └── src/
        └── server.js                  # MCP server — Claude reads charts via this
```

---

## MCP Servers (Claude Tools)

Both servers are registered in `~/.claude.json` under `mcpServers`. Claude picks them up on startup.

| Server | Config Key | Entry Point | Purpose |
|---|---|---|---|
| Alpaca | `"alpaca"` | `alpaca/server.js` | Place/manage trades on Alpaca paper/live |
| TradingView | `"tradingview"` | `tradingview-mcp-jackson/src/server.js` | Read charts, indicators, Pine Script |

### Alpaca MCP — Available Tools

| Tool | What It Does |
|---|---|
| `get_account` | Cash, buying power, portfolio value, status |
| `place_order` | Buy/sell — market, limit, stop, stop_limit |
| `get_positions` | All open positions with P&L |
| `close_position` | Liquidate a position by symbol |
| `get_orders` | List open/closed orders |
| `cancel_order` | Cancel an order by ID |
| `get_quote` | Latest price, bid/ask, daily OHLCV |

### Alpaca Credentials
- Account type: **Paper trading**
- Base URL: `https://paper-api.alpaca.markets`
- Keys stored as env vars in `~/.claude.json` (never commit keys to git)

---

## System Architecture

```
+----------------------------------------------------------+
|                    TRADING SYSTEM                        |
|                                                          |
|  DATA LAYER        ->  live prices, OHLCV bars, news     |
|       |                                                  |
|  SIGNAL ENGINE     ->  "should I buy or sell?"           |
|       |                                                  |
|  RISK MANAGER      ->  "how much? what is my stop?"      |
|       |                                                  |
|  EXECUTION         ->  place the order (Alpaca / MT5)    |
|       |                                                  |
|  PORTFOLIO TRACKER ->  what do I own, current P&L        |
|       |                                                  |
|  SCHEDULER         ->  run every N min, market hours     |
|       |                                                  |
|  LOGGER            ->  what happened and why             |
|                                                          |
|  BACKTESTER        ->  did this work historically?       |
+----------------------------------------------------------+
```

### Signal Delivery Patterns

| Pattern | How It Works | Best For |
|---|---|---|
| Polling | Ask TradingView MCP every N min for indicator values, evaluate, trade | Learning, simple strategies |
| Webhooks | TradingView alert fires HTTP POST to local server, server trades instantly | Production, speed, reliability |

Currently using **polling**. Will upgrade to webhooks in Phase 3.

---

## Phased Roadmap (Milestone-Based)

### Phase 1 — Data Foundation [NEXT]
- [x] Alpaca MCP server — full trade execution via Claude
- [x] TradingView MCP server — live chart and indicator access via Claude
- [x] Knowledge Base PDF — research doc on algo trading architecture and tech stack
- [ ] Project structure (`pyproject.toml`, venv, TOML configs)
- [ ] Async WebSocket clients (Binance + IC Markets cTrader)
- [ ] Candle builder, indicator engine (pandas-ta on pandas)
- [ ] Redis (hot cache), SQLite (trade log), Parquet (historical bars)
- [ ] structlog + Telegram alerts

### Phase 2 — First Strategy + Backtest + Paper Trade
- [ ] EMA crossover + RSI (system test), then Asian Range Breakout (real strategy)
- [ ] VectorBT backtest on 2+ years of data with walk-forward validation
- [ ] Paper trading mode with P&L tracking
- **Milestone:** Sharpe > 1.0 on 2-year backtest

### Phase 3 — Risk Management + M3S
- [ ] Risk manager as separate ZeroMQ process (circuit breakers, Kelly sizing)
- [ ] **Master Money Management System (M3S)** — 4 modes (Fortress/Balanced/Assault/AI Adaptive), AI-powered capital allocation + compounding
- **Milestone:** Risk system prevents losses beyond limits for 30 consecutive days

### Phase 4 — Multi-Exchange Execution
- [ ] IC Markets (forex/gold), Binance (crypto), IBKR (options/futures)
- **Milestone:** Paper trades on 2+ exchanges with <200ms latency

### Phase 5 — AI Agent Intelligence
- [ ] 6 Claude agents (Technical, Sentiment, Order Flow, Bull/Bear Debate, Meta-Strategist, Execution)
- [ ] Simple Python orchestrator with caching layer (60%+ cost reduction)
- **Milestone:** AI signals improve backtest Sharpe by >0.2

### Phase 6 — Options Module
- [ ] IBKR options, Greeks engine, iron condors, delta hedging

### Phase 7 — Production Deployment
- [ ] Docker Compose, AWS VPS, TimescaleDB migration, Grafana monitoring

### Phase 8 — Scale
- [ ] 3+ uncorrelated strategies (SMC/ICT, Multi-Indicator Fusion, Pairs Trading)
- [ ] Pine Script ingestion pipeline
- **Milestone:** Portfolio Sharpe > 1.5

---

## Tech Stack Decisions

### Execution Platforms

| Platform | Markets | Cost | Status |
|---|---|---|---|
| IC Markets | Forex, Gold, Metals, CFDs | 0.0 pip raw + $3.50/lot | PRIMARY (forex/gold) — Phase 1 |
| Alpaca | US stocks, ETFs | Free API | MCP built |
| Binance | Crypto spot + futures | 0.02/0.04% maker/taker | Phase 4 |
| Interactive Brokers | Options, futures, forex | Commission based | Phase 6 |

> MT5 removed — Windows-only C++, does not work on macOS. IC Markets cTrader + IBKR replace it entirely.

### Backtesting Frameworks

| Framework | Best For | Status |
|---|---|---|
| VectorBT | Fast vectorized parameter sweeps | Recommended |
| NautilusTrader | Tick-level validation + live bridge (Rust core) | Recommended |

### Signal & Intelligence Tools

| Tool | What It Adds |
|---|---|
| TradingView MCP | Live chart state, indicator values, Pine Script |
| pandas-ta | 150+ technical indicators in Python, vectorized |
| TA-Lib | C-backed, very fast, industry standard indicators |
| Alpaca News API | Real-time market news, free with account |
| Claude API | LLM sentiment scoring, multi-agent analyst roles |

### Multi-Agent Framework (Phase 5)

Simple Python orchestrator (direct Claude API calls + `asyncio.gather()`). No LangGraph — overkill for our needs.

```
+------------------+  +-----------------+  +------------------+
|  Technical       |  |  Sentiment      |  |  Order Flow      |
|  Analyst Agent   |  |  Analyst Agent  |  |  Analyst Agent   |
+--------+---------+  +--------+--------+  +--------+---------+
         |                     |                    |
         +---------------------+--------------------+
                               |
                  +------------+------------+
                  | Bull vs Bear Debate     |
                  +------------+------------+
                               |
                  +------------+------------+
                  | Meta-Strategist (Regime)|
                  +------------+------------+
                               |
              +----------------+----------------+
              |   M3S — Master Money Manager   |
              |   (AI Adaptive allocation)     |
              +----------------+----------------+
                               |
                  +------------+------------+
                  |  Execution Agent        |
                  |  (IC Markets/Alpaca/    |
                  |   Binance/IBKR)        |
                  +-------------------------+
```

---

## Key Architectural Decisions

| Decision | Choice | Reason |
|---|---|---|
| Strategy language | Python | Ecosystem, ML libraries, flexibility |
| Primary forex/gold broker | IC Markets (cTrader) | Native Python API, 0.0 pip raw, macOS compatible, best India tax |
| US equities execution | Alpaca | Free API, paper trading, MCP built |
| Options/futures | IBKR (`ib_insync`) | Pure Python, replaces MT5, covers forex+futures+options |
| DataFrames | pandas (primary) | pandas-ta compatibility; Polars only for bulk ETL |
| Signal source (now) | TradingView MCP polling | Easiest to start with |
| Signal source (later) | TradingView webhooks | Faster, production-grade |
| Backtesting | VectorBT + NautilusTrader | Fast sweeps + tick-level validation |
| Agent orchestration | Simple Python orchestrator | LangGraph is overkill; direct Claude API + asyncio |
| Money management | M3S (4 modes, AI-powered) | No retail system has dynamic AI allocation + compounding |
| Database (Phase 1-3) | SQLite + Parquet | TimescaleDB deferred to Phase 7 |

---

## Claude Memory Files

These files live in `~/.claude/projects/-Users-prince/memory/` and give Claude full context across every conversation so you never have to re-explain the project.

| File | Type | What It Stores |
|---|---|---|
| `project_algo_trading.md` | project | Everything built, file paths, full phased roadmap |
| `user_algo_trader.md` | user | Your goals, style, current level, what you're building toward |
| `reference_algo_trading_stack.md` | reference | Best tools per layer — backtesting, execution, signals, MCP locations |
| `feedback_mcp_config.md` | feedback | MCP servers must go in `~/.claude.json`, not settings files |
| `user_tutor_mode.md` | feedback | Always explain with structure, analogies, examples — tutor mode on |

---

## Knowledge Base PDF

Full research document covering:
- The 3 tiers of algo trading (HFT vs Quant Funds vs Retail)
- What HFT firms actually use and why you can't copy it
- What quant hedge funds do that you CAN mimic
- How HNWIs trade
- Best modular architecture
- Full tech stack comparison
- Broker comparison (IC Markets, IBKR, Binance, Alpaca)
- Multi-agent AI trading (2026 cutting edge)
- Phased build order

**File:** `Algo_Trading_Knowledge_Base.pdf`
**Regenerate:** `python3 generate_pdf.py`

---

## Why We Are Building This

The gap between retail and institutional algo trading has never been smaller. The tools exist. What separates profitable traders is:

1. **Strategy quality** — backtested, evidence-based edges
2. **Risk discipline** — survive drawdowns, size correctly
3. **Systematic execution** — no emotions, rules-based
4. **Iteration speed** — measure, learn, improve monthly

Renaissance Technologies returns 66% annually (before fees) — not through faster chips, but through better math on better data. That is the model.

---

*Last updated: April 2026 | Version 1.1 — 17 corrections applied, M3S added, IC Markets as primary forex broker*
