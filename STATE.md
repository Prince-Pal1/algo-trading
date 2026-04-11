# STATE -- Session Continuity Tracker

**Last Updated:** April 11, 2026 -- Session 5

---

## Last Completed Section

Phase 2 (First Strategy + Backtest + Paper Trading) is COMPLETE. Full pipeline validated end-to-end: strategy development, historical data download, backtesting, parameter optimization, and walk-forward validation.

## Current Progress State

| Task | Status |
|---|---|
| --- PHASE 1: DATA FOUNDATION --- | COMPLETE |
| Config system (TOML) | DONE |
| Data types (msgspec) | DONE |
| Structured logging (structlog + orjson) | DONE |
| Binance WebSocket feed | DONE |
| Candle builder | DONE |
| Feature engine (ta library) | DONE |
| Storage (SQLite + Parquet) | DONE |
| Main entry point + TradingEngine | DONE |
| Pipeline smoke test (live Binance) | DONE |
| --- PHASE 2: STRATEGY + BACKTEST --- | COMPLETE |
| BaseStrategy interface | DONE (`src/strategies/base.py`) |
| StrategyRouter | DONE (`src/strategies/router.py`) |
| EMA Crossover strategy | DONE but UNPROFITABLE — deprecated |
| BB+RSI Mean Reversion strategy | DONE (`src/strategies/day_trading/bb_rsi_mr.py`) |
| Historical data downloader | DONE (`src/data/downloader.py`) |
| Backtest engine (event-driven) | DONE (`src/backtest/engine.py`) |
| Walk-forward validator | DONE (`src/backtest/walk_forward.py`) |
| Paper executor | DONE (`src/execution/paper_executor.py`) |
| Executor base interface | DONE (`src/execution/base.py`) |
| Wire into main.py | DONE |
| strategies.toml config | DONE (validated params) |
| Backtest validation on real data | DONE (2 years, 5 symbols, walk-forward OOS) |
| --- PHASE 3: RISK MANAGEMENT --- | NOT STARTED |

## Phase 2 Results Summary

### EMA Crossover (deprecated)
- Tested on 1m, 5m, 15m BTC; 6+ parameter iterations
- Best result: -5.2% return on 15m (PF 0.91)
- Root cause: trend-following in a mean-reverting intraday market
- Lesson: crypto intraday is ~60-70% mean-reverting, not trending

### BB+RSI Mean Reversion (validated winner)
- **Strategy:** Buy at lower BB + RSI oversold, sell at upper BB + RSI overbought, only in flat markets (ADX < 20)
- **Validated params:** RSI 25/75, ADX < 20, SL 3×ATR, 1h timeframe, 0.04% commission
- **Universe:** ETHUSDT, BNBUSDT, ADAUSDT, DOTUSDT, MATICUSDT (BTC excluded — trends too hard)

**Full 2-year backtest (Apr 2024 - Apr 2026):**
| Metric | Value |
|--------|-------|
| Total trades | 56 |
| Win rate | 60.7% |
| Profit factor | 1.452 |
| Net return | +5.4% |

**Walk-forward OOS (Year 2 only):**
| Metric | Value |
|--------|-------|
| Total trades | 25 |
| Win rate | 60.0% |
| Profit factor | 1.513 |
| Net return | +2.7% |

**Individual Sharpe ratios (2yr):** MATIC 1.77, DOT 0.62, BNB 0.47, ADA 0.10, ETH -0.13

### Key Learnings
1. EMA crossover is a lagging trend-follower — wrong tool for mean-reverting crypto intraday
2. ADX < 20 is the critical filter — isolates truly ranging markets where MR has edge
3. RSI 25/75 (strict) dramatically outperforms RSI 30/70 (43% → 68% WR)
4. 1h timeframe gives bigger moves (BBL→BBM) vs 15m where moves are too small vs commission
5. Altcoins (BNB, ADA, DOT) mean-revert better than BTC
6. Trend filters (EMA, DI+/DI-) are logically contradictory with mean reversion entries
7. The winning parameters are NOT the default textbook values — real optimization matters

## Next Step to Execute

**Phase 3 — Risk Management + Portfolio:**
1. Expand symbol universe (20+ altcoins) to increase trade count
2. Multi-timeframe confirmation (1h + 4h)
3. Portfolio-level risk manager (max drawdown, max correlated exposure)
4. Adaptive symbol selection (rolling walk-forward to rotate which symbols to trade)
5. ZeroMQ risk process (separate from trading engine)

## Current Assumptions / Decisions

- Platform: macOS
- Indicator library: `ta` (not pandas-ta)
- Backtesting: Custom event-driven engine (NOT VectorBT) — same code path as live
- Primary strategy: BB+RSI Mean Reversion with ADX regime filter
- Best timeframe: 1h
- Best commission: 0.04% (Binance VIP maker)
- BTC excluded from mean reversion universe (trends too hard)
- Database: SQLite + Parquet (Phase 1-3)
- Phases: Milestone-based, 8 phases total

## Files Modified This Session (Session 5)

1. `src/strategies/day_trading/bb_rsi_mr.py` — NEW: BB+RSI Mean Reversion strategy
2. `src/strategies/scalping/ema_crossover.py` — Added anti-whipsaw filters (cooldown, MACD, min SL)
3. `src/strategies/router.py` — Registered bb_rsi_mr in STRATEGY_REGISTRY
4. `src/backtest/engine.py` — Added max_notional_pct cap, fixed Sharpe calculation (daily resampling)
5. `config/strategies.toml` — Added bb_rsi_mr config, disabled ema_crossover, validated params
6. `STATE.md` — Full Phase 2 results
