# Algo Trading System

A modular, event-driven algorithmic trading system for systematic trading at retail scale. Same `BaseStrategy.on_candle() → Signal | None` code path in backtest and live. Risk management runs as a separate ZeroMQ process and cannot be bypassed.

**Current phase:** Phase 3b part 2 — M3S construction (AI compounding system). See [ROADMAP.md](ROADMAP.md).

**What's built:** Data pipeline (Binance WS → candles → indicators → storage), event-driven backtest engine with 14 chart renderers and 7 validation protocols, 3-strategy portfolio (bb_rsi_mr_opt / donchian_ensemble_adx / vol_momentum, backtest Sharpe 2.318), adaptive risk manager with 4 modes + per-strategy profiles, paper trading deployed via launchd with watchdog + pre-flight + health checks, 289-test correctness suite including bit-exact TradingView cross-validation.

---

## Project Source of Truth

Each project concern lives in exactly one file. Redundancy causes drift.

| Concern | File |
|---|---|
| Phase status, milestones, next action | [ROADMAP.md](ROADMAP.md) |
| Current session + recent history | [STATE.md](STATE.md) |
| Historical sessions (8-17) | [SESSIONS_ARCHIVE.md](SESSIONS_ARCHIVE.md) |
| Live module registry + data flow + known gotchas | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Decision rationale + research | [MASTER_PLAN.md](MASTER_PLAN.md) |
| Original v1.1 blueprint (frozen) | [BLUEPRINT.md](BLUEPRINT.md) |
| M3S detailed spec | [docs/M3S_SPEC.md](docs/M3S_SPEC.md) |
| 7-stage strategy dev process | [STRATEGY_DEVELOPMENT_PROCESS.md](STRATEGY_DEVELOPMENT_PROCESS.md) |
| Claude instructions + doc hygiene rules | [CLAUDE.md](CLAUDE.md) |

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run a backtest (produces SQLite row + JSON report + HTML report)
python3 -m scripts.backtest run sma_crossover --symbol BTCUSDT --tf 1h --days 365

# Validate a strategy
python3 -m scripts.backtest validate bb_rsi_mr --tier lite

# Import a strategy from any format
python3 -m scripts.import_strategy import --format natural --describe "Buy when 9 EMA crosses 21 EMA"

# Launch interactive dashboard
python3 -m scripts.dashboard

