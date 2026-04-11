# ARCHITECTURE.md -- Module Registry & Data Flow

**Auto-maintained by Claude. Updated after every file create/modify in src/.**

---

## Module Registry

### MCP Servers (External Interfaces -- Node.js)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Alpaca MCP Server | `alpaca/server.js` | `@alpacahq/alpaca-trade-api`, `@modelcontextprotocol/sdk` | Claude (via MCP) | ✅ working |
| Alpaca Connection | `alpaca/connection.js` | `@alpacahq/alpaca-trade-api`, `dotenv` | `alpaca/server.js` | ✅ working |
| Alpaca Trade Utils | `alpaca/trade.js` | `alpaca/connection.js` | `alpaca/server.js` | ✅ working |
| TradingView MCP Server | `tradingview-mcp-jackson/src/server.js` | CDP (port 9222), Express | Claude (via MCP) | ✅ working |

### Utilities (Python)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| PDF Generator | `generate_pdf.py` | `fpdf2` | Manual run | ✅ working |

### Data Layer (Python -- src/data/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Binance WebSocket Feed | `src/data/feeds/binance_ws.py` | `asyncio`, `orjson`, `websockets`, `certifi` | Candle Builder | ✅ working |
| Alpaca WebSocket Feed | `src/data/feeds/alpaca_ws.py` | `alpaca-trade-api`, `asyncio` | Candle Builder | 📋 planned |
| IBKR Data Feed | `src/data/feeds/ibkr_feed.py` | `ib_insync` | Candle Builder | 📋 planned |
| IC Markets Feed | `src/data/feeds/icmarkets_feed.py` | cTrader Open API | Candle Builder | 📋 planned |
| Deribit WebSocket Feed | `src/data/feeds/deribit_ws.py` | `asyncio`, `websockets` | Candle Builder | 📋 planned |
| Candle Builder | `src/data/candle_builder.py` | Data Feeds | Feature Engine, Strategies | ✅ working |
| Feature Engine | `src/data/feature_engine.py` | `ta`, `pandas` | Strategies | ✅ working |
| Order Book Processor | `src/data/order_book.py` | Binance WS | Ultra Scalp Strategies | 📋 planned |
| Historical Downloader | `src/data/downloader.py` | `httpx`, Exchange APIs | Backtesting | 📋 planned |

### Strategy Layer (Python -- src/strategies/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Base Strategy Interface | `src/strategies/base.py` | -- | All strategies | 📋 planned |
| EMA Crossover Scalp | `src/strategies/scalping/ema_crossover.py` | Feature Engine, `pandas-ta` | Signal Aggregator | 📋 planned |
| BB Squeeze | `src/strategies/scalping/bb_squeeze.py` | Feature Engine | Signal Aggregator | 📋 planned |
| Market Structure Break | `src/strategies/scalping/structure_break.py` | Feature Engine | Signal Aggregator | 📋 planned |
| VWAP Reversion | `src/strategies/ultra_scalp/vwap_reversion.py` | Order Book, Feature Engine | Signal Aggregator | 📋 planned |
| Order Book Imbalance | `src/strategies/ultra_scalp/orderbook_imbalance.py` | Order Book | Signal Aggregator | 📋 planned |
| CVD Divergence | `src/strategies/ultra_scalp/cvd_divergence.py` | Order Book | Signal Aggregator | 📋 planned |
| Supply/Demand Zones | `src/strategies/day_trading/supply_demand.py` | Feature Engine | Signal Aggregator | 📋 planned |
| Pairs Trading | `src/strategies/day_trading/pairs.py` | `pykalman`, `statsmodels` | Signal Aggregator | 📋 planned |
| Momentum | `src/strategies/day_trading/momentum.py` | Feature Engine | Signal Aggregator | 📋 planned |
| Iron Condor | `src/strategies/options/iron_condor.py` | Greeks Engine, IBKR | Signal Aggregator | 📋 planned |
| Credit Spread | `src/strategies/options/credit_spread.py` | Greeks Engine, IBKR | Signal Aggregator | 📋 planned |
| Vol Surface | `src/strategies/options/vol_surface.py` | `quantlib-python` | Options Strategies | 📋 planned |

