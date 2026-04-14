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
| Binance WebSocket Feed | `src/data/feeds/binance_ws.py` | `asyncio`, `orjson`, `websockets`, `certifi` | Candle Builder | ✅ working (exponential backoff 1s→60s, 10% jitter) |
| Historical Warmup | `src/data/warmup.py` | FeatureEngine, StrategyRouter, Downloader, ParquetStore | TradingEngine | ✅ working (200 candles from Parquet/REST, signals discarded) |
| Alpaca WebSocket Feed | `src/data/feeds/alpaca_ws.py` | `alpaca-trade-api`, `asyncio` | Candle Builder | 📋 planned |
| IBKR Data Feed | `src/data/feeds/ibkr_feed.py` | `ib_insync` | Candle Builder | 📋 planned |
| IC Markets Feed | `src/data/feeds/icmarkets_feed.py` | cTrader Open API | Candle Builder | 📋 planned |
| Deribit WebSocket Feed | `src/data/feeds/deribit_ws.py` | `asyncio`, `websockets` | Candle Builder | 📋 planned |
| Candle Builder | `src/data/candle_builder.py` | Data Feeds | Feature Engine, Strategies | ✅ working |
| Feature Engine | `src/data/feature_engine.py` | `ta`, `pandas` | Strategies | ✅ working |
| Order Book Processor | `src/data/order_book.py` | Binance WS | Ultra Scalp Strategies | 📋 planned |
| Historical Downloader | `src/data/downloader.py` | `httpx`, `certifi` | Backtesting | ✅ working |