# Run live data pipeline
python3 -m src.main
```

---

## Directory Structure

```
algo-trading/
├── src/
│   ├── main.py                    # Entry point -- TradingEngine
│   ├── config.py                  # TOML config loader (msgspec)
│   ├── data/
│   │   ├── feed.py                # Binance WebSocket feed
│   │   ├── candle_builder.py      # Tick -> OHLCV candle aggregation
│   │   ├── feature_engine.py      # Technical indicators (ta library)
│   │   ├── storage.py             # SQLite + Parquet storage
│   │   └── downloader.py          # Historical data downloader (Binance)
│   ├── strategies/
│   │   ├── base.py                # BaseStrategy interface
│   │   ├── router.py              # StrategyRouter -- dispatches candles
│   │   └── day_trading/
│   │       └── bb_rsi_mr.py       # BB+RSI Mean Reversion (validated)
│   ├── backtest/
│   │   ├── engine.py              # Event-driven backtest engine
│   │   ├── metrics.py             # compute_metrics() -- single source of truth
│   │   ├── result_store.py        # Atomic triple output (DB + JSON + HTML)
│   │   ├── charts.py              # 14 Plotly chart renderers
│   │   ├── report.py              # Jinja2 HTML report generator
│   │   ├── protocols.py           # 7 test protocols (smoke, MC, crash stress...)
│   │   ├── validator.py           # Tiered validation (lite/standard/intense/research)
│   │   ├── walk_forward.py        # Walk-forward optimization
│   │   └── quantstats_bridge.py   # QuantStats tearsheet integration
│   ├── research/
│   │   ├── strategy_ir.py         # Strategy YAML IR schema
│   │   ├── codegen.py             # IR -> Python code generator
│   │   ├── catalog.py             # Strategy catalog (25+ strategies)
│   │   └── parsers/               # 6 format parsers
│   │       ├── pine_parser.py     # Pine Script v4/v5
│   │       ├── mql_parser.py      # MQL4/MQL5
│   │       ├── natural_language.py # Natural language descriptions
│   │       ├── webhook_parser.py  # TV alerts, Telegram signals
│   │       ├── raw_rules.py       # YAML/JSON rules
│   │       └── python_framework_parser.py  # Freqtrade, Backtrader, etc.
│   ├── execution/
│   │   ├── base.py                # Executor interface
│   │   └── paper_executor.py      # Paper trading executor
│   └── dashboard/
│       └── app.py                 # Streamlit 5-page dashboard
├── scripts/
│   ├── backtest.py                # Unified backtest CLI
│   ├── import_strategy.py         # Multi-format strategy import CLI
│   ├── dashboard.py               # Streamlit launcher
│   └── run_verification.py        # Engine verification suite
├── config/
│   ├── settings.toml              # Main config
│   ├── strategies.toml            # Strategy parameters
│   ├── catalog.toml               # Strategy catalog
│   ├── crash_events.toml          # 6 crypto crash events
│   └── report_template.html       # Jinja2 HTML template
├── tests/                         # Unit + integration tests
├── research/                      # Strategy research documents
├── reports/                       # Generated backtest reports (gitignored)
├── alpaca/                        # Alpaca MCP server (US equities)
├── tradingview-mcp-jackson/       # TradingView MCP server
├── ARCHITECTURE.md                # Module registry + data flow
├── STATE.md                       # Session continuity tracker
├── MASTER_PLAN.md                 # Full project context
└── CLAUDE.md                      # Claude instructions
```

---

## Architecture

```
Data Feed (Binance WS) -> Candle Builder -> Feature Engine -> Strategy -> Backtest Engine
                                                                             |
                                                                 compute_metrics() [single source of truth]
                                                                             |
                                                                 ResultStore.save_run() -> DB + JSON + HTML
```

The backtest engine is event-driven: the same `BaseStrategy.on_candle() -> Signal | None` code path runs in both backtest and live mode. `compute_metrics()` is the single source of truth for all performance statistics -- every output format (CLI, JSON, HTML, dashboard) reads from it.

---

## Strategy Ingestion Pipeline

```
Any Format (Pine Script, MQL4/5, natural language, webhooks, Python frameworks, raw YAML)
    -> Parser -> Strategy YAML IR -> Code Generator -> BaseStrategy subclass -> Backtest
```

Six parsers normalize strategies from different sources into a common YAML intermediate representation. The code generator then produces a Python class that subclasses `BaseStrategy`, ready to backtest or deploy.

---

## MCP Servers

Both servers are registered in `~/.claude.json` under `mcpServers`. Claude picks them up on startup.

| Server | Config Key | Entry Point | Purpose |
|---|---|---|---|
| Alpaca | `"alpaca"` | `alpaca/server.js` | Place/manage trades on Alpaca paper/live |
| TradingView | `"tradingview"` | `tradingview-mcp-jackson/src/server.js` | Read charts, indicators, Pine Script |

### Alpaca MCP -- Available Tools

| Tool | What It Does |
|---|---|
| `get_account` | Cash, buying power, portfolio value, status |
| `place_order` | Buy/sell -- market, limit, stop, stop_limit |
| `get_positions` | All open positions with P&L |
| `close_position` | Liquidate a position by symbol |
| `get_orders` | List open/closed orders |
| `cancel_order` | Cancel an order by ID |
| `get_quote` | Latest price, bid/ask, daily OHLCV |

---

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the authoritative phase table and next action. No phase status is duplicated here — all status lives in that one file.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Data classes | msgspec |
| Logging | structlog |
| Indicators | ta library |
| Storage | SQLite + Parquet |
| Charts | Plotly |
| Reports | Jinja2 HTML templates |
| Dashboard | Streamlit |
| Market data | Binance API (WebSocket + REST) |
| Config | TOML |

---

*Last updated: April 2026*