### Execution Layer (Python -- src/execution/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Base Executor | `src/execution/base.py` | -- | All executors | 📋 planned |
| Binance Executor | `src/execution/binance_executor.py` | `ccxt[async]`, Binance WS | Order Manager | 📋 planned |
| Alpaca Executor | `src/execution/alpaca_executor.py` | `alpaca-trade-api` | Order Manager | 📋 planned |
| IBKR Executor | `src/execution/ibkr_executor.py` | `ib_insync` | Order Manager | 📋 planned |
| IC Markets Executor | `src/execution/icmarkets_executor.py` | cTrader Open API | Order Manager | 📋 planned |
| Deribit Executor | `src/execution/deribit_executor.py` | Deribit WS | Order Manager | 📋 planned |
| Paper Executor | `src/execution/paper_executor.py` | -- | Order Manager | 📋 planned |
| Order Manager | `src/execution/order_manager.py` | All Executors | Risk Manager | 📋 planned |

### Risk Layer (Python -- src/risk/ -- SEPARATE PROCESS via ZeroMQ)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Risk Server | `src/risk/risk_server.py` | `pyzmq` | All trading components | 📋 planned |
| Pre-Trade Checks | `src/risk/pre_trade.py` | Portfolio Tracker | Risk Server | 📋 planned |
| Circuit Breaker | `src/risk/circuit_breaker.py` | Trade Log (SQLite) | Risk Server | 📋 planned |
| Position Sizer | `src/risk/position_sizer.py` | Kelly criterion math | Risk Server | 📋 planned |
| Portfolio Heat | `src/risk/portfolio_heat.py` | Portfolio Tracker | Risk Server | 📋 planned |
| Correlation Limits | `src/risk/correlation.py` | `pandas`, `numpy` | Risk Server | 📋 planned |
| Greeks Risk | `src/risk/greeks_risk.py` | `py_vollib` | Risk Server | 📋 planned |
| Stress Test | `src/risk/stress_test.py` | Portfolio Tracker | Risk Server | 📋 planned |

### M3S -- Master Money Management (Python -- src/m3s/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Strategy Registry | `src/m3s/strategy_registry.py` | -- | Allocator, AI Advisor | 📋 planned |
| Mode Manager | `src/m3s/modes.py` | -- | Allocator, Compounding | 📋 planned |
| Capital Allocator | `src/m3s/allocator.py` | Registry, Modes, `numpy` | Main Loop | 📋 planned |
| Compounding Engine | `src/m3s/compounding.py` | Portfolio Tracker, Modes | Main Loop | 📋 planned |
| AI Advisor | `src/m3s/ai_advisor.py` | `anthropic`, All M3S modules | Mode Manager, Allocator | 📋 planned |
| Mode Switch | `src/m3s/mode_switch.py` | Telegram, AI Advisor | Mode Manager | 📋 planned |

### AI Agents (Python -- src/agents/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Orchestrator | `src/agents/orchestrator.py` | `anthropic`, `asyncio` | Signal Aggregator | 📋 planned |
| Technical Analyst | `src/agents/technical_analyst.py` | TradingView MCP, `anthropic` | Orchestrator | 📋 planned |
| Sentiment Analyst | `src/agents/sentiment_analyst.py` | Alpaca News API, `anthropic` | Orchestrator | 📋 planned |
| Order Flow Agent | `src/agents/orderflow_agent.py` | Order Book data, `anthropic` | Orchestrator | 📋 planned |
| Bull vs Bear Debate | `src/agents/debate.py` | `anthropic` | Orchestrator | 📋 planned |
| Meta-Strategist | `src/agents/meta_strategist.py` | All agents, `anthropic` | Orchestrator | 📋 planned |

