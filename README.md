# Algo Trading System

A modular, event-driven algorithmic trading system for systematic trading at retail scale. Same `BaseStrategy.on_features() → Signal | None` code path in backtest and live. Risk management runs as a separate ZeroMQ process and cannot be bypassed.

**Current phase:** Phase G Gold Leveraged Stack — G.0 through G.2 engineering complete on branch `feat/gold-refactor`; merge to main scheduled 2026-04-17 after crypto Day 4 sprint cron. Main branch is running 3b-2 M3S shadow + 3c meta-label shadow. See [ROADMAP.md](ROADMAP.md).

**What's built on main (crypto stack):** Data pipeline (Binance WS → candles → indicators → storage), event-driven backtest engine with 14 chart renderers and 7 validation protocols, 3-strategy portfolio (bb_rsi_mr_opt / donchian_ensemble_adx / vol_momentum, backtest Sharpe 2.318), adaptive risk manager with 4 modes + per-strategy profiles, paper trading deployed via launchd with watchdog + pre-flight + health checks, M3S (Master Money Management System) with 10 sub-phases in shadow mode, Phase 3c meta-labeling (LightGBM + LR) also in shadow mode, and a 734-test correctness suite including bit-exact TradingView cross-validation.

**What's built on `feat/gold-refactor` (gold stack, pre-merge):** All of the above plus a separate leveraged backtest engine for XAUUSD with CFD-accurate margin accounting, a two-book architecture (institutional Tiers 1-4 + aggressive Tier 5), Brownian-bridge intrabar path reconstruction, IC Markets cost model (0.13 pip spread + $3/lot/side commission), M3S `request_leverage` policy API with 5 reason codes + geometric-mean blend in `risk_scalar` for leverage > 1×, inline leverage gates for in-process fast-path, RCU versioned portfolio view, SQLite `leverage_grants` store, and **962 tests passing**. **Two diversified institutional strategies**: `donchian_gold` (tuned to +38% / 12% DD / Calmar 1.675 / Sharpe 1.275) and `vol_momentum_gold` (tuned to +20% / 8% DD / Calmar 1.365 / Sharpe 1.103, correlation with donchian_gold = +0.23). **Combined two-strategy institutional book on 2yr XAUUSD 1h: +65.23% total return / 188 trades / 0 broker stop-outs.** `LeverageBudgetAllocator` allocates aggregate leverage across tiers via floor + dynamic Sharpe-weighted pool. `ParquetReplayFeed` + `ShadowOrchestrator` run strategies against historical data chronologically as if live (matches BinanceWebSocketFeed contract — drop-in swap to ICMarketsFeed post-KYC). IC Markets cTrader feed + executor + OAuth helper built, awaiting Spotware KYC approval. Three Tier 5 aggressive strategies (`candle_burst_hunter`, `news_spike_fade`, `hedged_structure_play`) are **infrastructure-complete but NOT alpha-ready** — correct state machines, risk management, broker stop-out detection all work, but every tested entry signal still wipes the sub-book. Full alpha research deferred to a dedicated pass.

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
│   │   ├── feeds/binance_ws.py         # Binance WebSocket feed
│   │   ├── feeds/icmarkets_feed.py     # IC Markets cTrader Open API feed (Phase 4 skeleton)
│   │   ├── candle_builder.py           # Tick -> OHLCV candle aggregation
│   │   ├── feature_engine.py           # Technical indicators (ta library)
│   │   ├── storage.py                  # SQLite + Parquet storage
│   │   └── downloader.py               # Historical data downloader (Binance)
│   ├── strategies/
│   │   ├── base.py                # BaseStrategy interface + leverage_range schema (G.0c)
│   │   ├── router.py              # StrategyRouter -- dispatches candles
│   │   ├── day_trading/
│   │   │   └── bb_rsi_mr.py       # BB+RSI Mean Reversion (crypto, validated)
│   │   ├── trend_following/
│   │   │   ├── donchian_ensemble.py    # Donchian channel ensemble (crypto)
│   │   │   └── donchian_gold.py        # Gold-tuned Donchian with session filter (G.2d)
│   │   ├── momentum/vol_momentum.py    # Vol-scaled momentum (crypto)
│   │   ├── carry/funding_mean_reversion.py # Funding-rate MR (Session 22)
│   │   └── aggressive/                 # Tier 5 infrastructure (NOT alpha-ready)
│   │       ├── candle_burst_hunter.py
│   │       ├── news_spike_fade.py
│   │       └── hedged_structure_play.py
│   ├── backtest/
│   │   ├── engine.py              # Event-driven backtest engine (crypto, TV-bit-exact)
│   │   ├── leveraged_engine.py    # Separate leveraged engine for gold (G.2a.4)
│   │   ├── book.py                # LeveragedPosition + Book (CFD margin model, G.2a)
│   │   ├── costs.py               # IC Markets spread + commission (G.2a.2)
│   │   ├── path.py                # Brownian bridge intrabar path (G.2a.3)
│   │   ├── structure_levels.py    # PDH/PDL, round numbers, Fib retraces (G.2e)
│   │   ├── metrics.py             # compute_metrics() -- single source of truth
│   │   ├── result_store.py        # Atomic triple output (DB + JSON + HTML)
│   │   ├── charts.py              # 14 Plotly chart renderers
│   │   ├── report.py              # Jinja2 HTML report generator
│   │   ├── protocols.py           # 7 test protocols (smoke, MC, crash stress...)
│   │   ├── validator.py           # Tiered validation (lite/standard/intense/research)
│   │   ├── walk_forward.py        # Walk-forward optimization
│   │   └── quantstats_bridge.py   # QuantStats tearsheet integration
│   ├── m3s/                       # Master Money Management System (Phase 3b-2)
│   │   ├── compounder.py          # risk_scalar + target_leverage + geometric-mean blend (G.2b)
│   │   ├── hooks.py               # M3S facade + request_leverage API (G.2b)
│   │   ├── allocator.py           # HRP-lite + Ledoit-Wolf shrinkage
│   │   ├── portfolio.py           # PortfolioTracker + rolling stats
│   │   ├── regime.py              # Regime detector + auto mode switching
│   │   ├── evaluation.py          # Purged CV + Bayesian Kelly
│   │   ├── edge_decay.py          # Strategy edge-decay detection
│   │   ├── leverage_grants.py     # SQLite append store for leverage policy decisions (G.2b)
│   │   ├── aggressive_compounder.py # Fixed-% sizing for Tier 5 sub-book (G.2e)
│   │   ├── portfolio_view.py      # RCU versioned snapshot for lock-free reads (G.2c)
│   │   └── signal_filter/         # Phase 3c meta-labeling pipeline
│   ├── risk/                      # ZMQ-isolated risk manager + inline fast path
│   │   ├── manager.py             # 7-check chain + mode resolution
│   │   ├── server.py / client.py  # ZMQ transport
│   │   ├── kelly_sizer.py
│   │   ├── circuit_breakers.py
│   │   ├── kill_switch.py
│   │   ├── fat_finger.py
│   │   └── inline_leverage.py     # In-process fast path for leveraged engine (G.2c)
│   ├── research/
│   │   ├── strategy_ir.py         # Strategy YAML IR schema
│   │   ├── codegen.py             # IR -> Python code generator
│   │   ├── catalog.py             # Strategy catalog (25+ strategies)
│   │   └── parsers/               # 6 format parsers (Pine, MQL, NL, webhook, YAML, Python framework)
│   ├── execution/
│   │   ├── base.py                # Executor interface
│   │   ├── paper_executor.py      # Paper trading executor
│   │   └── icmarkets_executor.py  # IC Markets cTrader executor (Phase 4 skeleton)
│   ├── utils/
│   │   ├── types.py               # Signal, Fill, LeverageGrant (G.2b) + LeverageReasonCode
│   │   ├── instruments.py         # Instrument registry (G.0b) — XAUUSD, tick_size, contract_size
│   │   ├── config.py              # TOML config loader (msgspec)
│   │   └── logger.py              # structlog setup
│   └── dashboard/
│       └── app.py                 # Streamlit 5-page dashboard
├── scripts/
│   ├── backtest.py                # Unified backtest CLI
│   ├── import_strategy.py         # Multi-format strategy import CLI
│   ├── dashboard.py               # Streamlit launcher
│   └── run_verification.py        # Engine verification suite
├── config/
│   ├── settings.toml              # Main config + [m3s_gold] gold bucket (G.2e)
│   ├── strategies.toml            # Strategy parameters
│   ├── risk.toml                  # Risk manager dials + [leverage] gates (G.0c) + [profiles.aggressive_retail] (G.2c)
│   ├── catalog.toml               # Strategy catalog
│   ├── news_calendar.csv          # NFP/FOMC/CPI/ECB windows for spread widening (G.2e)
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

