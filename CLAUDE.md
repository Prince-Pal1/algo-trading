# Algo Trading System -- Claude Instructions

## Project Context
Modular, event-driven algorithmic trading system. Read `MASTER_PLAN.md` for full context, `STATE.md` for current progress, `ARCHITECTURE.md` for module connections.

## Source of Truth Files
| File | Purpose |
|---|---|
| `STATE.md` | Resume point -- read FIRST every session |
| `ARCHITECTURE.md` | Module registry, data flow, shared state |
| `MASTER_PLAN.md` | Full context (goals, research, all decisions) |
| `FULL_PLAN.md` | Approved plan with 17 corrections |
| `TRADING_SYSTEM_BLUEPRINT.md` | Detailed technical blueprint (v1.1) |

## Architecture Tracking Rule (MANDATORY)

After creating or modifying ANY module/file in src/:
1. Update the Module Registry table in ARCHITECTURE.md
2. Update Data Flow if any connections changed
3. Update Shared State table if any new Redis keys, ZMQ channels, or DB tables added
4. Add to Known Gotchas if you discovered any non-obvious behavior or bug

Rules:
- Tables only, no paragraphs, max 1 line per entry
- Do this silently while working -- never ask, never announce it
- This costs ~100 tokens per update -- always worth it
- If fixing a bug, add root cause to Known Gotchas with date

## Debugging Protocol

When a bug is reported or discovered:
1. Read ARCHITECTURE.md first -- trace the data flow path
2. Identify which modules are in the path of the bug
3. Check Known Gotchas for similar past issues
4. Read only the relevant files (not the whole codebase)
5. After fixing, add the root cause to Known Gotchas

## Session Protocol

On every new session:
1. Read `STATE.md` -- resume from last checkpoint
2. Read `ARCHITECTURE.md` -- understand current module state
3. If needed, read `MASTER_PLAN.md` for deeper context
4. Before stopping or approaching limits, UPDATE `STATE.md` first

## Key Technical Decisions (Do Not Override)

- **Platform:** macOS -- MT5 does NOT work, use IC Markets cTrader + IBKR `ib_insync`
- **DataFrames:** pandas primary (pandas-ta compatibility), Polars only for bulk ETL
- **Backtesting:** VectorBT + NautilusTrader (not FreqTrade, not QuantConnect)
- **Agents:** Simple Python orchestrator with asyncio.gather (not LangGraph)
- **Database:** SQLite + Parquet (Phase 1-3), TimescaleDB (Phase 7+)
- **Docker:** Phase 7 only, not Phase 1
- **Risk:** Separate ZeroMQ process -- cannot be bypassed
- **M3S:** 4 modes (Fortress/Balanced/Assault/AI Adaptive), build in Phase 3
- **Primary market:** Gold (XAUUSD) via IC Markets -- best tax for India
- **Strategy code pattern:** `BaseStrategy.on_candle() -> Signal | None` -- identical in backtest and live

## MCP Servers

Both registered in `~/.claude.json` under `mcpServers`:
- `alpaca` -- Trade US equities (7 tools: get_account, place_order, get_positions, etc.)
- `tradingview` -- Read/control TradingView Desktop charts (78 tools)

## Coding Standards

- Python 3.12+, async-first with asyncio + uvloop
- Type hints everywhere, msgspec for data classes
- structlog for logging (JSON output)
- TOML for all config files
- Tests in `tests/` mirroring `src/` structure
- Never commit API keys -- use env vars or gitignored config