### Portfolio & Backtest (Python -- src/portfolio/, src/backtest/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Portfolio Tracker | `src/portfolio/tracker.py` | SQLite, Redis | Risk, M3S, Dashboard | 📋 planned |
| Greeks Tracker | `src/portfolio/greeks_tracker.py` | `py_vollib` | Risk, Dashboard | 📋 planned |
| Rebalancer | `src/portfolio/rebalancer.py` | M3S Allocator | Main Loop | 📋 planned |
| VectorBT Runner | `src/backtest/vectorbt_runner.py` | `vectorbt`, `pandas` | Research | 📋 planned |
| Nautilus Runner | `src/backtest/nautilus_runner.py` | `nautilus_trader` | Validation | 📋 planned |
| Walk-Forward | `src/backtest/walk_forward.py` | VectorBT Runner | Research | 📋 planned |
| Backtest Report | `src/backtest/report.py` | `pandas`, `matplotlib` | Research | 📋 planned |

### Utilities (Python -- src/utils/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Config Loader | `src/utils/config.py` | `tomllib` | All modules | ✅ working |
| Logger | `src/utils/logger.py` | `structlog`, `orjson` | All modules | ✅ working |
| Telegram Bot | `src/utils/telegram.py` | `python-telegram-bot` | Risk, M3S, Alerts | 📋 planned |
| Metrics | `src/utils/metrics.py` | `prometheus-client` | Monitoring (Phase 7) | 📋 planned |
| Types | `src/utils/types.py` | `msgspec` | All modules | ✅ working |
| Storage (SQLite+Parquet+Redis) | `src/data/storage.py` | `sqlite3`, `pyarrow`, `redis` | All modules | ✅ working |
| Main Entry Point | `src/main.py` | All data layer modules | -- | ✅ working |

---

## Data Flow

### Live Trading Path
```
Exchange WS (Binance/IC Markets/Alpaca/IBKR)
  --> Data Ingest (asyncio + uvloop)
    --> Candle Builder (OHLCV from ticks)
      --> Feature Engine (pandas-ta indicators)
        --> Strategy Modules (on_candle -> Signal)
          --> AI Agent Layer (Claude consensus)
            --> Signal Aggregator (weighted vote)
              --> Risk Manager [SEPARATE ZeroMQ PROCESS]
                --> M3S Allocator (position sizing, mode-aware)
                  --> Execution Engine (smart order routing)
                    --> Portfolio Tracker (P&L update)
                      --> Trade Log (SQLite) + Redis (hot state)
```

### Backtest Path
```
Historical Data (Parquet files)
  --> VectorBT / NautilusTrader
    --> Same Strategy.on_candle() code
      --> Simulated fills
        --> Performance Metrics (Sharpe, drawdown, win rate)
          --> Backtest Report
```

### M3S Decision Loop
```
Portfolio Tracker (P&L, drawdown, Sharpe)
  --> AI Advisor (Claude, every 30 min)
    --> Mode Decision (Fortress/Balanced/Assault/Adaptive)
      --> Capital Allocation (Kelly-weighted, correlation-aware)
        --> Compounding Engine (base capital + profit pool)
          --> Strategy Registry (enable/disable per mode)
            --> Telegram Notification
```

### Risk Circuit Breaker Path
```
Trade Fill Event
  --> Portfolio Tracker update
    --> Risk Server checks:
        Daily P&L > limit?  --> HALT trading
        Weekly DD > limit?  --> REDUCE position sizes
        Monthly DD > limit? --> HALT + mandatory review
        Max DD breached?    --> EMERGENCY close all
    --> Telegram alert on any trigger
```

---

## Shared State

### Redis Keys

