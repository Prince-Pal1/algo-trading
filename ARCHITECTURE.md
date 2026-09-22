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
| Binance WebSocket Feed | `src/data/feeds/binance_ws.py` | `asyncio`, `orjson`, `websockets`, `certifi`, `httpx`, Order Book Processor | Candle Builder, Depth Recorder | ✅ working (exponential backoff 1s→60s, 10% jitter). 2026-09-21: opt-in `depth_symbols=[...]` adds `@depth@100ms` diff streams, one `OrderBookState` per symbol, REST bootstrap after connect, auto re-bootstrap on desync, `on_depth` callback. **Live depth path is UNTESTED against the exchange** — the cloud container's egress proxy blocks Binance. |
| Historical Warmup | `src/data/warmup.py` | FeatureEngine, StrategyRouter, Downloader, ParquetStore | TradingEngine | ✅ working (200 candles from Parquet/REST, signals discarded) |
| Alpaca WebSocket Feed | `src/data/feeds/alpaca_ws.py` | `alpaca-trade-api`, `asyncio` | Candle Builder | 📋 planned |
| IBKR Data Feed | `src/data/feeds/ibkr_feed.py` | `ib_insync` | Candle Builder | 📋 planned |
| IC Markets Feed | `src/data/feeds/icmarkets_feed.py` | `ctrader-open-api`, Twisted (threaded reactor), Types, `scripts/ctrader_refresh_token` | TradingEngine (gold) | ✅ Session 23 D1 — full 5-stage auth walk; Session 24 (2026-05-05) added `ProtoOASubscribeLiveTrendbarRes` ack handler + `_send_loud()` + 30s pending-subscribe audit; Session 27 (2026-06-08) added `_handle_token_expired()` auto-refresh on `CH_ACCESS_TOKEN_INVALID` (uses `scripts/ctrader_refresh_token.refresh_tokens` + `_write_env_tokens`, updates in-memory config + os.environ, re-sends `GetAccountListByAccessTokenReq`; guarded by `_token_refresh_in_flight`/`_token_refresh_failed` for at-most-once-per-process semantics). Reactor runs in worker thread, asyncio stays on the main loop. |
| Parquet Replay Feed | `src/data/feeds/parquet_replay_feed.py` | pandas, Types | Shadow orchestrator, future tests | ✅ G.2h.2 (async `on_candle` contract matches BinanceWebSocketFeed; realtime + fast-forward modes) |
| Deribit WebSocket Feed | `src/data/feeds/deribit_ws.py` | `asyncio`, `websockets` | Candle Builder | 📋 planned |
| Candle Builder | `src/data/candle_builder.py` | Data Feeds | Feature Engine, Strategies | ✅ working |
| Feature Engine | `src/data/feature_engine.py` | `ta`, `pandas` | Strategies | ✅ working |
| Order Book Processor | `src/data/order_book.py` | Types (`OrderBookSnapshot`) | Binance WS feed, Depth Recorder | ✅ 2026-09-21 (`OrderBookState` — REST-snapshot bootstrap + diff-stream sync with spot `U`/futures `pu` contiguity checks; a sequence gap sets DESYNCED and the book then refuses to serve snapshots until re-bootstrapped) |
| Depth Recorder | `src/data/depth_recorder.py` | `pyarrow`, `pandas`, Types | `scripts/depth.py`, dashboard page 8 | ✅ 2026-09-21 (`DepthRecorder` decimates the 100ms stream to a sampling cadence; `DepthStore` persists long-form rows keyed on (timestamp, side, price) — NOT ParquetStore, see Known Gotchas; `to_heatmap_grid` pivots with tick-aligned price bucketing) |
| Historical Downloader | `src/data/downloader.py` | `httpx`, `certifi` | Backtesting | ✅ working |
| CVD / Order Flow | `src/data/cvd.py` | `pandas`, `numpy`, `msgspec`, Candle Builder (`TF_MS`), Types | `src/data/agg_trades.py`, `scripts/cvd.py` | ✅ 2026-09-21 (streaming `CVDCalculator` + vectorized `compute_delta_bars` + `detect_divergences`/`detect_absorption`; parity-tested both paths; degenerate quote-only-feed guard) |
| AggTrades Downloader | `src/data/agg_trades.py` | `httpx`, `certifi`, CVD, ParquetStore | `scripts/cvd.py` | ✅ 2026-09-21 (free Binance Data Vision daily dumps; day-by-day fold into delta bars, CVD continuous across days; handles headerless/headered, 7- and 8-column, µs timestamps) |

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
| Donchian Gold (leveraged) | `src/strategies/trend_following/donchian_gold.py` | DonchianEnsembleStrategy, stdlib | LeveragedBacktestEngine | ✅ G.2d (session-filtered XAUUSD, leverage_range=(10,50), London/NY only, tier=INSTITUTIONAL_TREND) |
| Vol Momentum Gold (leveraged) | `src/strategies/momentum/vol_momentum_gold.py` | VolMomentumStrategy, stdlib | LeveragedBacktestEngine | ✅ G.2h.1 (tuned: mw=240, vt=0.20, long_only=True, session=True → +20% / Calmar 1.365 / corr<0.23 with donchian_gold, tier=INSTITUTIONAL_MR) |
| Adaptive Momentum | `src/strategies/momentum/adaptive_momentum.py` | Feature Engine (ATR_14), BaseStrategy, numpy | Router, Backtest CLI | ✅ Session 30 (multi-horizon 24/72/168h vol-normalized momentum + Kaufman ER gate + tanh sizing + ATR chandelier trail + hysteresis; signals inline from `_closes` deque; beats vol_momentum: mean Sharpe 0.45 vs 0.27, maxDD halved, 8× less turnover; `enabled=false`. NB: hours-based lookbacks → 1h-only design TF) |
| Candle Burst Hunter | `src/strategies/aggressive/candle_burst_hunter.py` | BaseStrategy, Feature Engine (ATR) | Tier 5 sub-book | ❌ KILLED (G.2f + task #94 + task #100 M1 revival fail) — see `strategy_graveyard` |
| News Spike Fade | `src/strategies/aggressive/news_spike_fade.py` | BaseStrategy, NewsWindow, news_calendar.csv | Tier 5 sub-book | ❌ KILLED (G.2f + task #92 + task #100 M1 revival fail with peak_reversal mode) — see `strategy_graveyard` |
| Hedged Structure Play | `src/strategies/aggressive/hedged_structure_play.py` | BaseStrategy, structure_levels | Tier 5 sub-book | ❌ KILLED (G.2f + task #93 + task #100 M1 revival fail) — see `strategy_graveyard` |
| Strategy Graveyard | `src/strategies/graveyard.py` | sqlite3 | Ingest script + future revival tracking | ✅ 2026-04-14 (structured registry for killed strategies, backs onto `data/trades.db :: strategy_graveyard` table, idempotent upsert on (name, version)) |
| Scalper Base | `src/strategies/base_scalper.py` | BaseStrategy, collections.deque, M1SubBar | Scalper strategies | ✅ 2026-04-14 task #102 (rolling N-bar history buffer + multi_bar_velocity/is_multi_bar_burst/is_volume_confirmed/broke_n_bar_high/intrabar_max_velocity/intrabar_body_direction helpers) |
| AlternateTimeframeBuilder | `src/data/timeframe.py` | pandas, collections.deque | SwiftAlmaStrategy + any future alt-TF strategy | ✅ 2026-04-14 task #103 (incremental OHLCV aggregation from base TF into higher TF, reproducing Pine's `request.security(higherTF, ...)` pattern deterministically) |
| SwiftAlmaStrategy | `src/strategies/trend_following/swift_alma.py` | BaseStrategy, _alma, AlternateTimeframeBuilder | Graveyard (PREMISE — cost-killed) | ❌ 2026-04-14 task #103 (ported from TradingView SWIFTALGO Pine Script, 3-tier TP ladder 50/30/20 at 1/1.5/2%, ALMA(close)×ALMA(open) crossover on alt TF, passes zero-cost gate but fails IC Markets Raw) |
| ZeroCostFeeModel | `src/backtest/costs.py` | dataclass | Pine-faithful backtests | ✅ 2026-04-14 task #103 (drop-in FeeModel returning 0 spread, 0 slippage, 0 commission — lets us reproduce TradingView strategy tester numbers for apples-to-apples comparison) |
| Fee Profile Registry | `src/backtest/fee_profiles.py` + `config/broker_fees.toml` | tomllib, costs.py | Every backtest | ✅ 2026-04-14 task #106 + 2026-04-16 task #141 (research-calibrated named broker × platform × scenario profiles. 9 profiles cover IC Markets cTrader/MT4 × XAUUSD/FX × normal/news/stress + pine_zero_cost. `group_profiles_by_broker_platform()` + `list_brokers()` helpers for the hierarchical dashboard fee selector. See `docs/BROKER_FEES.md`.) |
| SmokeDemo Strategy | `src/strategies/demos/smoke_demo.py` | BaseStrategy, collections.deque | `scripts/smoke_test_dashboard.py` only | ✅ 2026-04-16 task #141 (minimal SMA(5)/SMA(20) crossover on XAUUSD — NOT for production. Guaranteed signal firing on any XAUUSD window. Registered as `smoke_demo` in router. Powers the 11-step dashboard smoke test harness.) |
| TV Parity Framework | `src/backtest/tv_parity.py` | pandas, _alma, feature_engine, openpyxl | tv_parity_validate.py CLI + future Pine ports | ✅ 2026-04-14 task #104 (reusable library for Stage 0 Pine port validation — CSV/xlsx loaders with auto-TZ + ladder leg collapse, ISO 8601 chart time support, DST-aware anchor detection, lookahead ALMA crossover helper, reversal simulator, bar-by-bar + trade-by-trade matchers, Pine config auto-extract from xlsx Properties sheet, Markdown report writer) |
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
| IC Markets Executor | `src/execution/icmarkets_executor.py` | `ctrader-open-api`, asyncio | TradingEngine (gold) | ✅ Phase 4 skeleton (NewOrder/ClosePosition request flow + async Future correlation via clientMsgId) |
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
| Inline Leverage Gates | `src/risk/inline_leverage.py` | Types (Signal) | LeveragedBacktestEngine, future inline fast path | ✅ G.2c (in-process 3 gates: per-position / aggregate / liquidation buffer; INSTITUTIONAL + AGGRESSIVE_RETAIL profiles) |

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
| Leverage Grant Store | `src/m3s/leverage_grants.py` | `sqlite3` | M3S.request_leverage | ✅ G.2b (data/trades.db :: leverage_grants table, append-only) |
| Aggressive Retail Compounder | `src/m3s/aggressive_compounder.py` | stdlib | LeveragedBacktestEngine Tier 5 | ✅ G.2e (fixed-% sizing, daily/total DD kill switches, weekly refund from main) |
| Versioned Portfolio View (RCU) | `src/m3s/portfolio_view.py` | stdlib | Inline leverage gates, future fast-path risk | ✅ G.2c (lock-free versioned snapshot for cross-layer state reads) |
| Leverage Budget Allocator | `src/m3s/leverage_budget.py` | PortfolioSnapshot, Tier | Future: M3S.allocate_leverage_budget | ✅ G.2h.4 (tier-based floor + dynamic Sharpe-weighted pool, per-strategy grants never exceed requests) |

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
| Leveraged Book | `src/backtest/book.py` | stdlib | LeveragedBacktestEngine | ✅ G.2a (LeveragedPosition, SubBookState, Book, CFD 50% stop-out, institutional+aggressive sub-books) |
| Cost Models | `src/backtest/costs.py` | stdlib, csv | LeveragedBacktestEngine, Tier 5 strategies | ✅ G.2a.2 (ICMarketsMetalFeeModel: spread 0.13 pip + commission $3/lot/side + news windows) |
| Intrabar Path | `src/backtest/path.py` | random, math | LeveragedBacktestEngine | ✅ G.2a.3 (BrownianBridgeModel + PessimisticPathModel + check_sl_tp_hits, deterministic via run_id+bar_idx) |
| Leveraged Backtest Engine | `src/backtest/leveraged_engine.py` | Book, costs, path, FeatureEngine, BaseStrategy, M3S (optional) | Gold research, sweep scripts | ✅ G.2a.4 (fresh engine, separate from crypto BacktestEngine, integrates M3S.request_leverage when wired) |
| Shadow Orchestrator | `src/shadow_orchestrator.py` | Book, costs, path, ParquetReplayFeed/ICMarketsFeed, strategies | G.3 paper clock driver | ✅ G.2h.2 (async feed-driven, reuses leveraged engine components, matches batch engine results bit-exact on 2yr XAUUSD 1h: +38% / 12% DD / 69 trades) |
| Structure Levels | `src/backtest/structure_levels.py` | `pandas` | hedged_structure_play, future structure-aware strategies | ✅ G.2e (prior day H/L, session open ranges, round numbers, swing H/L, Fibonacci retraces) |
| Deep Backtest Pipeline | `src/backtest/deep_backtest.py` | LeveragedBacktestEngine, fee_profiles, STRATEGY_REGISTRY, M1PathModel | `scripts/deep_backtest.py` CLI | ✅ 2026-04-15 task #112 + #114 + #115 + #79 (6-phase pipeline: preflight → matrix → sanity → Phase 2.5 leverage_mode validation → walk-forward OOS → verdict). **Task #114: 5 leverage modes (INVARIANT / MARGIN_CAPPED / VOL_TARGETED / RISK_SCALED / KELLY_FRACTIONAL) via `_apply_leverage_mode()` strategy_params transform, mode-aware Phase 2 sanity + Phase 5 verdict gates. Task #115: zero-tolerance Phase 2.5 hard-fail assertions (9 for RISK_SCALED, 6+2 for KELLY_FRACTIONAL) + Phase 0 read-back probe. Task #79 (G.7): per-cell `AttributionBreakdown` (alpha / lev_amp / cost_drag / margin_rejection_drag / residual) computed after Phase 2 via `_compute_matrix_attribution`, serialized to `summary.json["attribution"]`. DRAWDOWN_BUDGETED deferred.** |
| Deep Backtest Report | `src/backtest/deep_backtest_report.py` | fpdf2, matplotlib, pandas | Deep Backtest Pipeline | ✅ 2026-04-15 task #112 + #79 (HTML + landscape-A4 PDF + PNG heatmaps + CSV; task #79 adds Attribution section between Walk-forward and Heatmaps, with best-cell breakdown + full-matrix table) |
| Deep Backtest Interactive TUI | `src/backtest/deep_backtest_interactive.py` | `questionary` (optional), `deep_backtest` config | `scripts/deep_backtest.py` CLI | ✅ 2026-04-15 task #114 (multi-select tick-box prompts for windows / timeframes / fees / leverages / leverage modes; fallback to CLI-flag mode when questionary missing or stdout not a TTY; supports multi-mode runs writing to separate report dirs) |
| Deep Backtest Compare CLI | `scripts/deep_backtest_compare.py` | stdlib only (argparse, json) | Manual run (portfolio composition review) | ✅ 2026-04-15 task #79 (G.7) — side-by-side HTML comparison of 2+ deep_backtest runs: verdict, best cell, attribution decomposition, walk-forward, equal-weight portfolio estimate, dominant/diversifier recommendation. Exit code reflects best verdict across inputs (0=DEPLOYABLE, 1=partial, 2=all failed) |
| Strategy Storage | `src/strategies/storage.py` | sqlite3, BaseStrategy (lazy), deep_backtest helpers | `run_deep_backtest` auto-hook, `scripts/strategies.py`, `scripts/strategies_backfill.py`, `scripts/strategy_graph.py`, `src/dashboard/app.py` page 6 | ✅ 2026-04-15 tasks #117/#118-#130 — Versioned registry in `data/trades.db`. Tables: `strategies` (parent), `strategy_versions` (variants — auto-captured from `run_deep_backtest()`), `strategy_version_runs` (append-only history, schema v5). Schema migration framework (`PRAGMA user_version` + `_apply_migrations`, schemas v1→v5: WF denormalization, killed_graveyard_id FK, backtest_run_id FK, history table). WAL + FK + retry-on-busy from day 1. Slug scheme `{mode}_L{int(baseline)}_{tf}` + 6-char param-hash on collision. Do-not-clobber UPSERT preserves user-set fields. JSON1 query helpers (`query_best_by_max_return`, `query_deployable_by_calmar`, `query_vanity_traps`). `kill_strategy()` decoupled hook. Test isolation via `ALGO_STRATEGY_DB` env var + `tests/test_backtest/conftest.py`. |
| Strategies CLI | `scripts/strategies.py` | `src.strategies.storage`, optional `rich`, `webbrowser` | Manual run | ✅ 2026-04-15 tasks #117/#120 — Subcommands: `list`/`show`/`register`/`open`/`sync`/`kill`/`vanity`/`top`/`history`. Amber vanity-flag on max-return cells with < 30 trades, ≥ 60% DD, or Calmar ≤ 0.2. `vanity` audits DEPLOYABLE rows for sanity-failures. `open` launches the deep_backtest HTML in browser. `sync` walks `router.STRATEGY_REGISTRY` and upserts empty rows. |
| Strategies Backfill | `scripts/strategies_backfill.py` | `src.strategies.storage`, pandas | Manual run | ✅ 2026-04-15 task #118 — Walks `reports/deep_backtest_*/` dirs, parses each `summary.json` + `matrix.csv`, upserts strategies + versions + history rows. Idempotent. Used to recover the 16 historical deep_backtest runs that pre-dated task #117. |
| Strategy Graph | `scripts/strategy_graph.py` | `src.strategies.storage` (no Graphviz install needed for DOT generation) | Manual run | ✅ 2026-04-15 task #130 — Emits a Graphviz DOT graph of strategies → versions, color-coded by status + verdict, with vanity-flag glyphs. Optional `--by-family` clusters by family. Render with `dot -Tsvg` separately. |
| Dashboard Page 6 — Strategies | `src/dashboard/app.py` (page 6) | `streamlit`, `pandas`, `src.strategies.storage` | Streamlit dashboard | ✅ 2026-04-15 task #122 — Reads `list_strategies()` + `list_versions()` + `list_version_runs()` + `query_vanity_traps()`. Embeds deep_backtest `index.html` reports inline via `st.components.v1.html`. Vanity-trap audit panel at top. Per-version run history expander. |
| swift_alma_v2 | `src/strategies/trend_following/swift_alma_v2.py` | BaseStrategy, feature_engine (`_alma`), AlternateTimeframeBuilder, `adx_14`/`atr_14` from engine | Registered in `router.STRATEGY_REGISTRY`; deep-backtestable via dashboard page 7 | ✅ 2026-04-15 task #131 — Leverage-mode-aware ALMA crossover upgrade of parent swift_alma. 8 research-justified upgrades: decoupled `max_risk_per_trade`/`sl_pct` (k=2 default), ADX≥22 regime filter, 4h EMA 50 HTF trend, London+NY session filter, 3-bar cooldown, `_effective_risk_pct()` mode-aware sizing branch, ATR-scaled SL/TP (1.0×/1.5×ATR), vol-target realized-vol layer. **Produces 5 distinct P&L under all 5 leverage modes** (parent only differentiates 1 of 5). Validated: +4.36%/+4.81%/+5.63%/+5.74%/+7.81% at 1y MT4 L=15 across INVARIANT/VOL_TARGETED/RISK_SCALED/KELLY/MARGIN_CAPPED — all net-positive vs parent's −12.47%. Phase 2.5 correctly flags RISK_SCALED (32% deviation from linear) and KELLY_FRACTIONAL (trade-count divergence from margin interactions) as expected framework behavior. |
| swift_alma_v2 tests | `tests/test_strategies/test_swift_alma_v2.py` | pytest, swift_alma_v2 | Test suite | ✅ 2026-04-15 task #131 — 30 tests: constructor × 5, effective_risk_pct × 10 (all 5 modes + clamping + distinct-values assertion), regime filter × 1, session filter × 4, HTF EMA × 3, cooldown × 1, framework injection × 6 (verifies backwards-compat — donchian_gold and swift_alma v1 get NO `leverage_mode` injected). Full suite: 1306 passing. |

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

### Flow — level-gated order flow (Python -- src/flow/)

Human supplies levels; the engine reports what flow does when price reaches them.
It never selects a level and never places an order.

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Level Registry | `src/flow/level_registry.py` | `tomllib`, `config/levels.toml` | Flow Engine, monitor | ✅ 2026-09-22 (TOML levels; preserves `first_seen_ms` across reloads — the only thing separating a level written before price arrived from hindsight; malformed file keeps prior levels rather than disarming live ones) |
| Zone Features | `src/flow/features.py` | Types | Evidence, Zone State | ✅ 2026-09-22 (`ZoneAccumulator` — **~1.17 µs/tick**, bounded deques, no allocation per tick. `absorption = |delta_ratio| × (1 − min(1, range_ratio))` × trade-count confidence ramp. Also tape velocity + acceleration, large-print share vs market norm, and **probe tracking** for the lower-volume retest. `MarketContext` supplies baselines measured outside the zone) |
| Evidence Scorer | `src/flow/evidence.py` | Features, Level Registry | Zone State | ✅ 2026-09-22 (rule-based by design — rules generate the labels ML would later need. Tape weighted ~2× book because crypto book display is spoofable. Always reports FOR **and** AGAINST) |
| Zone State Machine | `src/flow/zone_state.py` | Features, Evidence, Types | `scripts/flow_monitor.py` | ✅ 2026-09-22 (`ZoneMonitor` IDLE→APPROACHING→EVALUATING→CONFIRMED/INVALIDATED→IDLE; invalidation beats confirmation; one signal per test. **`ZonePhase` models the attack/defence sequence** — WATCHING→ABSORBING→TURNING (or FAILING) — and a signal fires only on the TURN, not on absorption alone; `require_turn=False` is the comparison arm. `FlowEngine` routes ticks, builds `MarketContext`, tracks tape lag) |
| Signal / Outcome Log | `src/flow/signal_log.py` | `orjson` | `scripts/flow_monitor.py` | ✅ 2026-09-22 (JSONL. Logs a **null-hypothesis baseline for every zone entry**, not just confirmations, so "does the scorer beat taking the level?" is answerable from the log) |
| Live Server + Page | `src/flow/live_server.py` + `live_page.html` | `websockets`, `orjson` | Browser | ✅ 2026-09-22 (serves page on `GET /`, pushes snapshots on `/ws` at 5 Hz. NOT Streamlit — see Known Gotchas. Lag is the headline number; 50-signal ring buffer replayed on connect) |
| Flow Monitor CLI | `scripts/flow_monitor.py` | Binance WS feed, all of `src/flow/` | Manual run | ✅ 2026-09-22 (`--symbol --depth --threshold --port`; uvloop; levels hot-reload every 10s) |

### Dashboard (Python -- src/dashboard/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Streamlit App | `src/dashboard/app.py` | ResultStore, Charts, `streamlit` | Browser | ✅ working (5 pages) |
| Dashboard Launcher | `scripts/dashboard.py` | `streamlit` | Manual run | ✅ working |
| Flow Charts | `src/dashboard/flow_charts.py` | `plotly`, `pandas` | Dashboard page 8 | ✅ 2026-09-21 (`price_cvd_panels` — price/CVD/delta as three panels on ONE shared x-axis, never a dual y-axis; divergence direction encoded by marker shape as well as colour; validated diverging blue↔red pair) |
| Dashboard Page 8 — Order Flow | `src/dashboard/app.py` (page 8) | `streamlit`, Flow Charts, `src.data.cvd`, `src.data.agg_trades` | Streamlit dashboard | ✅ 2026-09-21 (symbol/TF picker from `list_flow_series()`, lookback + z-threshold controls, chart + Divergences/Absorption/Bars table views, research-only warning banner) |

### Utilities (Python -- src/utils/)

| Module | File | Depends On | Used By | Status |
|---|---|---|---|---|
| Config Loader | `src/utils/config.py` | `tomllib` | All modules | ✅ working |
| Logger | `src/utils/logger.py` | `structlog`, `orjson` | All modules | ✅ working |
| Telegram Bot | `src/utils/telegram.py` | `python-telegram-bot` | Risk, M3S, Alerts | 📋 planned |
| Metrics | `src/utils/metrics.py` | `prometheus-client` | Monitoring (Phase 7) | 📋 planned |
| Types | `src/utils/types.py` | `msgspec` | All modules | ✅ working |
| Storage (SQLite+Parquet+Redis) | `src/data/storage.py` | `sqlite3`, `pyarrow`, `redis` | All modules | ✅ working; `Storage(db_path=...)` kwarg supports per-engine DB isolation (Session 23 D1) |
| Main Entry Point | `src/main.py` | All data layer, warmup, heartbeat, M3S (disabled default) | -- | ✅ working (warmup → live feed, heartbeat, graceful shutdown, M3S wired sub-phase 0.8); Session 23 D1: `--broker` + `--engine-name` CLI args for dual-engine topology; reads `config/active_broker.toml`; auto-disables uvloop when broker=ic_markets_ctrader (Twisted incompatibility) |
| Heartbeat Monitor | `src/monitoring/heartbeat.py` | `orjson`, `asyncio` | TradingEngine | ✅ working (60s heartbeat.json, stale detection, kill file watcher, signal/exception metrics, WARNING status); `Heartbeat(heartbeat_path=...)` kwarg for per-engine file isolation (Session 23 D1) |
| Meta-label gate watcher | `scripts/meta_label_shadow_gate_watch.sh` | bash + python | launchd 6h (`com.algo-trading.meta-label-shadow-check`) | ✅ Session 23 D1 — runs shadow check, evaluates phase-5 live gate locally (HEALTHY + any strategy with PASS rolling_auc ∧ n≥20), fires one-shot osascript notification on BLOCKED→READY transition, idempotency via `data/meta_label_gate_cleared.flag` |
| Watchdog | `scripts/watchdog.py` | stdlib only + `osascript` notify | launchd | ✅ v3 2026-06-08 — adds `DATA_BLIND` status (`uptime_s > 1800` AND `tick_count == 0` → WARN log + macOS notify, NO kick; kicking won't recover auth failures). v2 (2026-05-05) basis: multi-engine (`heartbeat.json` + `heartbeat_gold.json`); `launchctl kickstart -k gui/<uid>/<label>` instead of broken PID-file kill; candle-stale threshold 7200s (1h candles need 2× grace); macOS notification on STALE; 600s kick cooldown |
| Orphan Position Closer | `scripts/operational/close_orphan_positions.py` | sqlite3 stdlib | manual + ops | ✅ Session 24 — synthetic close for paper positions whose owning strategy got disabled. Computes net P&L per `paper_executor._close_position` math, INSERTs closing trade row + DELETEs from `paper_positions` + UPDATEs `paper_equity`. Gated by `--apply` (default dry-run); refuses if engines running; writes timestamped DB backup before mutation. |
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

### Order Flow / CVD Path (research — offline, crypto only)
```
Binance Data Vision daily aggTrades ZIP  (free, no API key)
  --> agg_trades.parse_agg_trades()  (positional cols, µs→ms normalize)
    --> cvd.compute_delta_bars()     (bucket by TF_MS, cumulative CVD)
      --> ParquetStore "<SYMBOL>_<tf>_cvd.parquet"
        --> cvd.detect_divergences() / cvd.detect_absorption()
          --> scripts/cvd.py {fetch,show,screen}          (terminal)
          --> dashboard page 8 via flow_charts.price_cvd_panels()  (browser)

Live equivalent: binance_ws @trade (real is_buyer_maker) --> cvd.CVDCalculator.update(tick) --> DeltaBar
NOT available on the gold book: IC Markets CFD ticks carry no size/aggressor — see Known Gotchas 2026-09-21.
```

### Depth / Heatmap Path (research — live recording only, crypto only)
```
binance_ws @depth@100ms (diff stream)   +   REST /api/v3/depth (snapshot)
  --> order_book.OrderBookState         (sequence-checked; DESYNCED on a gap)
    --> feed.on_depth(OrderBookSnapshot)
      --> depth_recorder.DepthRecorder  (decimate to sample_ms, top N levels)
        --> DepthStore "data/depth/<SYMBOL>_depth.parquet"   (long-form rows)
          --> depth_recorder.to_heatmap_grid()   (tick-aligned price buckets)
            --> scripts/depth.py {record,show,heatmap}       (terminal / PNG)
            --> dashboard page 8 via flow_charts.depth_heatmap()  (browser)
```

### Level-Gated Flow Path (advisory — crypto only)
```
config/levels.toml  (YOU write these, before price arrives)
  --> level_registry.LevelRegistry        (hot-reload, first_seen_ms preserved)
       │
binance_ws @trade ──> FlowEngine.on_tick()          HOT PATH ~0.9 µs/tick
       │              └─> ZoneMonitor per level  (state machine)
       │                    └─> ZoneAccumulator  (delta, absorption, excursion)
       │                          └─> evidence.score_zone()  FOR / AGAINST
       ├─> SignalLog          signals + baselines + outcomes (JSONL, off hot path)
       └─> LiveServer         5 Hz WebSocket push -> http://127.0.0.1:8760

binance_ws @depth ──> OrderBookState --> ZoneMonitor.on_book()  (optional, --depth)

Advisory only: emits FlowSignal objects, never orders. No auto-execution path exists.

CVD sees AGGRESSIVE flow (who crossed the spread); depth sees PASSIVE resting
liquidity. Neither sees what the other sees — that is why both exist.
No historical backfill is possible: Binance archives trades, not order books.
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
| `strategy_graveyard` | graveyard.record_kill() via scripts/ingest_obituaries.py | graveyard.list_graveyard() / get_graveyard_entry() | Registry of killed strategies (name, version, kill_date, category, root_cause, revival_conditions, revival_attempts_json) — queryable by category/tag |
| `strategies` | storage.record_deep_backtest_result() (auto via run_deep_backtest hook) + scripts/strategies.py register | storage.list_strategies() / get_strategy() + scripts/strategies.py list/show | Parent-level versioned registry for LIVING strategies — runtime complement to graveyard. Fields: name (unique), family, tier, base_class, markets_json, status (researching/deployable/deployed/killed), tags_json. WAL + FK pragmas enforced. |
| `strategy_versions` | storage.record_deep_backtest_result() (auto) + scripts/strategies_backfill.py | storage.list_versions() / get_version() + scripts/strategies.py show + dashboard page 6 | Per-variant data. One row per (strategy_id, version_slug). Fields: leverage_mode, baseline_leverage, timeframe, params_json, verdict, **max_return_pct + max_return_cell_json (absolute-max from matrix, task #117 G.8)** + max_return_sane flag + max_return_warning, best_calmar_* (existing best_cell), wf_continuous_* (schema v2 task #119 — denormalized walk-forward continuous return/dd/Calmar/gate/folds), backtest_run_id (schema v4 task #124 — nullable FK to backtest_runs for future drill-down), report_dir + report_html_path (relative to REPO_ROOT), last_backtested_at. Slug scheme: `{mode}_L{int(baseline)}_{tf}` + 6-char param-hash collision fallback. |
| `strategy_version_runs` | storage._append_version_run() inside record_deep_backtest_result() + backfill | storage.list_version_runs() + scripts/strategies.py history + dashboard page 6 history expander | Append-only audit log (schema v5 task #123). One row per `run_deep_backtest()` call. Lets the UI show "3 runs on this version this month" + detect regressions where a slug's max-return drifted. Indexed on (version_id, run_timestamp). |

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
| 2026-09-22 | **Absorption is not the entry — the TURN is.** A defender can absorb for twenty minutes and then step away, which is exactly what makes "absorption = buy" a losing rule (region C→D of the teaching chart did precisely this). `ZonePhase` models the sequence explicitly: aggression arrives → it fails (ABSORBING) → the aggressors are trapped and price reverses (TURNING). A signal fires only on the transition to TURNING. Verified live: score reached 0.89 during absorption and correctly held fire, then fired at 0.936 once flow flipped. `require_turn=False` keeps the old behaviour as a comparison arm so the two can be judged on logged outcomes. |
| 2026-09-22 | **Probe volume means volume traded DURING a probe, not between probes.** The first implementation accumulated volume across the retrace, so a light retest reported a ratio of 74x instead of 0.08x — inverting the strongest signal in the system. A probe opens when price reaches the zone extreme and closes when it retraces away; a continuous push to new lows stays ONE probe, because that is one sustained attack. The open probe is included in the ratio: a retest happening right now is what you want to see, not something to wait out. |
| 2026-09-22 | **At a SUPPORT level the bullish evidence is aggressive SELLING that fails to move price** — not buying. Sellers hammer the bid, a passive buyer absorbs, price holds. Invert this sign and the system buys support only once buyers are already lifting the ask, i.e. at the worst available price, while calling it "confirmation". `features.py` documents it and `test_features_evidence.py::TestAbsorptionSign` pins it in both directions for support and resistance. |
| 2026-09-22 | **Absorption is maximal on a near-empty zone unless it is damped.** With three trades the price excursion is ~0, so `|delta_ratio| × (1 − range_ratio)` returns ~1.0 — arithmetically true, completely meaningless. `sufficient` already blocks *signalling* on thin zones, but the score is also rendered live, and a confident 0.95 built on three prints is exactly the false authority this system exists to avoid. Fixed with a trade-count confidence ramp in `ZoneAccumulator.features()`. Caught by a test, not in review. |
| 2026-09-22 | **Streamlit is the wrong tool for a live operator view.** It re-runs the whole script per refresh (1–3 s). For a page you watch while price is inside your level, that lag makes every number stale. `src/flow/live_server.py` serves a static page once and pushes state over a WebSocket at 5 Hz instead; the Streamlit dashboard keeps the post-hoc analysis, where lag does not matter. |
| 2026-09-22 | **`pkill -f <pattern>` matches the shell running it.** A command whose own text contains the pattern kills its own shell mid-script — silently, so a heredoc later in the same command never runs and the file it was writing simply does not appear. Use a more specific pattern, split the string, or match on pid. |
| 2026-09-21 | **`ParquetStore` cannot store depth — it deduplicates on `timestamp` alone.** `storage.py` `save()` does `drop_duplicates(subset=["timestamp"])`, which is right for OHLCV and flow bars (one row per bar) and catastrophic for depth, where every sample writes dozens of rows sharing one timestamp. Routing depth through it keeps ONE price level per sample and silently discards the rest; the resulting heatmap looks plausible and is almost entirely missing. `depth_recorder.DepthStore` exists solely to dedupe on the composite key `(timestamp, side, price)`. |
| 2026-09-21 | **A desynced order book does not fail loudly — it drifts.** Binance depth streams send diffs, not snapshots, so a single missed update leaves the book permanently wrong while it keeps answering queries normally. `OrderBookState` checks contiguity on every event (spot: `U <= lastUpdateId+1`; USD-M futures: `pu == lastUpdateId`), flips to DESYNCED on a gap, and then returns `None` from `snapshot()` until it is re-bootstrapped from REST. Refusing to serve is the point: a stale book is worse than no book. A reconnect also invalidates every book, so `binance_ws.start()` resets and re-bootstraps rather than resuming. |
| 2026-09-21 | **Heatmap price bins must align to the instrument's tick size.** Equal-width bins that do not divide the tick put two price levels in some rows and one in others, which renders as moiré banding that reads like real liquidity structure. `to_heatmap_grid` infers the tick (modal gap between adjacent distinct prices) and buckets on it, falling back to equal-width bins only when the price range is too wide to fit the row ceiling — and it reports which one it used. |
| 2026-09-21 | **CFD/spot-FX feeds cannot produce CVD, footprint or delta — there is no tape.** `icmarkets_feed.py:412-418` builds every `Tick` with `quantity=0.0, is_buyer_maker=False` because a CFD is bilateral: no central book, no trade size, no aggressor side. Candle `volume` from cTrader trendbars (`icmarkets_feed.py:472`, `tb.volume`) is **tick count, not contracts**, so anything volume-derived on the gold book (including `vpin.py`'s Bulk Volume Classification) reads noise. `CVDCalculator` detects this (all-zero-quantity ticks past `DEGENERATE_CHECK_AFTER`) and logs `cvd_degenerate_feed` once rather than emitting a flat, plausible-looking zero series. Real CVD on gold requires COMEX GC/MGC futures via IBKR tick-by-tick or Databento GLBX.MDP3 — NOT XAUUSD. |
| 2026-09-21 | **Binance Data Vision dumps are not one stable format.** Headerless before ~2024 and headered after; spot has 8 columns, USD-M futures 7 (no `is_best_match`); some 2025-onward datasets switched `transact_time` from **milliseconds to microseconds**. `agg_trades.py` assigns columns positionally by count and downscales any timestamp above 1e14. A µs timestamp read as ms lands the trade ~55,000 years in the future and silently produces one garbage bucket per day. |
| 2026-06-24 | **Paper positions are keyed by `(strategy, symbol)`, not `symbol` (Session 30)**: enabling `adaptive_momentum` to run *alongside* `vol_momentum` on the same coins exposed that `PaperExecutor._positions`, `RiskState.open_positions`, and the `paper_positions` table were all keyed by symbol alone. Two strategies on one symbol → the 2nd entry was silently rejected (`position_exists`) and a CLOSE matched by symbol could close the *other* strategy's position. Fixed by composite `(strategy, symbol)` keys (commit `4d0f6f6`): `paper_positions` PK is now `(strategy_name, symbol)`; `update_prices` marks *all* strategies holding a symbol; exposure/leverage already aggregate across positions so same-symbol exposure sums correctly. The **live Binance-futures / IC-Markets executors are intentionally NOT changed** — real venues net to one position per symbol, so symbol keying is correct there; this is paper-only. Migrate existing DBs with `scripts/operational/migrate_positions_composite_pk.py` (idempotent, backs up). The gold book had the same latent bug (donchian_gold + vol_momentum_gold both XAUUSD) — now also fixed. |
| 2026-06-24 | **Restarting the gold engine re-triggers the Session-29 warmup re-staleness**: any `launchctl kickstart` of `engine-gold` runs a fresh warmup; if `XAUUSD_1h.parquet` is >60h stale (it re-stales ~2.5d after each manual refresh) the Binance download 400s and warmup loads 0 candles → donchian_gold starves. Mitigation until the durable fix lands: run `python3 scripts/download_xauusd.py --years 2.0` (full 2y — REPLACE semantics, never a small `--years`) *before/after* restarting gold. Session 30 hit this and restored it (warmup loaded 300). **Now automated:** `com.algo-trading.xauusd-refresh` launchd job (`scripts/launchd/`) runs the 2y refresh daily at 21:17 local, keeping the cache <24h fresh so a restart never starves warmup. cTrader trendbar backfill remains the ideal long-term fix. |
| 2026-06-16 | **donchian_gold could never fire — warmup loaded too few candles for the 120-period Donchian (FIXED Session 29)**: gold has no live warmup download fallback (Binance `/klines` 400s on `XAUUSD` — not a Binance symbol), so gold warmup depends 100% on `data/historical/XAUUSD_1h.parquet`, which only refreshes when `download_xauusd.py` is run by hand (last: 2026-06-12, last bar 06-12 00:00). The recency gate tolerates stale cache only to `max(10 bars, 60h)`. The 06-14 17:27 boot saw `cache_age_min=3927.9` (65.5h > 60h tolerance) → `warmup_skipped_stale_cache` → **0 warmup candles** loaded into the FeatureEngine (the two 06-12 boots, age 657/1322 min, loaded 200 each). A cold FeatureEngine means `DCH_55`/`DCH_120`/`ADX_14` are NaN → `DonchianEnsembleStrategy.on_features` hits its `any(pd.isna(v))` warmup guard and returns `None` every bar. **The deeper bug:** `_compute_indicators` does not emit the `DCH_120`/`DCL_120` columns *at all* until the frame holds **≥245 bars** (measured: 240 fails, 245 passes — ~2× the 120 window). But `warmup(min_candles=200)` loaded only the last 200, and `DonchianEnsembleStrategy`'s NaN-guard requires *all three* channels (20/55/**120**) non-NaN — so even a fully-warm engine running for days only armed if its in-memory buffer organically grew past 245 (45+ uninterrupted live candles on top of warmup) before the next restart reset it to 200. With cold starts (0) + frequent restarts, it **never reached 245** → zero signals since 2026-04-29. (`COMPUTE_WINDOW=250` clears 245 by only 5 bars — fragile; a longer Donchian period would silently re-break this.) Live high 4360.82 on 06-15 16:00 (NY session, ADX~32) very likely broke the 55-bar high and would have been a LONG — missed because channels were NaN. **Class of bug is the inverse of the 06-12 fix:** Session 28 added the gate to stop a stale tail *poisoning* momentum; on the gold leg (no download fallback) the same gate now *starves* a breakout strategy that doesn't care about a 3-day-old tail. **FIX (Session 29, applied):** (1) `download_xauusd.py --years 2.0` refreshed `XAUUSD_1h.parquet` (last bar 06-12 00:00 → 06-15 00:00, age 65.5h→19.4h, back inside the 60h tolerance — old file backed up to `data/historical/XAUUSD_1h.parquet.bak.20260616`); (2) `src/data/warmup.py` `min_candles` **200 → 300** (must exceed the 245-bar DCH_120 floor with margin). Restart loaded **300** warmup candles (was 0/200) → DCH_120/DCL_120/ADX_14 valid on the first live candle → donchian_gold evaluates every bar again. **Still open (durable):** the cache re-stales ~2.5 days after each manual `download_xauusd.py` run because gold has no auto-refresh — the real fix is **cTrader trendbar warmup backfill** (the engine already receives those bars live; kills the Binance-only dependency — Session 28 open item). Cheaper alternatives: per-strategy-class staleness tolerance (breakout channels tolerate far staler tails than momentum), or a daily launchd cache-refresh job. |
| 2026-06-12 | **Warmup trusted cache row-count, not recency — vol_momentum traded the gap to April for 3 weeks (Session 28)**: `src/data/warmup.py` checked only `len(df) < min_candles` before feeding the parquet cache through FeatureEngine → StrategyRouter. The crypto 1h parquet froze 2026-04-17 (nothing in the live path refreshes `data/historical/`); with watchdog kicks restarting engines 2–3×/day, every restart re-primed vol_momentum's 168-bar `_closes` deque with April prices, so live "momentum" = current price ÷ April price − 1 — permanently negative after the May crypto decline → strategy structurally locked SHORT (54/56 trips), churning on noise with fees eating 2/3 of the loss. Verified arithmetically: observed signal `momentum` matched the stale-gap math per symbol. **Secondary bug:** warmup replayed candles through `router.on_features`, which logged every replay signal to the live `signals` table at stale prices (472 phantom rows purged via `scripts/operational/purge_warmup_phantom_signals.py`; phantom = hour-offset ≥ 2min AND no `signal_audit` match — audit only writes on the live path; on gold, offset alone would have misclassified ~200 live rows because cTrader trendbars arrive minutes late). **Fixes:** recency gate (cache must be ≤3 bars old; stale → download + persist back; download unavailable → tolerated to `max(10 bars, 60h)` (gold weekend); beyond → skip warmup LOUDLY); router `_storage` detached during replay. **Lessons:** a cache with enough rows is not a cache with the right rows; replay paths must be side-effect-free; frequent restarts turn a transient data bug into a chronic one. Full writeup: `docs/investigations/2026-06-11_vol_momentum_stale_warmup.md`. |
| 2026-06-12 | `scripts/download_xauusd.py` **REPLACES** the target parquet — it does not merge with existing rows. Running it with a short `--years` window silently truncates the 2-year history that backtests depend on (bitten 2026-06-12: `--years 0.2` cut `XAUUSD_1h.parquet` from 11,826 rows to 1,195; recovered by re-running `--years 2.0`). Always re-download the full window, or merge via `ParquetStore.save` semantics. |
| 2026-05-11 | macOS idle-sleep silently suspends Python engine processes when the Mac dozes (pmset `sleep 1` = 1-min idle timeout). When suspended: heartbeat writer stops → Binance WS socket TCP times out → on wake, watchdog sees `hb_age > 360s` and `launchctl kickstart -k`'s the engine. 516 sleep/wakes in 5 days observed before fix. Engine restarts every 30–90 min wiped vol_momentum's `_closes` deque on each boot, preventing entry-condition accumulation. **Fix: wrap all launchd `ProgramArguments` with `/usr/bin/caffeinate -i`** — caffeinate holds a `PreventUserIdleSystemSleep` assertion for the lifetime of its child Python process. *(2026-06-12 Session 28 addendum: `-i` does NOT block lid-close/forced sleep — heartbeats still went stale 600–8,000s with kicks 2–3×/day. Flags now `-i -s`; `-s` also blocks system sleep while on AC power. Battery + lid-close can still sleep — residual restarts are harmless post warmup-recency fix.)* Applies to `com.algo-trading.{engine, engine-gold, risk-server, watchdog}` plists. Verify with `pmset -g | grep sleep` (should show "sleep prevented by caffeinate, …"). Plist backups at `~/Library/LaunchAgents/.bak.20260511/`. |
| 2026-05-09 | FatFingerGuard pre-fix held a single GLOBAL running average across all (strategy, symbol) pairs. With strategies running at order-of-magnitude-different qty scales (vol_momentum/DOTUSDT ~482 vs funding_carry/BTCUSDT-CARRY ~17), one tiny fill from the small-qty pair after a state reset Welford'd the avg down (e.g. 14.235 with count=2 from a 5.679-qty fill) and locked out 100% of large-qty signals for 11 days — silent, no alerts. Fix: refactored to `RiskState.fat_finger_avg_pairs: dict[strategy/symbol, (avg, count)]` + 5-fill warmup before the qty check engages. Backfilled from `trades` table via `scripts/operational/migrate_fat_finger_per_pair.py`. **Original Session 22 Day 5 fix (commit `e36e075`) made the avg survive restarts but did NOT address cross-pair contamination — that was Session 25.** |
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
| 2026-04-14 | TV chart export `time` column has TWO formats: legacy unix seconds (numeric) AND newer ISO 8601 strings with timezone offset (e.g. `2025-12-29T04:30:00+05:30`). `load_tv_chart_csv()` auto-detects the format. Always use the resulting `dt` column (UTC pd.Timestamp) and `timestamp` column (UTC ms int64) — never trust the raw `time` column. |
| 2026-04-14 | TV xlsx strategy report has 5 sheets (Performance, Trades analysis, Risk-adjusted performance, List of trades, Properties) and the "Date and time" column is `datetime64[ns]` in the **chart's display timezone** (Prince's TV is set to IST = UTC+05:30). The xlsx does NOT carry tz info. `load_tv_trades_xlsx(path, tz=...)` requires the caller to specify the tz. CLI auto-detects from the chart CSV's ISO 8601 offset via `detect_chart_tz_from_csv()`. |
| 2026-04-14 | TV's strategy tester counts each `strategy.exit()` partial fill as a separate "trade". A 3-tier TP ladder (TP1=50%, TP2=30%, TP3=20%) where one entry creates 3 partial closes appears as 3 rows in the xlsx List of trades sheet, all sharing the same `(entry_ts, side, entry_price)`. `_collapse_ladder_legs()` groups them back to ONE logical trade per entry signal so the count matches the chart's `Long`/`Short` plotshape markers (1607 legs → 1231 unique entries on the SWIFT 108-day export). |
| 2026-04-14 | Pine Script's `res = input.timeframe('15')` input value is **misleading** — at runtime, Pine's `request.security(symbol, "{intRes×res_in_min}", ...)` actually uses `timeframe.in_seconds(timeframe.period)` (the CHART's TF), NOT the input value. The xlsx Properties sheet `'TIMEFRAME' = '15'` reflects the input default, not the runtime alt-TF. Verified empirically against SWIFT: Properties said res=15 + mult=8 = 120, but data showed alt_tf=40 = chart_5min × mult_8. `extract_pine_config_from_xlsx()` uses `chart_tf × multiplier` for `alt_tf_min`, falling back to `pine_res × multiplier` only when chart_tf is unknown. |
| 2026-04-14 | TV chart Long/Short markers can fire INSIDE an alt bar (not just at boundaries) due to intrabar mechanisms — most likely SL hits causing position closure followed by immediate re-entry at the next M5 bar via the alt-TF lookahead value. On SWIFT 108-day Vantage export, 3 of 1231 entries (0.24%) fired at non-aligned positions: 2026-02-23 01:45, 2026-04-14 14:55, 2026-04-14 15:00. These are unavoidable noise in the lookahead replication, NOT port bugs. The 99.84% bar-by-bar match is the ceiling for this kind of intrabar-interaction Pine script. |
| 2026-04-14 | **CRITICAL — cost model 90× slippage bug discovered + fixed (task #105)**. `SpreadSlippageConfig.atr_vol_mult = 0.5` (default since task G.2a.2) scaled slippage as half of bar ATR. With XAUUSD M5 median ATR $7.08, this produced 35.6 pips of slip per fill — `slip_pips = 0.2 + 0.5 × (7.08 / 0.10) = 35.6`. Real-world IC Markets ECN slippage on gold is 0.1-0.5 pips. ECN slippage is bounded by **order book depth**, not by bar volatility — the ATR scaling was a port from a market-maker model and never made sense for raw cTrader fills. On SwiftAlmaStrategy 108-day backtest, the bug charged $608.68/trade slippage vs real $6.75/trade, producing fake −49% return where real result is +13.97% (cTrader) or +20.28% (MT4). Fixed by setting `atr_vol_mult=0.0` and recalibrating spread/slip defaults from researched broker numbers (see costs.py docstring). **Affects EVERY gold strategy backtest run since G.2a.2** — donchian_gold, vol_momentum_gold, all 3 Tier 5 strategies, and SWIFT may have been wrongfully convicted. The graveyard entries should be re-evaluated with the corrected model. Regression test in `test_costs.py::test_atr_no_longer_inflates_slippage` + sanity guard `total < 100` prevents recurrence. |
| 2026-04-14 | **IC Markets gold commission: cTrader is ~3.86× more expensive than MT4** at $4500/oz. MT4 charges fixed $3.50/lot/side. cTrader charges $3 per $100,000 notional traded — for gold @ $4500/oz × 100 oz = $450k notional → $13.50/lot/side ($27 round trip vs MT4's $7). For FX where 1 lot ≈ $100k, both are equal. Per CLAUDE.md we deploy on cTrader, but **for gold-heavy strategies MT4 saves significant fees** and should be considered. `make_ic_markets_mt4_xauusd_schedule()` and `make_ic_markets_ctrader_xauusd_schedule()` factories in `costs.py`. The `commission_usd()` function requires `reference_price` for volume-based schedules to compute notional. Verified against IC Markets official spreads page (2026-04-14 task #105 research pass). |
| 2026-04-14 | **Cost-fix sweep results (task #107)** — re-evaluated 5 gold strategies under corrected `ic_markets_ctrader_xauusd_normal` profile vs the pre-fix `atr_vol_mult=0.5` baseline on 2yr Dukascopy XAUUSD. Findings: (1) **`donchian_gold` jumped from +38.16% to +107.36%** — the bug was hiding 69pp / ~3× of real performance. (2) **`vol_momentum_gold` jumped from +20.27% to +53.25%** — 33pp / ~2.5× understated. (3) The 3 graveyard strategies (`candle_burst_hunter`, `news_spike_fade`, `hedged_structure_play`) all stayed dead even with corrected fees — bug distortion 2-23pp but their structural issues (over-trading, peak-miss, regime mismatch) remain dominant. Bug distortion scales with TRADE COUNT × LEVERAGE: low-frequency low-leverage strategies (donchian/vol_momentum at 25x, ~70-120 trades) saw the biggest absolute distortion per trade (~$30-100/trade phantom cost), while high-frequency high-leverage scalpers (3829 trades at 500x) saw small per-trade distortion but already had structural negative gross alpha. Report at `reports/cost_recheck_2026-04-14.md`. Revival attempts appended to graveyard for the 3 dead strategies. Production strategy margins are now defended by honest baselines for paper trading. |
| 2026-04-15 | **`book.open_position` margin-check float precision bug (task #109)** — `if entry_margin > current_free_margin` rejects positions where `notional == equity` at 1x leverage due to ULP differences between the two equal-value floats. SwiftAlmaStrategy is most affected because its default `risk_pct == sl_pct == 0.005` produces `notional = equity × (risk/sl_pct) = equity × 1.0` exactly. At 1x leverage, that's margin == free_margin, which the engine rejected with "need margin $10000.00, have free margin $10000.00" (visually equal but float-not-equal). The bug ONLY fires for `pine_zero_cost` fee mode because spread-bearing fee models slightly perturb fill_price, breaking the exact-equality. Symptom in matrix #109: pine_zero produced 77 trades while cTrader/MT4 produced 307 on the same data — 4× discrepancy. Fixed in `src/backtest/book.py` line ~386 by adding 1-cent epsilon: `if entry_margin > current_free_margin + 0.01`. Regression tests in `tests/test_backtest/test_book.py::test_margin_exactly_equal_to_free_margin_succeeds` + `test_margin_just_over_free_margin_still_rejects`. The fix unblocks the SwiftAlma matrix and ANY future strategy that uses `risk_pct == sl_pct` sizing at 1x leverage. |
| 2026-04-15 | **Leverage semantics: `engine.run(leverage=N)` is a max-margin cap, NOT a position multiplier (task #110)** — When a strategy uses risk-based sizing where `notional = equity × (risk_pct/sl_pct)`, the position quantity is leverage-INDEPENDENT and P&L is therefore identical at every leverage. SwiftAlmaStrategy default config (`risk_pct == sl_pct == 0.005`) produces `notional = equity × 1.0` exactly, so the 240-cell matrix #109 showed bit-perfect identical P&L across 1x/50x/100x/500x/1000x. This is correct CFD broker accounting, not a bug. Validated 4 ways: (1) hand-traceable single trade match to 10+ decimals, (2) 7 unit tests in `tests/test_backtest/test_leverage_invariance.py` lock the invariance, (3) 1536-assertion audit of matrix #109's JSON data file (all pass), (4) counter-demo with `risk_pct = 0.005 × N` proves framework produces leverage variance when strategy opts in (scale 1×=+13.55%, scale 5×=+73%, scale 50×=−90% wipeout). The matrix's leverage axis IS meaningful in the `cost_pct_of_margin` column which scales linearly with leverage (0.006% @ 1x → 6.002% @ 1000x cTrader). Validation report at `reports/swift_leverage_validation_2026-04-15.md`. **Lesson**: any strategy using `risk_pct == sl_pct` will be leverage-invariant for P&L. To make leverage matter for P&L, either raise `risk_pct` independently of `sl_pct`, or use fixed-quantity sizing instead of risk-based. |
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
| 2026-04-14 | G.5b — `M1PathModel` in `src/backtest/path.py` provides tick-accuracy intrabar SL/TP ordering using 1-minute sub-bars as ground truth. Validated 100% ordering agreement + 0.0000 mean timing error vs 71.89% / 0.2427 for `BrownianBridgeModel` on 2000-bar samples from 2yr XAUUSD M5. Use `get_or_build_m1_path_model("data/historical/XAUUSD_1m.parquet", sub_bar_count=N)` (cached, loads once per process) — sub_bar_count = 5 for M5, 60 for H1. Pass to `LeveragedBacktestEngine(path_model=...)` as opt-in; default stays `BrownianBridgeModel` to preserve existing baselines. Recommended for any scalper with SL distance < 2× ATR (where bridge random-guess bias matters most). |
| 2026-04-14 | Tier 5 M1 revival failed (task #100). After G.5b unlocked tick-adjacent resolution, `candle_burst_hunter` and `news_spike_fade` (with new `require_peak_reversal=True` mode) were rerun at M1 cadence on 350k bars (12mo XAUUSD). `candle_burst`: all 6 configs -100% (ATR_20 on M1 is ~0.5-1.5, so the `travel > burst_atr_mult × ATR` filter degenerates into "every bar qualifies" — 1k-17k trades per config). `news_spike_fade`: all 7 peak-reversal configs -21% to -47% (reversal trigger fires on normal 2-5 pip M1 noise inside news windows, not on genuine spike stalls). Root cause for both: M1 resolution rescues the _timing_ problem but not the _signal quality_ problem. Need fundamentally different signal architectures (multi-bar velocity, volume confirmation, or real tick-level delta-price/delta-time). Obituaries amended in strategy docstrings. Aggressive sub-book stays empty. Results in `data/tier5_m1_revival.json`. |
| 2026-04-14 | Strategy graveyard (task #101): `src/strategies/graveyard.py` module + `strategy_graveyard` SQLite table inside `data/trades.db` holds structured kill records (name, version, kill_date, category ∈ {TIMING,NOISE,PREMISE,INFRA,REGIME}, root_cause_long, revival_conditions, research_artifacts_json, revival_attempts_json). Idempotent upsert on `(strategy_name, strategy_version)`. `scripts/ingest_obituaries.py` parses obituary docstrings of the 3 killed files and populates the table — 3 rows on first run, idempotent on re-run. Query patterns: `list_graveyard(category='NOISE')`, `list_graveyard(tag='tick-blocked')`, `append_revival_attempt(name, attempt_dict)` after a revival research sprint. The obituary docstrings in the strategy files remain the source of truth; the graveyard table is a cached/queryable projection. |
| 2026-04-14 | Scalper primitives (task #102): feature_engine parser updated to detect integer suffix so multi-underscore names like `volume_zscore_30` parse as name=`volume_zscore`, length=30, while `body_pct` stays as name=`body_pct`, length=None. Six new indicators: `roc_N`, `velocity_N` (requires `atr_20` requested first — silently skipped otherwise), `body_pct`, `close_in_range`, `volume_zscore_N`, `dollar_volume_N`. `M1PathModel._m1` schema upgraded from `(low, high)` tuples to `M1SubBar` named tuples (open, high, low, close, volume). `LeveragedBacktestEngine` now attaches `row["intrabar_sub_bars"]` (list of `M1SubBar`) when `path_model` is `M1PathModel` AND the M1 lookup has coverage for the bar's ts_ms. `ScalperStrategy` base class wraps the pattern: rolling N-bar history buffer + `multi_bar_velocity`/`is_volume_confirmed`/`broke_n_bar_high`/`intrabar_max_velocity` helpers. Strategies running against `BrownianBridgeModel` do NOT see `intrabar_sub_bars` — deliberate to prevent accidental use of uninitialized data. |
| 2026-04-14 | SWIFT port (task #103): ported the trading logic (~50 lines) of TradingView's SWIFTALGO Pine Script (~700 lines). Key finding: **leverage is orthogonal to P&L for risk-sized strategies** — every leverage level within a TF produces identical P&L because position notional is determined by `risk_pct/sl_pct` not by leverage, so leverage only affects required margin. Best Pine-faithful config: 4h × 5x with +52.40% / 4.90% DD / PF 1.60 on 229 trades. Best realistic config: same cell at **-12.47%** because IC Markets Raw commission ($6/lot RT) + spread consume all gross edge — a 65 percentage point haircut. Lesson: **Pine's strategy tester uses 0 commission by default**; any TV-ported strategy must be backtested in BOTH modes to find the true cost breakeven. |
| 2026-04-14 | 3-tier TP ladder via virtual leg accounting (task #103): Pine's `strategy.exit(qty_percent=50 @ TP1)` + `(30 @ TP2)` + `(20 @ TP3)` cannot be directly implemented in our engine (no partial close support). Pattern used: strategy tracks TP1/TP2 hit status internally, computes a WEIGHTED AVERAGE exit price when position finally closes (at TP3, SL, or opposite signal), and emits a single CLOSE signal with the synthetic exit price. Engine sees one position with an exit price that reflects the ladder's correct economics. Verified by `test_long_tp1_then_sl` — TP1 hit + SL exits at +0.25% instead of −0.5%, matching Pine's partial close behavior. Reusable for any future strategy with multi-leg TPs. |
| 2026-04-18 | **Dual-engine topology (Session 23 D1)**: `--broker <id>` + `--engine-name <name>` CLI args on `src.main` let the crypto engine (Binance altcoins + funding_carry) and gold engine (IC Markets cTrader XAUUSD) coexist. Each engine gets isolated `data/heartbeat_<name>.json`, `data/trades_<name>.db`, and `data/m3s_<name>.sqlite`. Risk server is shared (ZMQ multi-client). Strategies filter by broker via `_symbol_belongs_to_broker` (cTrader owns FX/metals; Binance owns crypto USDT pairs). The Binance engine plist keeps its legacy `data/` paths by running without `--engine-name`; gold plist adds `--engine-name gold`. |
| 2026-04-18 | **Twisted + asyncio bridge for ICMarketsFeed (Session 23 D1)**: `asyncioreactor` crashes with "reactor already installed" on second boot because `ctrader_open_api` imports twisted transitively at module load, installing the default SelectReactor before main gets a chance. Working pattern: run Twisted's default SelectReactor in a **worker thread** (`threading.Thread(target=reactor.run, installSignalHandlers=False)`), capture `self._loop = asyncio.get_running_loop()` in `ICMarketsFeed.start()`, and dispatch from Twisted callbacks back to asyncio via `asyncio.run_coroutine_threadsafe(self.on_candle(candle), self._loop)`. Also: cTrader `client.send()` returns a Deferred with a 5s timeout; subscribe requests typically don't resolve that Deferred (responses come via the shared message-received handler), so the timeout cancels into an unrelated callback and bubbles up as `TimeoutError`. Fix: `_send_quiet(client, req)` attaches an errback that swallows cancel/timeout. Finally: uvloop is incompatible with this pattern — gold engine auto-disables uvloop (`if args.broker != "ic_markets_ctrader"`). |
| 2026-04-18 | **ProtoOATrendbarPeriod enum values were wrong in `_TIMEFRAME_TO_PROTO` (Session 23 D1)**: original hardcoded mapping had `1h=8` but cTrader's actual H1 enum value is 9. Every TF above 5m was off-by-one or more. Verified via `ProtoOATrendbarPeriod.items()` and fixed: 1m=1, 5m=5, 15m=7, 30m=8, 1h=9, 4h=10, 1d=12. `ProtoOATrendbar` is delta-encoded: `low` is absolute (×100,000), `high = low + deltaHigh`, `open = low + deltaOpen`, `close = low + deltaClose`; timestamp field is `utcTimestampInMinutes` (bar-open reference, NOT close). `_trendbar_to_candle` computes close-referenced ts_ms by adding period minutes. |
| 2026-04-18 | **`Tick` dataclass field name is `is_buyer_maker`, NOT `exchange` (Session 23 D1)**: generic external-feed code is tempted to stash an `exchange="icmarkets"` tag in the Tick, but `src/utils/types.py::Tick` is a frozen msgspec.Struct with fields `symbol, price, quantity, timestamp, is_buyer_maker`. Adding unknown kwargs raises `TypeError: Unexpected keyword argument`. If exchange provenance must be tracked downstream, add it via a separate channel (strategy metadata, logging context). |
| 2026-05-05 | **cTrader trendbar subscribe silently fails on reconnect, no ack handler in dispatch (Session 24)**: `src/data/feeds/icmarkets_feed.py` sent `ProtoOASubscribeLiveTrendbarReq` for each (symbol × timeframe) but had NO handler for `ProtoOASubscribeLiveTrendbarRes`. Combined with `_send_quiet()`'s debug-level errback, a silent Spotware-side rejection of the trendbar subscribe (likely on Sun 22:27 UTC reconnect) degraded the gold engine to tick-only mode for **87 hours** without any log signal above debug level. Fix: added `ProtoOASubscribeLiveTrendbarRes` import + dispatch (drains `_pending_trendbar_subs` set, logs `icmarkets_trendbar_subscribed` with pending count), `_send_loud()` helper for WARNING-level errbacks, and a 30s `reactor.callLater` audit that logs `icmarkets_trendbar_subscribe_unacked` if pending non-empty. **Lesson generalized:** every external-feed subscribe must log its ACK at INFO. Silent subscribe paths produce STALE engines that don't alert. |
| 2026-05-05 | **Watchdog v2 — multi-engine + launchctl kickstart (Session 24)**: previous `scripts/watchdog.py` only checked one heartbeat (`heartbeat.json`), used a broken PID-file kill mechanism (no PID file ever written by engines — `data/pids/` empty since project start, every CRITICAL alert logged "no engine PID found to kill"), and had `DATA_STALE_KILL_THRESHOLD=1800s` that fires CRITICAL spuriously every hour for 1h-candle engines (candle_age oscillates 0-3600s naturally). Rewrote: iterate over `ENGINES = [{label, heartbeat_path, launchd_label}]`, replace `os.kill(pid, ...)` with `subprocess.run(["launchctl", "kickstart", "-k", f"gui/<uid>/<label>"])`, bump candle threshold to 7200s (2× grace for 1h candles), add macOS `osascript display notification` on STALE, 600s kick cooldown to prevent thrash. The 87h gold STALE outage was structurally invisible to the prior watchdog — even the gold heartbeat path wasn't checked. |
| 2026-06-08 | **cTrader access token expired silently, no auto-refresh + watchdog blind spot (Session 27)**: `src/data/feeds/icmarkets_feed.py` loaded `CTRADER_ACCESS_TOKEN` once at boot via `ICMarketsConfig.from_env()` and never rotated. Spotware access tokens expire ~30 days after issue. The token issued 2026-04-18 (Session 23 D1) expired 2026-05-18 05:12 UTC; for the next 21 days the gold engine looped: app-auth OK → `ProtoOAErrorRes(code="CH_ACCESS_TOKEN_INVALID")` → warning log → reconnect → repeat, 379 token errors. **Watchdog v2 missed it** because `last_candle_age_s` is `null` when no candles ever arrive (the candle-stale kick at `watchdog.py:162` only fires when the field is non-null), and the heartbeat file mtime was fresh (engine event loop alive, just data-blind). Fix: (1) new `scripts/ctrader_refresh_token.py` does a non-interactive `grant_type=refresh_token` exchange and writes the rotated pair back to `.env`; (2) `ICMarketsConfig.refresh_token` field loaded from env; `_handle_token_expired()` branches on `ProtoOAErrorRes.errorCode == "CH_ACCESS_TOKEN_INVALID"`, calls `refresh_tokens()`, persists, updates in-memory config + `os.environ`, and resumes the handshake by re-sending `ProtoOAGetAccountListByAccessTokenReq` — guarded by `_token_refresh_in_flight` + `_token_refresh_failed` so the engine attempts at most one refresh per process lifetime; (3) `scripts/watchdog.py` now reads `tick_count` + `uptime_s` from the heartbeat and emits a `DATA_BLIND` status (WARN log + macOS notify, NO kick) when `uptime_s > 1800` AND `tick_count == 0` — kicking won't recover auth failures, the operator must intervene. **Lesson generalized:** auth that expires on a wall-clock schedule must auto-refresh; "engine alive but receiving zero data past warmup" is its own watchdog signal distinct from `candle_age` (which is undefined when no candles ever arrive). |
| 2026-05-05 | **Disabled-strategy + open-position orphan (Session 24)**: `StrategyRouter.from_config()` at `src/strategies/router.py:109` skips `enabled = false` entries entirely (no instantiation). `paper_executor.restore_state()` at `paper_executor.py:391` reads positions from `paper_positions` table on every boot regardless of strategy enabled-state. Net: disabling a strategy with open paper positions **orphans them** — no `on_candle` fires, no exit signal generated, position sits in `paper_positions` forever, marked-to-market via tick `update_prices` only. Resolution: `scripts/operational/close_orphan_positions.py` synthesizes a close at `current_price` (mirroring `_close_position` math) and writes a closing trade row + DELETE from `paper_positions` + UPDATE `paper_equity`. Gated by `--apply` (refuses if engines running, writes timestamped DB backup before mutation). Used Session 24 to clean up 3 orphans (vol_momentum_gold + 2× donchian_ensemble_adx) for net realized +$11.26. |
| 2026-04-14 | `ZeroCostFeeModel` (task #103): drop-in fee model returning 0 spread, 0 commission, 0 slippage for all queries. Drop-in for `ICMarketsMetalFeeModel`. Used to reproduce TradingView strategy tester numbers (Pine defaults to 0 commission unless overridden). Critical for porting any TV strategy — reading TV backtest results without knowing the cost model underfits the live-trading reality. |
| 2026-04-14 | TV Parity Validation Framework (task #104): reusable Stage 0 validation for Pine Script ports. Library at `src/backtest/tv_parity.py` + CLI at `scripts/tv_parity_validate.py`. Strategy contract: Pine-ported strategies expose a `detect_signals_lookahead(cls, ohlc_df, *, anchor_segments=None, **config)` classmethod that mirrors Pine's `lookahead_on` semantics on the full OHLC DataFrame. The CLI imports any strategy dynamically, detects DST-aware anchor segments from TV's entry timestamps, invokes the classmethod, and matches bar-by-bar against TV's Long/Short entry markers in the chart CSV. Pass threshold: ≥99.0% match. Verified against SwiftAlmaStrategy on 107 days of real Vantage XAUUSD data spanning a DST transition — reproduces 100% (1228/1228) match in a single CLI invocation. Workflow documented in `docs/PINE_SCRIPT_PORT_WORKFLOW.md`. Future Pine ports follow the same pattern: write production `on_features` + validation classmethod, export TV chart data, run validator, iterate until 100%, then proceed to realistic backtest. |
| 2026-04-14 | Vantage XAUUSD alt-TF bars are anchored to EXCHANGE time (New York), not UTC. When US DST starts/ends, the UTC alignment of the alt bars shifts by 60 minutes = 20 min mod 40 min. A backtest window spanning a DST boundary will see TWO different anchor values (e.g., `:20 → :00` at 2026-03-08). `tv_parity.detect_alt_anchor_segments()` auto-detects these transitions by grouping TV's entry-bar timestamps by date, computing each day's modal `timestamp mod alt_tf_ms` value, and collapsing consecutive days with the same modal into segments. Returned as a list of (ts_ms, anchor_min) tuples consumed by `resample_with_dynamic_anchor`. Without this, the bar-by-bar match rate drops from 100% to ~67%. |
