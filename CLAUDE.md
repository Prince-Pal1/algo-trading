# Algo Trading System -- Claude Instructions

## Project Context
Modular, event-driven algorithmic trading system. Read `MASTER_PLAN.md` for full context, `STATE.md` for current progress, `ARCHITECTURE.md` for module connections.

## Source of Truth Files

Each fact lives in exactly ONE file. Redundancy causes drift. When you need information about X, look in the file listed here — and nowhere else.

| Concern | File |
|---|---|
| Phase status, milestones, next action | `ROADMAP.md` |
| Current session + resume point | `STATE.md` — read FIRST every session |
| Historical sessions (8-17) | `SESSIONS_ARCHIVE.md` |
| Live module registry + data flow + known gotchas | `ARCHITECTURE.md` |
| Decision rationale + research findings | `MASTER_PLAN.md` |
| Original v1.1 blueprint (FROZEN) | `BLUEPRINT.md` |
| M3S detailed spec | `docs/M3S_SPEC.md` |
| 7-stage strategy development process | `STRATEGY_DEVELOPMENT_PROCESS.md` |

## Autonomous Workflow Protocol (MANDATORY)

These rules make the doc system self-maintaining. Follow them without being reminded.

### Rule 1 — Update docs BEFORE responding "done"

After any unit of work that changes project state, update the relevant files as part of finishing the task — not after being asked, not at session end.

| Trigger | File(s) to update |
|---|---|
| Phase/sub-phase status changed | `ROADMAP.md` (row + header Next action) + `STATE.md` (Current Position + session narrative line) |
| Added/changed/removed a `src/` module | `ARCHITECTURE.md` module registry + data flow |
| Hit a non-obvious bug or behavior | `ARCHITECTURE.md` Known Gotchas + project memory if cross-session |
| Made an architectural decision | `MASTER_PLAN.md` decision log |
| Learned a behavioral preference from Prince | feedback memory + `MEMORY.md` index |
| Discovered a new external resource/dashboard/channel | reference memory + `MEMORY.md` index |
| Phase completed OR project description/components changed materially | `README.md` (low frequency — see Rule 5b) |

Trigger checkpoint: "about to respond 'done' or 'complete'" — that's when the updates must already be written.

### Rule 2 — Dynamic sub-phase discovery

When unplanned work appears mid-session, handle it without being told:
- **New sub-phase discovered:** insert a row in `ROADMAP.md` tagged `DISCOVERED` with a one-line reason, update "Current phase" + "Next action" if it shifts, note in `STATE.md` narrative. If the new work displaces the existing Next action, flag it in the response ("this delays M3S — confirm?"). Do NOT silently reorder.
- **Unplanned feature:** same flow with `UNPLANNED` tag. Small (< 1 session) = just do it and note in STATE. Larger = add ROADMAP row.
- **Scope discovered inside a phase:** add as nested bullet under the parent row.

ROADMAP.md is a live ledger, not a frozen plan. New rows are expected.

### Rule 3 — Autonomous memory placement

When Prince says "remember this" / "store this" / "add this to memory" without specifying a file:
1. Classify: `user` / `feedback` / `project` / `reference`
2. Scan `MEMORY.md` for an existing file that fits by topic
3. If a fit exists → append/merge. If not → create `<type>_<topic>.md` with frontmatter + add to `MEMORY.md` index
4. Report placement in one line: *"Stored as project memory in `project_X.md`"*
5. Never ask "where should I put this?" unless content genuinely spans multiple categories.

### Rule 4 — Session-start autonomy

First turn of any new session, before responding, silently read `STATE.md` Current Position → `ROADMAP.md` current row + Next action → relevant session narrative if referenced. Then answer the actual question. Never ask "where did we leave off?" — the answer is structurally determined by ROADMAP + STATE.

### Rule 5 — STATE.md high-frequency updates

STATE.md is updated whenever state changes, not just at session end:
- **Current Position header** updated every time a phase/sub-phase status changes
- **Session narrative** appended on every meaningful unit of work
- **Last-Updated timestamp** refreshed on every edit

### Rule 5b — README.md low-frequency refresh