| Key/Pattern | Set By | Read By | Type |
|---|---|---|---|
| `candle:{symbol}:{tf}` | Candle Builder | Strategies, Feature Engine | Hash (OHLCV) |
| `indicators:{symbol}:{tf}` | Feature Engine | Strategies, Agents | Hash (indicator values) |
| `orderbook:{symbol}` | Order Book Processor | Ultra Scalp Strategies | Sorted Set |
| `position:{symbol}` | Portfolio Tracker | Risk Manager, M3S | Hash |
| `portfolio:heat` | Portfolio Heat Monitor | Risk Server, M3S | String (float) |
| `portfolio:equity` | Portfolio Tracker | M3S, Dashboard | String (float) |
| `m3s:mode` | M3S Mode Manager | All Strategies, Risk | String (enum) |
| `m3s:allocation` | M3S Allocator | Strategies, Execution | Hash |

### ZeroMQ Channels

| Channel | Publisher | Subscriber | Type |
|---|---|---|---|
| `tcp://*:5555` (risk requests) | Trading Process | Risk Server | REQ/REP |
| `tcp://*:5556` (risk events) | Risk Server | Trading Process, Telegram | PUB/SUB |
| `tcp://*:5557` (kill switch) | Telegram Bot / Manual | Risk Server, Trading Process | PUB/SUB |

### SQLite Tables (Phase 1-3)

| Table | Written By | Read By | Type |
|---|---|---|---|
| `trades` | Execution Engine | Portfolio Tracker, Backtest Report | Trade log |
| `signals` | Strategy Modules | Backtest Report, Research | Signal log |
| `risk_events` | Risk Server | Dashboard, Research | Circuit breaker events |
| `m3s_decisions` | AI Advisor | Dashboard, Research | Mode/allocation history |
| `daily_pnl` | Portfolio Tracker | M3S, Circuit Breaker | Daily P&L summary |

### Parquet Files (data/)

| Path Pattern | Written By | Read By | Type |
|---|---|---|---|
| `data/historical/{symbol}_{tf}.parquet` | Downloader | VectorBT, NautilusTrader | OHLCV bars |
| `data/backtest_results/{strategy}_{date}.parquet` | Backtest Runner | Report Generator | Backtest output |

---

## Config Dependencies

| Config File | Used By | Critical Settings |
|---|---|---|
| `config/settings.toml` | All modules | `mode`, `log_level`, `telegram_token` |
| `config/strategies.toml` | Strategies, M3S Registry | Strategy params, risk profiles (SAFE/MODERATE/AGGRESSIVE) |
| `config/risk.toml` | Risk Server, Circuit Breaker | Max DD, daily limits, Kelly fraction per mode |
| `config/exchanges.toml` | Execution Layer | API keys, endpoints, paper/live flag (GITIGNORED) |
| `config/logging.toml` | Logger | Log format, output paths, structlog config |
| `~/.claude.json` | Claude MCP | MCP server paths, Alpaca env vars |

---

## Known Gotchas

| Date | Description |
|---|---|
| 2026-04-11 | `MetaTrader5` PyPI package is Windows-only C++ -- does NOT work on macOS. Use IC Markets cTrader + IBKR `ib_insync` instead. |
| 2026-04-11 | `pandas-ta` is no longer maintained/available for Python 3.11. Replaced with `ta` library (same indicators, different API). Feature engine updated. |
| 2026-04-11 | macOS Python 3.11 has no default SSL CA bundle (`ssl.get_default_verify_paths().cafile` is None). WebSocket clients must pass `ssl_context` with `certifi.where()`. |
| 2026-04-11 | India crypto tax: 30% flat + 1% TDS per transaction destroys high-frequency scalping margins. Focus scalping on forex/gold (IC Markets, slab rate 5-20%). |
| 2026-04-11 | fpdf2 only supports latin-1 encoding -- Unicode box-drawing chars and em dashes must be replaced with ASCII before PDF generation. |
| 2026-04-11 | Alpaca MCP API keys are stored as env vars in `~/.claude.json` -- never commit to git. |