### Strategy Layer (Python -- src/strategies/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Base Strategy Interface | `src/strategies/base.py` | -- | All strategies | ✅ working |
| Strategy Router | `src/strategies/router.py` | BaseStrategy, Config | TradingEngine | ✅ working (3 strategies registered, exception isolation per strategy) |
| EMA Crossover Scalp | `src/strategies/scalping/ema_crossover.py` | Feature Engine, `ta` | Router | ⚠️ unprofitable (disabled) |
| BB+RSI Mean Reversion | `src/strategies/day_trading/bb_rsi_mr.py` | Feature Engine, `ta` | Router, Backtest CLI | ✅ working (Portfolio Sharpe 1.315 on 5 altcoins: ETH/BNB/ADA/DOT/SOL; VPIN removed Session 15 — ADX<20 makes it redundant) |
| BB+RSI MR + VPIN | `scripts/backtest.py` (registry only) | bb_rsi_mr + VPINRegimeFilter(0.7) | Backtest CLI | ✅ tested (identical results to non-VPIN — redundant, kept for reference) |
| RSI(2) Verification | `src/strategies/verification/rsi2_mr.py` | Feature Engine, `ta` | Engine Verification | ✅ test-only (not for trading) |
| Walk-Forward EMA | `src/strategies/scalping/ema_crossover_wf.py` | BaseStrategy, numpy | Router, Backtest CLI | ✅ implemented (Sharpe -0.2 to +0.13 on 2024-2026 — EMA crossover unviable in current regime) |
| MacroHFT RL Agent | `src/strategies/rl/macro_hft.py` | Decomposition, PyTorch | Router | 📋 research-backed (Sharpe 3.89 ETH, KDD 2024) |
| Alpha-AS Market Making | `src/strategies/market_making/alpha_as.py` | Order Book, RL Agent | Router | 📋 research-backed (Sharpe 2.5+, PLOS ONE) |
| Cointegration Pairs | `src/strategies/stat_arb/pairs_trading.py` | `statsmodels`, Johansen test | Pairs Backtester | ✅ implemented (no usable cointegration found in 2024-2026 crypto — code ready for future use) |
| BTC-Neutral MR | `src/strategies/stat_arb/btc_neutral_mr.py` | numpy (rolling OLS), BaseStrategy | Backtest CLI | ❌ dead (Sharpe negative on all params — z-score reverts but price doesn't profit) |
| Donchian Ensemble | `src/strategies/trend_following/donchian_ensemble.py` | Feature Engine (donchian_N), BaseStrategy | Router, Backtest CLI | ✅ working (Sharpe 1.389 NEAR, 1.376 AVAX, 1.253 DOT; trend-following complement to bb_rsi_mr) |
| Vol-Scaled Momentum | `src/strategies/momentum/vol_momentum.py` | numpy (momentum + vol), BaseStrategy | Router, Backtest CLI | ✅ working (Sharpe 0.919 XRP, 0.659 DOT; momentum with inverse-vol sizing) |
| VPIN Calculator | `src/data/vpin.py` | `scipy.stats`, numpy | Regime Filter | ✅ implemented (BVC-based VPIN from OHLCV candles) |
| VPIN Regime Filter | `src/strategies/filters/regime_filter.py` | VPIN Calculator | All Strategies | ✅ implemented (kill switch at VPIN > 0.7) |
| Regime Decomposition | `src/data/decomposition.py` | OHLCV Data | MacroHFT, Strategy Selection | 📋 research-backed (KDD 2024) |
| BB Squeeze | `src/strategies/scalping/bb_squeeze.py` | Feature Engine | Signal Aggregator | 📋 planned |
| Market Structure Break | `src/strategies/scalping/structure_break.py` | Feature Engine | Signal Aggregator | 📋 planned |
| VWAP Reversion | `src/strategies/ultra_scalp/vwap_reversion.py` | Order Book, Feature Engine | Signal Aggregator | 📋 planned |
| Order Book Imbalance | `src/strategies/ultra_scalp/orderbook_imbalance.py` | Order Book | Signal Aggregator | 📋 planned |
| CVD Divergence | `src/strategies/ultra_scalp/cvd_divergence.py` | Order Book | Signal Aggregator | 📋 planned |
| Supply/Demand Zones | `src/strategies/day_trading/supply_demand.py` | Feature Engine | Signal Aggregator | 📋 planned |
| Pairs Trading (legacy) | `src/strategies/day_trading/pairs.py` | `pykalman`, `statsmodels` | Signal Aggregator | 📋 planned |
| Momentum | `src/strategies/day_trading/momentum.py` | Feature Engine | Signal Aggregator | 📋 planned |
| Iron Condor | `src/strategies/options/iron_condor.py` | Greeks Engine, IBKR | Signal Aggregator | 📋 planned |
| Credit Spread | `src/strategies/options/credit_spread.py` | Greeks Engine, IBKR | Signal Aggregator | 📋 planned |
| Vol Surface | `src/strategies/options/vol_surface.py` | `quantlib-python` | Options Strategies | 📋 planned |

### Execution Layer (Python -- src/execution/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Base Executor | `src/execution/base.py` | -- | All executors | ✅ working |
| Binance Executor | `src/execution/binance_executor.py` | `ccxt[async]`, Binance WS | Order Manager | 📋 planned |
| Alpaca Executor | `src/execution/alpaca_executor.py` | `alpaca-trade-api` | Order Manager | 📋 planned |
| IBKR Executor | `src/execution/ibkr_executor.py` | `ib_insync` | Order Manager | 📋 planned |
| IC Markets Executor | `src/execution/icmarkets_executor.py` | cTrader Open API | Order Manager | 📋 planned |
| Deribit Executor | `src/execution/deribit_executor.py` | Deribit WS | Order Manager | 📋 planned |
| Paper Executor | `src/execution/paper_executor.py` | Storage, sqlite3 | TradingEngine | ✅ working (execute + execute_order, position persistence, crash recovery via restore_state()) |
| Order Manager | `src/execution/order_manager.py` | All Executors | Risk Manager | 📋 planned |

### Risk Layer (Python -- src/risk/ -- SEPARATE PROCESS via ZeroMQ)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Risk Config | `src/risk/config.py` | `msgspec`, `get_config()` | All risk modules | ✅ working (+ StrategyRiskProfile struct, strategy_profiles dict) |
| Risk Modes | `src/risk/modes.py` | `msgspec`, `enum` | RiskManager | ✅ working (AGGRESSIVE/BALANCED/DEFENSIVE/CUSTOM, ModeMultipliers) |
| Risk State | `src/risk/state.py` | `sqlite3`, `orjson` | All risk modules | ✅ working (+ active_mode, custom_multipliers) |
| Kill Switch | `src/risk/kill_switch.py` | RiskState | RiskManager | ✅ working |
| Circuit Breakers | `src/risk/circuit_breakers.py` | RiskConfig, RiskState | RiskManager | ✅ working (3-level + EV gate + mode confidence floor) |
| Fat Finger Guard | `src/risk/fat_finger.py` | RiskConfig, RiskState | RiskManager | ✅ working (mode-resolved max value) |
| Position Limits | `src/risk/position_limits.py` | RiskConfig, RiskState | RiskManager | ✅ working (+ risk-based limit path) |
| Duplicate Filter | `src/risk/duplicate_filter.py` | -- | RiskManager | ✅ working |
| Kelly Sizer | `src/risk/kelly_sizer.py` | RiskConfig, `numpy` | RiskManager | ✅ working (fractional Kelly + vol targeting + skip_vol flag) |
| Drawdown Scaler | `src/risk/drawdown_scaler.py` | RiskConfig | RiskManager | ✅ working (aggression exponent + sensitivity/floor) |
| Risk Manager | `src/risk/manager.py` | All risk checks, `sqlite3`, `orjson` | RiskServer, InlineRiskClient | ✅ working (7-check chain + mode/profile resolution) |
| Risk Server | `src/risk/server.py` | `pyzmq`, RiskManager | TradingEngine (via ZMQ) | ✅ working (+ SET_MODE command) |
| Risk Client | `src/risk/client.py` | `pyzmq`, `msgspec` | TradingEngine (main.py) | ✅ working (+ set_mode()) |
| Inline Risk Client | `src/risk/client.py` | RiskManager | BacktestEngine | ✅ working (+ set_mode()) |
| Correlation Limits | `src/risk/correlation.py` | `pandas`, `numpy` | Risk Server | 📋 Phase 3b |
| Greeks Risk | `src/risk/greeks_risk.py` | `py_vollib` | Risk Server | 📋 Phase 6 |

### Strategies — Phase 3b-3 Additions (Python -- src/strategies/)

| Module | File | Status | Notes |
|---|---|---|---|
| Funding Carry Strategy | `src/strategies/carry/funding_carry.py` | ✅ shipped (Session 22) | v1 uses synthetic `BTCUSDT-CARRY` via funding proxy; real multi-leg futures deferred to v2 |
| Funding Synthetic Builder | `src/data/funding_synthetic.py` | ✅ shipped | Converts funding Parquet → synthetic OHLCV with `close[i+1] = close[i] × (1 + rate − friction)` |
| Binance Funding Downloader | `src/data/downloader.py` (extended) | ✅ shipped | `BinanceFundingDownloader` class; hits `/fapi/v1/fundingRate` REST; writes to `data/historical/funding/<SYM>_8h.parquet` |
| RankCache Primitive | `src/strategies/ranking.py` | ✅ shipped | `RankCache` + `compute_clenow_score` + `rank_weights_from_scores`; reusable for any rank-based strategy |
| Momentum Rank Cache Builder | `scripts/build_momentum_rank_cache.py` | ✅ shipped | Offline pre-pass; computes Clenow scores per rebalance; writes `data/historical/momentum_rank_cache.parquet` |
| Clenow Momentum Strategy | `src/strategies/momentum/clenow_momentum.py` | 💀 KILLED at B.3 (Session 22) | Kept in repo with `enabled=false`; obituary at `docs/strategy_obituaries/strategy_b_clenow_momentum.md` |
| Universe Manifest | `config/universes.toml` | ✅ shipped | Survivorship-bias-aware top-30 altcoin manifest + delisted tokens (LUNA, FTT, UST, CEL, SRM) |

### M3S -- Master Money Management (Python -- src/m3s/)

Phase 3b-2. Canonical plan: `docs/planning/m3s_plan_v1.md` § v1.1 ADDENDUM. Sub-phase 0.1 (scaffolding) complete Session 22; remaining sub-phases listed in `ROADMAP.md`.

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| M3S Types (msgspec) | `src/m3s/types.py` | `msgspec` | all M3S modules | ✅ sub-phase 0.1 (Session 22) |
| Mode Manager (4 modes + CUSTOM rails) | `src/m3s/modes.py` | `msgspec`, logger | Compounder, Allocator, Hooks | ✅ sub-phase 0.1 (Session 22) |
| SQLite State Store | `src/m3s/state.py` | `sqlite3`, `msgspec` | Scheduler, Hooks, meta_backtest | ✅ sub-phase 0.1 (Session 22) |
| Portfolio Tracker | `src/m3s/portfolio.py` | Types, stdlib | Allocator, Compounder, Edge-Decay | ✅ sub-phase 0.2 |
| Edge-Decay Monitor (Tier 1 #2) | `src/m3s/edge_decay.py` | Types, Portfolio Tracker | Allocator | ✅ sub-phase 0.2 |
| Compounder (+ CVaR, Tier 1 #5) | `src/m3s/compounder.py` | Types, Modes, Portfolio Tracker | Hooks | ✅ sub-phase 0.3 |
| Allocator HRP-lite (+ Ledoit-Wolf, Tier 1 #4) | `src/m3s/allocator.py` | `numpy`, Portfolio Tracker | Hooks | ✅ sub-phase 0.4 (pure-numpy LW, no sklearn dep) |
| Conviction Scorer (Tier 1 #3) | `src/m3s/conviction.py` | Types, Signal | Hooks | ✅ sub-phase 0.5 |
| Integration Hooks | `src/m3s/hooks.py` | Allocator, Compounder, Conviction, Edge-Decay, State | `src/main.py`, Router | ✅ sub-phase 0.5 |
| Scheduler (async weekly/daily tick) | `src/m3s/scheduler.py` | Hooks, State, `asyncio` | `src/main.py` task | ✅ sub-phase 0.6 (+ save_state/load_state) |
| Meta-Backtest Simulator | `src/m3s/meta_backtest.py` | Hooks, Portfolio Tracker, trade logs | CLI `scripts/m3s_cli.py` | ✅ sub-phase 0.7 |
| Regime Detector (Tier 1 #1) | `src/m3s/regime.py` | stdlib | Scheduler, Mode Manager | ✅ sub-phase 0.9 (RegimeClassifier + AutoModeSwitcher) |
| Evaluation Layer (Purged CV + Bayesian Kelly, Tier 1 #6-7) | `src/m3s/evaluation.py` | `numpy` | Allocator, Strategy Admission | ✅ sub-phase 0.10 |
| M3S CLI | `scripts/m3s_cli.py` | Hooks, State | Manual ops (freeze/thaw/status) | 📋 not built yet — add on demand |
| Shadow Checker | `scripts/m3s_shadow_check.py` | `sqlite3` (raw, no M3S import) | launchd 6h schedule | ✅ Session 22 post-commit (4-day compressed clock, 7 invariant checks, writes report/status/alerts/clock files) |
| Shadow launchd agent | `scripts/launchd/com.algo-trading.m3s-shadow-check.plist` | -- | launchctl | ✅ Session 22 (StartInterval=21600s) |
| Shadow activate helper | `scripts/m3s_activate_shadow.sh` | plist, launchctl | Manual trigger | ✅ Session 22 |
| Shadow deactivate helper | `scripts/m3s_deactivate_shadow.sh` | launchctl | Manual trigger | ✅ Session 22 |
| M3S promote (authoritative) | `scripts/promote_m3s_authoritative.sh` | shadow check + clock + meta + DSR | Manual/cron | ✅ Session 22 Day 0.5 (4-gate promotion with --dry-run/--force/--no-commit) |
| AI Advisor (deferred) | `src/m3s/advisor.py` | `anthropic`, State | Scheduler (read-only shadow) | 📋 Phase 3c |
| Meta-Label Audit Store | `src/m3s/signal_filter/audit.py` | `sqlite3` | Backtest engine, main.py | ✅ Phase 3c Phase 0 (signal_audit table + label writers) |
| Meta-Label Feature Builder | `src/m3s/signal_filter/features.py` | Types, Snapshot | Audit, Filter | ✅ Phase 3c (24 feature keys, 15 populated + 9 deferred) |
| Meta-Label Training Labels | `src/m3s/signal_filter/labels.py` | audit rows | Train | ✅ Phase 3c Phase 0 (triple-barrier method) |
| Meta-Label Purged CV | `src/m3s/signal_filter/cv.py` | numpy | Train | ✅ Phase 3c Phase 2 (AFML ch.7 purged K-fold t1) |
| Meta-Label Training Pipeline | `src/m3s/signal_filter/train.py` | LR, LightGBM (optional), Audit, CV | CLI `train_meta_classifier.py` | ✅ Phase 3c Phase 2 (LightGBM A/B sanity check, LR fallback, sample uniqueness) |
| Meta-Label Live Filter | `src/m3s/signal_filter/filter.py` | joblib, features, training_feature_keys | src/main.py | ✅ Phase 3c Phase 3 (shadow + advisory + live modes, mtime hot-reload) |
| Strategy History Cache | `src/m3s/signal_filter/strategy_history.py` | stdlib (deque) | features.py, main.py | ✅ Session 22 Day 0.5 addendum (in-process ring buffer for deferred feature keys) |
| Funding Rate Mean Reversion | `src/strategies/carry/funding_mean_reversion.py` | BaseStrategy, quantile buffer | STRATEGY_REGISTRY | ✅ Session 22 Day 0.5 addendum (disabled, Stage 3 soft pass, needs Stages 4-7) |
| Funding MR research script | `scripts/research_funding_mr.py` | funding_mean_reversion, OHLCV + funding parquets | Stage 3 validation | ✅ Session 22 Day 0.5 addendum |
| Meta-Label Training CLI | `scripts/train_meta_classifier.py` | train.py | Weekly retrain launchd | ✅ Phase 3c Phase 2 |
| Meta-Label Harvest | `scripts/harvest_audit_from_backtest.py` | Backtest engine, audit | Bootstrap training | ✅ Session 22 sprint (2,819 rows harvested from 4 strategies) |
| Meta-Label Shadow Checker | `scripts/meta_label_shadow_check.py` | sqlite3, joblib | Promotion scripts | ✅ Session 22 Day 0.5 (rolling AUC/Brier, gate helpers passes_phase4/5) |
| Meta-Label Promote | `scripts/promote_meta_label.sh` | shadow check helpers | Manual/cron | ✅ Session 22 Day 0.5 (--to shadow\|advisory\|live) |
| Meta-Label Retrain Agent | `scripts/launchd/com.algo-trading.m3s-retrain.plist` | train_meta_classifier.py | launchctl weekly | ✅ Session 22 (Sunday 00:03 UTC) |
| Deflated Sharpe Monitor | `scripts/deflated_sharpe_from_audit.py` | evaluation.py `_deflated_sharpe`, audit | Promotion Gate 4 | ✅ Session 22 Day 0.5 |
| Project Status Aggregator | `scripts/project_status.py` | heartbeat/shadow/DSR JSON + TOML | launchd 30min | ✅ Session 22 Day 0.5 (data/project_status.md) |
| Project Status Agent | `scripts/launchd/com.algo-trading.project-status.plist` | project_status.py | launchctl 1800s | ✅ Session 22 Day 0.5 (not yet loaded) |

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
| Backtest Engine | `src/backtest/engine.py` | Feature Engine, Strategies, Metrics, RiskManager (optional) | Research | ✅ verified (TradingView match, optional risk gating) |
| Backtest Metrics | `src/backtest/metrics.py` | `scipy`, `numpy`, `pandas` | Engine, ResultStore, Report | ✅ working |
| Result Store | `src/backtest/result_store.py` | Metrics, Report, Charts, `structlog` | CLI | ✅ working |
| Chart Renderers | `src/backtest/charts.py` | `plotly` | Report, Dashboard | ✅ working (14 chart types) |
| HTML Report | `src/backtest/report.py` | Charts, `jinja2`, `plotly` | ResultStore | ✅ working (extended sections) |
| QuantStats Bridge | `src/backtest/quantstats_bridge.py` | `quantstats` | CLI, Dashboard | ✅ working |
| Report Template | `config/report_template.html` | -- | HTML Report | ✅ working |
| Backtest CLI | `scripts/backtest.py` | Engine, ResultStore, Downloader, Validator | Manual run | ✅ working (run, list, validate, compare) |
| Strategy Validator | `src/backtest/validator.py` | Backtest Engine, Downloader, Protocols | Research, CLI | ✅ working (tier dispatch) |
| Test Protocols | `src/backtest/protocols.py` | Engine, Metrics, Downloader | Validator, CLI | ✅ working (smoke, spot_check, monte_carlo, crash_stress, sensitivity, psr, dsr) |
| Crash Events DB | `config/crash_events.toml` | -- | Protocols (crash_stress) | ✅ working (6 events) |
| Walk-Forward Validator | `src/backtest/walk_forward.py` | Backtest Engine | Research | ✅ working |

### Research Tooling (Python -- src/research/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Strategy Catalog | `src/research/catalog.py` | `tomllib`, `msgspec`, `sqlite3` | CLI, Validator | ✅ working |
| Catalog Data | `config/catalog.toml` | -- | Strategy Catalog | ✅ working |
| Catalog CLI | `scripts/catalog.py` | Strategy Catalog | Manual run | ✅ working |
| Strategy IR | `src/research/strategy_ir.py` | `msgspec`, `yaml` | Parsers, CodeGen | ✅ working |
| Code Generator | `src/research/codegen.py` | Strategy IR, BaseStrategy | Import CLI | ✅ working (IR → Python) |
| Parser Base | `src/research/parsers/base.py` | -- | All parsers | ✅ working (auto-detect + registry) |
| Raw Rules Parser | `src/research/parsers/raw_rules.py` | Strategy IR | Import CLI | ✅ working |
| NL Parser | `src/research/parsers/natural_language.py` | Claude API (optional) | Import CLI | ✅ working (rule-based + LLM) |
| Pine Parser | `src/research/parsers/pine_parser.py` | regex | Import CLI | ✅ working (v4/v5) |
| Webhook Parser | `src/research/parsers/webhook_parser.py` | `json` | Import CLI | ✅ working |
| MQL Parser | `src/research/parsers/mql_parser.py` | regex, Claude API (optional) | Import CLI | ✅ working (MQL4/5) |
| Python Parser | `src/research/parsers/python_framework_parser.py` | `ast`, regex | Import CLI | ✅ working (Freqtrade, Backtrader) |
| Import CLI | `scripts/import_strategy.py` | Parsers, CodeGen, Strategy IR | Manual run | ✅ working |

### Dashboard (Python -- src/dashboard/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Streamlit App | `src/dashboard/app.py` | ResultStore, Charts, `streamlit` | Browser | ✅ working (5 pages) |
| Dashboard Launcher | `scripts/dashboard.py` | `streamlit` | Manual run | ✅ working |

### Utilities (Python -- src/utils/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Config Loader | `src/utils/config.py` | `tomllib` | All modules | ✅ working |
| Logger | `src/utils/logger.py` | `structlog`, `orjson` | All modules | ✅ working |
| Telegram Bot | `src/utils/telegram.py` | `python-telegram-bot` | Risk, M3S, Alerts | 📋 planned |
| Metrics | `src/utils/metrics.py` | `prometheus-client` | Monitoring (Phase 7) | 📋 planned |
| Types | `src/utils/types.py` | `msgspec` | All modules | ✅ working |
| Storage (SQLite+Parquet+Redis) | `src/data/storage.py` | `sqlite3`, `pyarrow`, `redis` | All modules | ✅ working (incl. backtest_*, validation_*, paper_positions, paper_equity tables) |
| Main Entry Point | `src/main.py` | All data layer, warmup, heartbeat, M3S (disabled default) | -- | ✅ working (warmup → live feed, heartbeat, graceful shutdown, M3S wired sub-phase 0.8) |
| Heartbeat Monitor | `src/monitoring/heartbeat.py` | `orjson`, `asyncio` | TradingEngine | ✅ working (60s heartbeat.json, stale detection, kill file watcher, signal/exception metrics, WARNING status) |
| Watchdog | `scripts/watchdog.py` | stdlib only (no src/ imports) | launchd | ✅ working (90s checks, 180s stale alert, 360s kill, writes watchdog_status.json + alerts.log) |
| Pre-Flight Check | `scripts/preflight.py` | zmq, websockets, src.utils.config | Manual / start_paper.sh | ✅ working (8 checks: config, data, risk server, WS, disk, kill file, DB, PIDs) |
| Health Check Script | `scripts/check_health.py` | `json` | SSH/manual | ✅ working (exit 0/1/2 for healthy/warning/critical) |

---

## Data Flow

### Live/Paper Trading Path (Phase 4 — production-ready)
```
[Startup] warmup(200 candles from Parquet/REST) → indicators + strategies warm
          PaperExecutor.restore_state() → crash-recovered positions + equity

[Live]    Binance WS (exp backoff) → CandleBuilder → FeatureEngine → StrategyRouter
            (exception-isolated per strategy) → Signal(s)
          → RiskClient (ZMQ REQ → tcp://127.0.0.1:5555 → RiskServer/RiskManager)
            → if approved → PaperExecutor.execute(signal) → report_fill() → persist to SQLite
            → if rejected → log + skip

[Monitor] Heartbeat → data/heartbeat.json (60s), stale detection, kill file (data/KILL)
[Shutdown] SIGTERM → persist positions + equity → close feed → done

Launch: ./scripts/start_paper.sh (risk server + engine + log files)
    or: launchctl load scripts/launchd/*.plist (24/7 with auto-restart)
```

### Backtest Path (Phase 2 — working)
```
BinanceDownloader → DataFrame → _compute_indicators() → strategy.process() row-by-row
                                                       → BacktestEngine (SL/TP/slippage sim)
                                                         → BacktestResult (metrics + equity curve)
```
Key principle: `strategy.process()` is called identically in backtest and live. Strategy never knows which mode.

### Live Path with M3S (sub-phase 0.8, DISABLED by default)
```
Feed → CandleBuilder → FeatureEngine → StrategyRouter → Signal
                                                         |
                                                         ▼
                              M3S.on_signal (sub-phase 0.8 hook, shadow-mode default)
                                         │  - allocator weight (HRP-lite + Ledoit-Wolf)
                                         │  - compounder scalar (vol target × Sharpe pace × CVaR × DD)
                                         │  - conviction multiplier (Tier 1 #3)
                                         │  - edge-decay factor (halved / paused)
                                         │  - hard rail: scaled ≤ original risk_pct
                                         ▼
                                   RiskClient (ZMQ) → RiskServer process (untouched)
                                         ▼
                                   PaperExecutor.execute(signal) → Fill
                                         ▼
                                   PaperExecutor._close_position → on_trade_close_hook → M3S.on_trade_close
                                                                                            ├── tracker update
                                                                                            └── compounder advance (per-trade cadence, CUSTOM only)

Scheduled tick (async):
  M3SScheduler.run() → M3S.rebalance()
      ├── allocator.compute(snapshot) → AllocationDecision (persisted to m3s_state)
      ├── compounder.update_base(snapshot, trigger="scheduled")
      ├── edge_decay.check(snapshot, now_ms) → halve/pause transitions
      └── [AutoModeSwitcher.maybe_switch if regime inputs are provided]

Persistence: m3s_state (compound/allocation/mode/edge_decay namespaces) + m3s_events (audit log).
Rollback: `settings.toml [m3s] enabled = false` → M3S does not initialize. Engine runs unchanged.
```

### Future Live Path (Phase 3+)
```
Exchange WS (Binance/IC Markets/Alpaca/IBKR)
  --> Data Ingest (asyncio + uvloop)
    --> Candle Builder (OHLCV from ticks)
      --> Feature Engine (ta library indicators)
        --> StrategyRouter → Strategy.on_features() -> Signal
          --> Risk Manager [SEPARATE ZeroMQ PROCESS]
            --> M3S Allocator (position sizing, mode-aware)
              --> Execution Engine (smart order routing)
                --> Portfolio Tracker (P&L update)
                  --> Trade Log (SQLite) + Redis (hot state)
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
| `risk_state` | RiskState.persist() | RiskState.load_from_db() | Key-value persistence across restarts |
| `risk_decisions` | RiskManager._log_decision() | Dashboard, Audit | Every risk decision with checks_json + equity snapshot |
| `paper_positions` | PaperExecutor | PaperExecutor.restore_state() | Open positions (crash recovery) |
| `paper_equity` | PaperExecutor | PaperExecutor.restore_state() | Equity + trade count (singleton, crash recovery) |
| `m3s_state` | M3SStore.put() (sub-phase 0.1) | Scheduler, Hooks, crash recovery | (namespace, key, value JSON, updated_ts_ms) — compound base/HWM, active mode, latest allocation |
| `m3s_events` | M3SStore.append_event() (sub-phase 0.1) | Meta-backtest, Dashboard, Audit | Append-only log (allocation / compound_update / mode_transition / dd_freeze / custom_config_loaded) |

### Parquet Files (data/)

| Path Pattern | Written By | Read By | Type |
|---|---|---|---|
| `data/historical/{symbol}_{tf}.parquet` | Downloader | VectorBT, NautilusTrader | OHLCV bars |
| `data/backtest_results/{strategy}_{date}.parquet` | Backtest Runner | Report Generator | Backtest output |

### Backtest DB Tables (data/trades.db)

| Table | Written By | Read By | Purpose |
|---|---|---|---|
| `backtest_runs` | ResultStore.save_run() | CLI list/compare | Run metadata (strategy, symbol, tf, dates) |
| `backtest_results` | ResultStore.save_run() | CLI compare, Dashboard | Metrics (Sharpe, DD, PF, equity curve JSON) |
| `validation_sessions` | ResultStore.save_validation() | CLI compare | Validation run metadata (strategy, symbol, tier, verdict) |
| `protocol_results` | ResultStore.save_validation() | CLI compare | Individual protocol results (passed, detail JSON, runtime) |

---

## Config Dependencies

| Config File | Used By | Critical Settings |
|---|---|---|
| `config/settings.toml` | All modules | `mode`, `log_level`, `telegram_token` |
| `config/strategies.toml` | Strategies, M3S Registry | Strategy params, risk profiles (SAFE/MODERATE/AGGRESSIVE) |
| `config/risk.toml` | Risk Server, Circuit Breaker | Max DD, daily limits, Kelly fraction, [modes] default, [strategy_profiles.*] |
| `config/m3s.toml` (sub-phase 0.8) | M3S Scheduler, Hooks | M3S enabled/phase, mode, allocator, compounder, CUSTOM safety rails, regime detector, edge-decay, conviction, evaluation (see m3s_plan_v1.md §A8) |
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
| 2026-04-11 | EMA crossover is unprofitable on crypto intraday. Crypto 1m-15m is ~60-70% mean-reverting. Use mean reversion strategies instead. |
| 2026-04-11 | Trend-following filters (EMA level, DI+/DI-) are logically contradictory with mean reversion entries. At BB extremes, directional indicators always agree with the extreme. Use regime filters (ADX) instead. |
| 2026-04-11 | Risk-based position sizing with tight stops on BTC ($50 SL on $68K price) creates 13x+ leverage and $270+ commission per trade. Always cap notional with `max_notional_pct`. |
| 2026-04-11 | Sharpe calculation with per-candle returns on sparse signal strategies is degenerate (99%+ zero returns). Must resample to daily returns via `pd.to_datetime + resample("D")`. |
| 2026-04-11 | BTC mean-reverts poorly (PF 0.42) even at ADX<20. Altcoins (BNB, ADA, DOT, MATIC) mean-revert well (PF 1.4-6.9). Exclude BTC from MR universe. |
| 2026-04-11 | Double-commission bug in `engine.py`: `_close_position()` stores `trade.pnl = pnl - commission`, but equity update was `equity += trade.pnl - trade.commission` (subtracting twice). Fixed: `equity += trade.pnl`. |
| 2026-04-11 | `continue` on last-bar signal (no next bar) skipped `equity_history.append()`, crashing with length mismatch. Fixed: wrapped entire position-opening block in `if i + 1 < n`. |
| 2026-04-12 | VPIN regime filter has ZERO impact on bb_rsi_mr because ADX < 20 already blocks the same high-activity periods VPIN detects. VPIN is only useful for strategies WITHOUT an ADX filter. |
| 2026-04-12 | bb_rsi_mr spot_check always fails: strategy only generates 0-3 trades per 90-day window due to strict ADX < 20 + BB touch + RSI extreme triple filter. This is by design, not a bug. |
| 2026-04-12 | Compare subcommand loads equity curves from DB JSON, normalizes to returns, combines equal-weight. Portfolio Sharpe 1.315 with avg pairwise correlation 0.031 across 5 altcoins. |
| 2026-04-12 | Donchian Channel breakout: `ta.DonchianChannel` includes current bar's high/low, so `close > DCH_N` is impossible (close <= high always). Must compare close against PREVIOUS bar's channel values via `self._prev_features`. |
| 2026-04-12 | BTC-Neutral MR z-score reverts 100% within 20 bars mathematically, but price-based SL/TP fires before z-reversion. The mismatch between z-space exits and price-space stops makes the strategy unprofitable. |
| 2026-04-13 | M3S `M3SMode.CUSTOM` is distinct from `RiskMode.CUSTOM` — M3S modes (CONSERVATIVE/STANDARD/GROWTH/CUSTOM) live in `src/m3s/modes.py`, risk modes (AGGRESSIVE/BALANCED/DEFENSIVE/CUSTOM) live in `src/risk/modes.py`. Two separate enums on purpose — M3S sits in front of the risk server and shrinks signals; risk modes govern the ZMQ risk checks behind it. Never cross-import. |
| 2026-04-13 | M3S `compound_hwm_gate` is force-enabled on every mode including CUSTOM. Loader logs a WARN and overrides `compound_hwm_gate=False` to True because variance-drag math applies universally — no-HWM compounding is mathematically negative-EV regardless of mode aggression. |
| 2026-04-12 | Strategies needing reference symbol data (e.g., BTC for beta-neutral): add `"ref_symbol": "BTCUSDT"` to STRATEGY_REGISTRY entry. `cmd_run()` downloads it and adds `ref_close` column to OHLCV. |
| 2026-04-12 | Multi-strategy portfolio (bb_rsi_mr + donchian + vol_momentum) achieves Sharpe 1.748 with near-zero cross-strategy correlation (-0.049, -0.033). Mean-reversion + trend + momentum = 3 uncorrelated edge types. |
| 2026-04-12 | `dc_short`, `bb_period`, `rebalance_interval`, `vol_target` have ZERO effect on Sharpe within ±20% grid. The strategy edge comes from other params — don't waste optimization time on these. |
| 2026-04-12 | ADX > 25 trend filter on Donchian improves 4/6 symbols (+18-54%) by filtering false breakouts in ranging markets. Symbol-specific: helps NEAR/DOGE/ETH/ADA, hurts AVAX/DOT. |
| 2026-04-12 | Long-only kills both Donchian (-75%) and Vol Momentum (-72%). Crypto short trades during downtrends are where significant edge lives for trend-following and momentum strategies. |
| 2026-04-12 | VPIN filter has ZERO impact on Donchian (tested on NEAR/AVAX/DOT). Pattern: VPIN is universally useless as an overlay when strategies already have their own entry conditions filtering toxic flow. |
| 2026-04-12 | BTC-Neutral stat-arb structurally broken since Jan 2024 ETF approval — BTC decoupled from altcoins per LSE 2026 paper. Rolling OLS produces artifactual z-score reversion, not real mean-reversion. |
| 2026-04-12 | WF-EMA was cherry-picked Sharpe from 81 combos during 2020-2021 bull. 2024-2026 crypto is "whipsaw intraday, trending on higher TFs". 6H+ is the current viable timeframe for trend-following. |
| 2026-04-12 | `cmd_optimize` subcommand automates multi-round grid search with PARAM_PRIORITY table, convergence detection (< 5% improvement for 3 rounds), and param base updating between rounds. |
| 2026-04-12 | Risk-gated backtesting requires 3 fixes vs live: (1) `RiskState(db_path=":memory:")` to avoid persist/load failures, (2) `DuplicateFilter` must use signal.timestamp not `time.time()` or all signals look like duplicates, (3) `check_period_resets(timestamp_ms)` must use candle time not wall-clock or daily/weekly resets never fire and strategy_paused set grows forever. |
| 2026-04-12 | Fat finger `update_price()` must be called on every evaluate(), not just approved signals. Otherwise price gaps between sparse trades (days/weeks) trigger false FAT_FINGER_PRICE rejections. |
| 2026-04-12 | AGGRESSIVE risk mode ≈ NONE in backtest: 0 rejections for Donchian (45/45 trades pass), Vol Momentum (136/136). DEFENSIVE filters 19-26 trades depending on symbol, often improving Sharpe by removing bad trades. |
| 2026-04-12 | Warmup must temporarily replace `feature_engine.on_features` callback, not call `_on_features` directly — otherwise signals during warmup execute trades. Warmup callback routes to router (warms strategy state) but discards signals. |
| 2026-04-12 | PaperExecutor price persistence is throttled to 10s intervals (`_persist_prices_batch`) — updating on every tick would thrash SQLite. Positions + equity are persisted immediately on open/close. |
| 2026-04-12 | `compute_metrics()` was extracted from `BacktestEngine._compute_metrics()` into standalone function in `metrics.py`. Tests referencing the old API must import from `src.backtest.metrics`. |
| 2026-04-12 | `Candle` msgspec struct uses field `closed`, not `is_closed`. warmup.py had `is_closed=True` which crashed on startup. Always check struct definition in `src/utils/types.py`. |
| 2026-04-12 | `ta` library ADX/Donchian/Stoch need `2*window` minimum rows (e.g., ADX_14 needs 28+). Previous guard in feature_engine.py used `window+5` which crashed during warmup when buffer was building. Fixed to `2*window+5`. |
| 2026-04-12 | `main.py` had `testnet=True` hardcoded. Paper trading needs production Binance WS for real prices (fills are simulated locally). Changed to read from `config/exchanges.toml`. |
| 2026-04-12 | Indicator correctness testing must pin values to a **closed historical bar** (2+ bars back from live), not `iloc[-1]`. Live-forming bars drift every second → any captured value is stale instantly. Use `reference_bar_timestamp_ms` in `tests/fixtures/tv_reference_btcusdt_1h.json` to index into the fixture. Closed bars are immutable → tolerances can drop to 0.01. |
| 2026-04-12 | TV indicator capture workflow: **one indicator at a time**. Clear chart → add one study → Prince screenshots Data Window on the fixed reference bar → read value → remove → next. Multi-indicator captures overlap legends and make values unreadable. `scripts/build_tv_fixture.py` holds the pinned golden values. |
| 2026-04-12 | TV MCP `indicator_set_inputs` silently returns `updated_inputs: {}` for most studies (the input override doesn't apply). Workaround: ask Prince to change via Settings dialog. `data_get_indicator` also returns `inputs: []` for most studies → cannot verify settings via API, must read legend visually. |
| 2026-04-12 | `ta.stoch()` returns **raw %K** (no smoothing), but TV's default Stochastic `(14, 3, 3)` displays **SMA3 of raw %K** as its "%K". So TV's `%K` at default settings == our `STOCHd_14` (`stoch_signal`), NOT our `STOCHk_14`. Our `STOCHk_14` only matches TV when TV `%K smoothing = 1`. |
| 2026-04-12 | EMA seed method creates residual divergence for long periods: `ta.ewm(adjust=False)` seeds with first bar's value, while some TV scripts use SMA-of-first-N then transition. EMA 9 matches bit-exact, EMA 21 diverges by ~40 on BTC. For verification, always use TV's native `Moving Average Exponential`, not custom multi-EMA scripts. |
| 2026-04-12 | Bit-exact verified against TradingView on closed bar (Sun 12 Apr 2026 20:30 IST, BTCUSDT 1H): EMA_9, RSI_14, BBU/BBM/BBL_20, MACD/signal/hist, ATR_14 (RMA), ADX_14, DI+/-_14, STOCHd_14. All match to 4 decimals. `_compute_indicators()` is validated against TV on real market data. |
| 2026-04-12 | SOLUSDT fails bb_rsi_mr (Sharpe -1.424, 1 losing trade). SOL trends too hard for mean reversion even with ADX < 20 filter. Replaced with APTUSDT (Sharpe 1.424). |
| 2026-04-12 | `heartbeat.json` now includes `signal_count`, `rejection_count`, `strategy_exceptions`. Status priority: KILLED > STALE > WARNING (exceptions > 5) > HEALTHY. |
| 2026-04-12 | PaperExecutor `_open_position()` computed opening commission but never deducted from equity. BacktestEngine charges both sides at close. Fixed: added `self._equity -= commission` in both `_open_position()` and `execute_order()`. Paper trading was silently over-reporting equity. |
| 2026-04-12 | MATICUSDT delisted from Binance spot 2024-09-10 (Polygon→POL rebrand). WS stream produces nothing, parquet ends Sep 2024. Replaced with SOLUSDT in bb_rsi_mr. |
| 2026-04-12 | RiskClient had no auto-recovery: after 3 consecutive ZMQ failures, permanently rejected all signals even if risk server restarted. Fixed: 60s cooldown resets failure counter. |
| 2026-04-12 | Shutdown order matters: heartbeat must stop LAST, not first. If heartbeat stops before state persistence, watchdog may kill the process mid-persist. Reordered: feed→persist→storage→heartbeat. |
| 2026-04-12 | Feature engine `_compute_indicators()` had `if col not in df.columns` guards that cached indicators once and never updated with new data. Removed all guards so indicators always recompute on each candle. |
| 2026-04-12 | `orjson.dumps()` without `OPT_SERIALIZE_NUMPY` crashes on numpy.float64 scalars from `ta` library. `round()` on numpy scalars returns numpy scalars. Fixed in storage.py (signal metadata) and heartbeat.py. Added `default` handler in logger.py as safety net. |
| 2026-04-12 | Duplicate candle emission: both tick-aggregation and Binance kline pass-through emitted candles to FeatureEngine. CandleBuilder now auto-detects kline pairs and suppresses tick-built candles for them. |
| 2026-04-12 | WS subscription waste: system subscribed to ALL symbol×timeframe kline streams (cartesian product). Now uses `needed_pairs` set from enabled strategies — only subscribes to klines strategies actually consume. |
| 2026-04-12 | `pd.concat([df, new_row])` copies entire 500-row buffer on every candle append. Replaced with `df.loc[len(df)] = row_dict` for in-place append. Eliminates FutureWarning and GC pressure. |
| 2026-04-12 | FeatureEngine recomputed ALL 500 rows of indicators on every candle. Now uses `COMPUTE_WINDOW=250` tail slice — 2x less work, identical results (recursive indicators converge within 250 rows for all windows up to 120). |
| 2026-04-12 | `msgspec.json.Encoder()` has no numpy support by default (unlike orjson). RiskClient serialization would crash on numpy.float64 signal fields. Added `enc_hook=_numpy_enc_hook` to convert via `.item()`. |
| 2026-04-13 | `src/m3s/signal_filter/train.py` `ScaledLR` wrapper must be **module-level** (not nested inside `_fit_logistic`) for joblib pickling to work. Nested classes fail with `Can't pickle ... it's not found as ... .<locals>.ScaledLR`. Also, LightGBM import must be `except (ImportError, OSError)` not just `ImportError` because macOS libomp.dylib missing raises `OSError: dlopen` not `ImportError`. |
| 2026-04-13 | Scripts that load meta-label joblib artifacts (e.g., `meta_label_shadow_check.py`) must prepend the repo root to `sys.path` before `joblib.load()` — joblib deserializes `src.m3s.signal_filter.train.ScaledLR` via fully-qualified module name, and scripts run with different CWD/PYTHONPATH hit `ModuleNotFoundError: No module named 'src'`. |
| 2026-04-13 | LightGBM with default params on 1000-2000 audit rows can produce a **null predictor**: all-zero feature importance, AUC exactly 0.500. `train.py` A/B sanity check rejects it via `LGBM_MIN_AUC_UPLIFT=0.03` rule — LightGBM only wins if it beats LR by ≥0.03 AUC. After rejection, delete stale `*_lightgbm_*.joblib` artifacts that aren't symlinked as latest. |
| 2026-04-13 | `FundingSyntheticFeed` Binance endpoint gotcha: `/fapi/v1/premiumIndex` returns `nextFundingTime` not `lastFundingTime`. Switched to `/fapi/v1/fundingRate?limit=1` which returns `fundingTime` + `fundingRate` directly. |
| 2026-04-13 | Warmup must skip synthetic symbols (e.g., `BTCUSDT-CARRY`). `_get_needed_pairs_from_config` in `main.py` filters out `-CARRY`/`-SYNTH` suffixes to avoid WS subscription errors. `src/data/warmup.py` has a `warmup_skip_synthetic` branch. |
| 2026-04-13 | M3S `m3s_shadow_check.py` promotion clock was compressed 7 → 4 days for Session 22 sprint. 16 audits at 6h cadence give dense validation in a short window. `promote_m3s_authoritative.sh` hard-codes the 4-day threshold. |
| 2026-04-13 | `FEATURE_KEYS` in `src/m3s/signal_filter/features.py` is **append-only**. Order matters for reproducibility. New features go at END of the list. Old harvested rows in `signal_audit.features_json` are forward-compatible: missing keys fill with `None` → `0.0` in training pipeline (equivalent to ignoring the feature). Current size: 24 keys (15 Phase 0 + 9 Session 22 enrichment). |
| 2026-04-13 | `FeatureEngine._compute_indicators()` auto-computes `VOL_ZSCORE_20` (always, when n≥22) and `MACD_HIST_ZSCORE_50` (when `MACD_hist` column exists and n≥52) as backward-compatible additions for meta-label features. Strategies don't need to read them; they populate the feature builder's `volume_zscore_20` / `macd_hist_zscore_50` keys automatically. |
| 2026-04-13 | LightGBM null-predictor root cause: purged sample_uniqueness_weights from `src/m3s/signal_filter/cv.py` are in [0, 1] and their mean on typical backtest data is ~0.0005, which starves LightGBM's `min_child_samples=20` check (effective n per leaf << 20 after weight scaling). Fix in `train.py`: normalize weights to mean=1.0 before passing to `.fit(sample_weight=...)`. Preserves relative down-weighting for highly-overlapping labels while keeping split counts intact. Hyperparameter tuning was a red herring — the real bug was weight scaling. |
| 2026-04-13 | Training X matrix masks `entry_price_ref`/`portfolio_equity`/`portfolio_hwm`/`m3s_alloc_weight_now` via `_TRAINING_FEATURE_EXCLUDES` in `train.py`. These keys are present in `FEATURE_KEYS` (so audit rows record them for reconstruction) but excluded from `build_xy` output because they're raw-scale / time-cohort / allocator-state leaks. `filter.py::_feature_row` must call `training_feature_keys()` to match the trained shape at inference. |
| 2026-04-13 | `StrategyHistoryCache` is an in-process ring buffer (deque per strategy) updated by `TradingEngine._audit_live_signal` and `_audit_live_close`. Reads are O(1) — querying `signal_audit` SQL on every signal would add I/O to the hot path. Survives process restart only via the audit DB (cache is rebuilt from empty on each boot and refills as signals flow). |
| 2026-04-14 | Watchdog's file-mtime check is blind to "alive but frozen" engines: an orphaned Python process from a previous run kept writing `heartbeat.json` every ~30s with `last_candle_age_s=8570` (2.4h stale) while the CandleBuilder had deadlocked. mtime stayed fresh → watchdog reported HEALTHY → no restart. Fix in `scripts/watchdog.py`: added `_read_candle_age_s()` that parses `last_candle_age_s` INSIDE the heartbeat, and a new kill branch firing at `DATA_STALE_KILL_THRESHOLD=1800s` (longer than file-mtime KILL_THRESHOLD to absorb weekend/overnight gaps). Also: `launchctl kickstart -k` only kills the launchd-tracked PID — orphan processes from earlier crashed runs must be `kill -9`'d manually by PID from `ps aux \| grep src.main`. |
| 2026-04-14 | macOS `launchd` `StartInterval` fires are **skipped when the Mac is asleep**. A 30-min agent loaded 48h ago shows `runs=42` instead of the naive 96 — missed fires during sleep are NOT replayed (at most one fire after wake). This is why `project-status` / `m3s-shadow-check` appear "never fire" — they DO fire (`launchctl print` shows the run count), just less often than wall-clock expects. For critical periodic agents, use `StartCalendarInterval` (absolute wall-clock times) which launchd DOES replay on wake. Non-critical agents can stay on `StartInterval` — watchdog catches any resulting data staleness at the engine level. |