README is an onboarding doc, not a live tracker. Refresh when: a phase completes, the project description drifts from reality, or a new top-level component lands in `src/`. Not for per-task status updates.

### Rule 6 — Doc updates are silent

Do NOT announce doc updates in the response body (exception: Rule 3's one-line memory placement confirmation). The diff is visible in git.

### Enforcement

- `scripts/doc_lint.py` fails if phase status claims appear outside `ROADMAP.md`
- `feedback_autonomous_docs.md` memory pins these rules cross-session so they survive compaction

### Hard rules

1. **NEVER add phase-status claims to any file other than `ROADMAP.md`.** Other files link to it, they do NOT copy it.
2. **When a phase status changes, update `ROADMAP.md` FIRST**, then `STATE.md`.
3. **`BLUEPRINT.md` is FROZEN.** Never edit. If the design changes, update `ARCHITECTURE.md` and note the deviation in `MASTER_PLAN.md`.
4. **Before writing any new `.md` file**, check if the content fits an existing file. Add files only for genuinely new concerns.

## Strategy Development Process (MANDATORY)

When developing, evaluating, or optimizing ANY strategy, follow `STRATEGY_DEVELOPMENT_PROCESS.md`.
Never skip stages. Document kill decisions with obituaries. Update the process file when new lessons emerge.

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
1. Read `STATE.md` — resume from last checkpoint (Current Position header)
2. Read `ROADMAP.md` — confirm current phase + Next action
3. Read `ARCHITECTURE.md` — understand current module state if relevant to the task
4. If needed, read `MASTER_PLAN.md` for deeper context on *why* decisions were made
5. Update `STATE.md` continuously per Autonomous Workflow Protocol Rule 5, not just at session end

## Key Technical Decisions (Do Not Override)

- **Platform:** macOS -- MT5 does NOT work, use IC Markets cTrader + IBKR `ib_insync`
- **DataFrames:** pandas primary (`ta` library for indicators, not pandas-ta)
- **Backtesting:** Custom event-driven engine (`src/backtest/engine.py`) — same code path as live. NOT VectorBT.
- **Strategy Ingestion:** YAML IR → code generator, 6 format parsers (Pine Script, MQL4/5, NL, webhook, raw rules, Python frameworks)
- **Reports:** Triple output (SQLite + JSON + HTML) via `ResultStore.save_run()`, single `compute_metrics()` source of truth
- **Dashboard:** Streamlit 5-page interactive UI (`streamlit run src/dashboard/app.py`)
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

## CLI Commands

| Command | Purpose |
|---|---|
| `python3 -m scripts.backtest run <strategy> --symbol X --tf 1h` | Run backtest → triple output (DB + JSON + HTML) |
| `python3 -m scripts.backtest run <strategy> --enable-risk --risk-mode BALANCED` | Run backtest with risk gating |
| `python3 -m scripts.backtest risk-compare <strategy> --symbol X --tf 1h` | Compare same strategy across all risk modes |
| `python3 -m scripts.backtest validate <strategy> --tier lite` | Run validation protocols (lite/standard/intense/research) |
| `python3 -m scripts.backtest compare 1 2 3` | Compare multiple backtest runs |
| `python3 -m scripts.import_strategy import --format pine --file X` | Import strategy from any format |
| `python3 -m scripts.import_strategy formats` | List supported import formats |
| `python3 -m scripts.dashboard` | Launch Streamlit dashboard |
| `python3 -m scripts.run_verification` | Run backtest engine verification suite |
| `python3 scripts/preflight.py` | Pre-flight check before paper trading launch (exit 0=ok, 1=warn, 2=fail) |
| `python3 scripts/watchdog.py` | Independent heartbeat monitor — kills frozen engine after 360s stale |

## Coding Standards

- Python 3.11+, async-first with asyncio + uvloop
- Type hints everywhere, msgspec for data classes
- structlog for logging (JSON output)
- TOML for all config files
- Tests in `tests/` mirroring `src/` structure, `pytest-asyncio` (mode=auto) for async tests
- Never commit API keys -- use env vars or gitignored config