**Crypto stack (main branch):**

```
Binance WS -> Candle Builder -> Feature Engine -> Strategy -> BacktestEngine
                                                                     |
                                                         compute_metrics() [SSOT]
                                                                     |
                                                         ResultStore -> DB + JSON + HTML
```

**Gold stack (`feat/gold-refactor`):**

```
Dukascopy parquet -> FeatureEngine -> Strategy -> LeveragedBacktestEngine
                                                     |
                                         +-----------+-----------+
                                         |                       |
                                        Book (CFD margin)    M3S.request_leverage
                                         |                       |
                                  institutional    aggressive    LeverageGrant ->
                                  sub-book         sub-book      leverage_grants.db
                                         |                       |
                                  BrownianBridge path reconstruction
                                         |
                                  IC Markets cost model (spread + commission)
                                         |
                                  broker stop-out at 50% margin level
                                         |
                                  total_equity curve + metrics
```

The backtest engine is event-driven: the same `BaseStrategy.on_features() -> Signal | None` code path runs in both backtest and live mode. `compute_metrics()` is the single source of truth for performance statistics on the crypto stack; the leveraged engine emits an equivalent metrics dict keyed on institutional / aggressive / total return + max_dd + broker stop-out count.

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
| ML | scikit-learn (LR) + LightGBM (meta-labeling) |
| Storage | SQLite + Parquet |
| Charts | Plotly |
| Reports | Jinja2 HTML templates |
| Dashboard | Streamlit |
| Market data (crypto) | Binance API (WebSocket + REST) |
| Market data (gold) | Dukascopy + IC Markets cTrader (planned, Phase 4 un-deferral) |
| Config | TOML |

---

*Last updated: 2026-04-14 — Phase G.2 complete on feat/gold-refactor, merge scheduled 2026-04-17*
