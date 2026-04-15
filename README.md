# Algo Trading System

A modular, event-driven algorithmic trading system for systematic trading at retail scale. Same `BaseStrategy.on_features() → Signal | None` code path in backtest and live. Risk management runs as a separate ZeroMQ process and cannot be bypassed.

**Current phase:** Phase G Gold Leveraged Stack — G.0 through G.2 + tasks #101-#116 shipped on `feat/gold-refactor`. **1227 tests passing.** Merge to main scheduled 2026-04-17 after the crypto Day 4 sprint cron. Main branch runs 3b-2 M3S shadow + 3c meta-labeling shadow. See [ROADMAP.md](ROADMAP.md) for the authoritative phase status.

**Validated production strategies (as of 2026-04-15 empirical validation):**

| Strategy | Mode | Deployment | Validated at | Verdict |
|---|---|---|---|---|
| `donchian_gold` | RISK_SCALED | L=15 (half-Kelly) | WF Calmar **+11.7**, ann return **+140%/yr** | ✅ DEPLOYABLE |
| `vol_momentum_gold` | MARGIN_CAPPED | L=10-15 | WF Calmar **+4.93**, 6/6 profitable folds | ✅ DEPLOYABLE |
| `swift_alma` | INVARIANT | — | WF continuous Calmar +0.488 | ❌ research-only |

---

## Project source of truth

Each concern lives in exactly one file. Redundancy causes drift.

| Concern | File |
|---|---|
| Phase status, milestones, next action | [ROADMAP.md](ROADMAP.md) |
| Current session + recent history | [STATE.md](STATE.md) |
| Historical sessions (8-17) | [SESSIONS_ARCHIVE.md](SESSIONS_ARCHIVE.md) |
| Live module registry + data flow + known gotchas | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Decision rationale + research | [MASTER_PLAN.md](MASTER_PLAN.md) |
| Original v1.1 blueprint (frozen) | [BLUEPRINT.md](BLUEPRINT.md) |
| M3S detailed spec | [docs/M3S_SPEC.md](docs/M3S_SPEC.md) |
| Leverage strategy design principles | [docs/LEVERAGE_STRATEGY_DESIGN.md](docs/LEVERAGE_STRATEGY_DESIGN.md) |
| Broker fee profiles reference | [docs/BROKER_FEES.md](docs/BROKER_FEES.md) |
| 7-stage strategy dev process | [STRATEGY_DEVELOPMENT_PROCESS.md](STRATEGY_DEVELOPMENT_PROCESS.md) |
| Claude instructions + doc hygiene rules | [CLAUDE.md](CLAUDE.md) |

---

## Features

**Data & ingestion**
- Binance WebSocket live feed + historical REST downloader (crypto)
- Dukascopy parquet historical XAUUSD data (gold, 2y 1m/5m/1h)
- IC Markets cTrader Open API feed + OAuth helper (Phase 4, awaiting KYC)
- Event-driven candle builder with tick → OHLCV aggregation
- `ta`-library-based feature engine with 30+ indicators

**Strategies**
- 3 crypto-validated + 3 gold-validated + 7 registry-registered strategies
- Pluggable `BaseStrategy.on_features() → Signal | None` contract
- `leverage_range: tuple[float, float]` first-class metadata per strategy
- Strategy ingestion from Pine Script / MQL / natural language / webhooks / YAML / Python

**Backtest engines**
- Event-driven `BacktestEngine` (crypto, TradingView bit-exact parity)
- Separate `LeveragedBacktestEngine` for gold (CFD margin accounting, 50% broker stop-out, Brownian bridge + M1PathModel intrabar)
- 14 Plotly chart renderers, 7 validation protocols, tiered validation runner
- QuantStats integration, walk-forward optimizer
- Single-source-of-truth `compute_metrics()` for crypto; equivalent metrics dict for gold

**Deep Backtest framework** (task #112 + #114 + #115 + #79)
- Interactive `questionary` TUI for multi-select dimension picker
- 6-phase pipeline: preflight → matrix → sanity → leverage-validation → walk-forward → verdict → report
- 5 leverage modes: INVARIANT / MARGIN_CAPPED / VOL_TARGETED / RISK_SCALED / KELLY_FRACTIONAL
- Phase 0 read-back probe + Phase 2.5 zero-tolerance hand-trace validation
- **G.7 attribution** (task #79): per-cell decomposition of `return_pct` into alpha + leverage amplification + cost drag + margin rejection + residual
- HTML + landscape-A4 PDF + PNG heatmaps per run (HTML now includes Attribution section)
- Multi-mode dispatch: pick N modes, get N reports
- **Cross-run compare** CLI: `scripts/deep_backtest_compare.py` produces side-by-side HTML of 2+ runs with portfolio recommendation

**Risk management**
- ZeroMQ-isolated risk manager (out-of-process, cannot be bypassed)
- 4 adaptive modes: AGGRESSIVE / BALANCED / DEFENSIVE / CUSTOM
- Per-strategy profiles + mode backtest verification
- Leverage gates: per-position / aggregate / liquidation buffer
- Inline fast-path for leveraged engine (RCU versioned portfolio view)
- M3S `request_leverage` API with 5 reason codes + leverage_grants audit store

**Money management (M3S — Phase 3b-2)**
- `Compounder.risk_scalar` with geometric-mean blend + leverage damping at L > 1
- 10 sub-phases: regime detection, edge decay, purged CV, Bayesian Kelly, LeverageBudgetAllocator, HRP-lite allocator, meta-labeling (LR + LightGBM, shadow)
- Floor + Sharpe-weighted dynamic pool leverage allocation

**Live operations**
- Paper trading via launchd, watchdog with data-staleness detection
- Pre-flight checks, 5-step ramp plan, 30-day clock for G.3 paper validation
- Shadow orchestrator for ParquetReplayFeed → backtest parity verification
- Streamlit 5-page interactive dashboard

---

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt
pip install questionary  # optional, enables deep_backtest interactive TUI

# Crypto backtest (produces SQLite row + JSON + HTML)
python3 -m scripts.backtest run bb_rsi_mr --symbol BTCUSDT --tf 1h --days 365

# Deep backtest a strategy (interactive multi-select TUI)
PYTHONPATH=. python3 scripts/deep_backtest.py donchian_gold

# Deep backtest non-interactive with RISK_SCALED mode
PYTHONPATH=. python3 scripts/deep_backtest.py donchian_gold \
  --non-interactive \
  --timeframes 1h --windows 365 --leverages 10,15,20 \
  --fees ic_markets_mt4_xauusd_normal \
  --leverage-mode risk_scaled --baseline-leverage 10

# Validate strategy across multiple intervals
python3 -m scripts.backtest validate bb_rsi_mr --tier lite

# Import a strategy from Pine Script / MQL / natural language
python3 -m scripts.import_strategy import --format natural \
  --describe "Buy when 9 EMA crosses 21 EMA"

# Launch interactive dashboard
python3 -m scripts.dashboard

# Run live pipeline
python3 -m src.main
```

---

## Architecture — system overview

```mermaid
graph TB
    subgraph "Data Sources"
        BWS["Binance WebSocket<br/>(crypto, live)"]
        BR["Binance REST<br/>(crypto, historical)"]
        DUKA["Dukascopy parquet<br/>(gold, historical)"]
        ICM["IC Markets cTrader<br/>(gold, Phase 4)"]
    end

    subgraph "Ingestion"
        CB["Candle Builder<br/>tick → OHLCV"]
        FE["Feature Engine<br/>ta library"]
        STORE[("SQLite + Parquet")]
    end

    subgraph "Strategy Layer"
        SR["Strategy Router"]
        BASE["BaseStrategy<br/>on_features() → Signal"]
    end

    subgraph "Money Mgmt / M3S"
        COMP["Compounder<br/>risk_scalar + target_leverage"]
        REQ["request_leverage<br/>5 reason codes"]
        ALLOC["LeverageBudget<br/>Allocator"]
        GRANTS[("leverage_grants.db")]
    end

    subgraph "Risk"
        RM["Risk Manager<br/>ZMQ isolated"]
        IL["Inline Leverage Gate<br/>in-process fast path"]
    end

    subgraph "Execution"
        BE["BacktestEngine<br/>(crypto TV-parity)"]
        LBE["LeveragedBacktestEngine<br/>(gold CFD)"]
        PE["PaperExecutor"]
        CTE["cTrader Executor<br/>(Phase 4)"]
    end

    subgraph "Output"
        METRICS["compute_metrics()<br/>SSOT"]
        RS["ResultStore<br/>DB + JSON + HTML"]
        DASH["Streamlit<br/>5-page dashboard"]
    end

    BWS --> CB
    BR --> STORE
    DUKA --> FE
    ICM -.Phase 4.-> CB
    CB --> FE
    FE --> SR
    SR --> BASE
    BASE --> COMP
    COMP --> REQ
    REQ --> GRANTS
    ALLOC --> REQ
    COMP --> RM
    COMP --> IL
    RM --> BE
    IL --> LBE
    BE --> METRICS
    LBE --> METRICS
    METRICS --> RS
    METRICS --> PE
    PE --> CTE
    RS --> DASH
```

---

## Architecture — Gold Leveraged Stack

```mermaid
graph LR
    subgraph "Inputs"
        XAU["XAUUSD data<br/>1m / 5m / 15m / 30m / 1h / 4h / 1d"]
        FEES["Fee profiles<br/>pine_zero / MT4 / cTrader"]
    end

    subgraph "Strategies"
        DG["donchian_gold<br/>session + ATR stops<br/>leverage_range (10, 50)"]
        VMG["vol_momentum_gold<br/>vol_target + ATR stops<br/>leverage_range (10, 50)"]
        SA["swift_alma<br/>Pine port + ALMA<br/>leverage_range (1, 100)"]
    end

    subgraph "Leveraged Engine"
        SIG["Signal emission"]
        SIZ["Position sizing<br/>q = equity × risk_pct / sl_dist"]
        LM["_apply_leverage_mode<br/>task #114"]
        BOOK["Book<br/>CFD margin<br/>(1¢ epsilon gate)"]
        STOP["Broker stop-out<br/>50% margin level"]
        PATH["M1PathModel<br/>tick-accurate SL/TP<br/>(fallback: Brownian bridge)"]
    end

    subgraph "Sub-books"
        INST["Institutional sub-book<br/>Tiers 1-4<br/>$7000 initial"]
        AGGR["Aggressive sub-book<br/>Tier 5<br/>$3000 initial"]
    end

    subgraph "Output"
        EQC["equity curves"]
        LG["leverage_grants.db"]
        TRD["trades[]"]
        METG["metrics dict"]
    end

    XAU --> SIG
    DG --> SIG
    VMG --> SIG
    SA --> SIG
    SIG --> LM
    LM --> SIZ
    SIZ --> BOOK
    FEES --> BOOK
    BOOK --> PATH
    BOOK --> STOP
    BOOK --> INST
    BOOK --> AGGR
    INST --> EQC
    AGGR --> EQC
    BOOK --> TRD
    BOOK --> LG
    EQC --> METG
    TRD --> METG
```

---

## Deep Backtest pipeline (task #112 + #114 + #115 + #79)

```mermaid
graph TD
    START(["PYTHONPATH=. python3 scripts/deep_backtest.py strategy"])
    TUI{"Interactive TUI?<br/>stdout is TTY +<br/>questionary installed"}
    PROMPTS["Multi-select:<br/>• windows (1mo→4y)<br/>• timeframes (1m→1d)<br/>• fees (pine/MT4/cTrader)<br/>• leverages (1x→1000x)<br/>• leverage modes<br/>• WF yes/no + retune yes/no"]
    FLAGS["Parse CLI flags<br/>--non-interactive"]

    P0["Phase 0 — Preflight<br/>• data files exist<br/>• fee profile valid<br/>• strategy resolves<br/>• M1PathModel cache<br/>• leverage_mode read-back probe<br/>  (task #115 RISK_SCALED / KELLY only)"]

    P1["Phase 1 — Matrix<br/>windows × TFs × leverages × fees cells<br/>(each cell: 1 backtest + metrics)"]

    P2["Phase 2 — Sanity checks<br/>• error cells<br/>• trade count sanity<br/>• leverage invariance detection<br/>• RISK_SCALED Pearson linearity<br/>• margin-rejection warnings (>30% rate)<br/>• extreme DD cells"]

    P25{"Mode?"}
    P25R["Phase 2.5 — RISK_SCALED validation<br/>9 hard-fail assertions on 30d hand-trace:<br/>trade count / sides / prices / q-scaling ±15% /<br/>P&L-scaling ±15% / aggregate / read-back ×2 /<br/>margin ratio / commission scaling"]
    P25K["Phase 2.5 — KELLY_FRACTIONAL validation<br/>6 hard-fail + 2 warnings:<br/>invariance across L / priors math /<br/>read-back / cap-clamp warn / unrealistic-priors warn"]
    P3["Phase 3 — Leverage deep-dive<br/>auto-triggered on invariance<br/>(passthrough modes only)"]

    P4["Phase 4 — Walk-forward OOS<br/>N × fold_days non-overlapping folds<br/>dt-based slicing<br/>optional per-fold retune<br/>continuous-run cross-check"]

    P5{"Phase 5 — Verdict<br/>mode-aware gates"}
    FAILED["FAILED<br/>(2.5 hard-fail<br/>overrides all)"]
    RONLY["RESEARCH_ONLY<br/>(WF fails mode gate)"]
    DEPL["DEPLOYABLE<br/>(clears mode gate)"]
    NEEDSWF["NEEDS_WF<br/>(no WF run)"]

    P6["Phase 6 — Report<br/>• matrix.csv<br/>• summary.json<br/>• index.html<br/>• report.pdf (A4 landscape)<br/>• heatmaps/ PNGs"]

    START --> TUI
    TUI -->|yes| PROMPTS
    TUI -->|no| FLAGS
    PROMPTS --> P0
    FLAGS --> P0
    P0 --> P1
    P1 --> P2
    P2 --> P25
    P25 -->|RISK_SCALED| P25R
    P25 -->|KELLY_FRACTIONAL| P25K
    P25 -->|INVARIANT/MARGIN_CAPPED/VOL_TARGETED| P3
    P25R --> P4
    P25K --> P4
    P3 --> P4
    P4 --> P5
    P5 --> FAILED
    P5 --> RONLY
    P5 --> DEPL
    P5 --> NEEDSWF
    FAILED --> P6
    RONLY --> P6
    DEPL --> P6
    NEEDSWF --> P6
```

---

## Leverage modes — decision flowchart (task #113 research + #114 implementation)

```mermaid
graph TD
    START["New strategy deployment"]
    Q1{"Pine port or<br/>TV replica?"}
    INV["Mode: INVARIANT<br/>risk_pct == sl_pct<br/>notional = equity<br/>Gate: Calmar ≥ 0.5"]
    Q2{"Edge depends on<br/>volatility regime?"}
    VT["Mode: VOL_TARGETED<br/>risk_pct × vol_scalar ∈ [0.5, 2.0]<br/>regime-adaptive<br/>Gate: Calmar ≥ 1.0"]
    Q3{"Solid OOS stats<br/>(100+ trades,<br/>stable win_rate/payoff)?"}
    KF["Mode: KELLY_FRACTIONAL<br/>risk_pct = min(0.25,<br/>kelly_fraction × f*)<br/>half-Kelly safe default<br/>Gate: Calmar ≥ 0.5 AND ann ≥ 50%"]
    Q4{"Calmar ≥ 1 under<br/>MARGIN_CAPPED?<br/>(has real edge)"}
    RS["Mode: RISK_SCALED<br/>risk_pct = base × (L / baseline)<br/>Linear return amplification<br/>Gate: Calmar ≥ 0.3 AND ann ≥ 100%"]
    MC["Mode: MARGIN_CAPPED<br/>DEFAULT<br/>notional fixed, L gates margin<br/>Gate: Calmar ≥ 1.0"]

    START --> Q1
    Q1 -->|yes| INV
    Q1 -->|no| Q2
    Q2 -->|yes| VT
    Q2 -->|no| Q3
    Q3 -->|yes| KF
    Q3 -->|no| Q4
    Q4 -->|yes, return-hungry| RS
    Q4 -->|no, risk-efficient| MC

    classDef deployable fill:#d4edda,stroke:#0a7a3a,color:#000
    classDef validated fill:#fff3cd,stroke:#856404,color:#000
    class RS,MC deployable
    class KF,INV,VT validated
```

**Strategy → mode assignment (empirically validated 2026-04-15):**

| Strategy | Mode | Why | Source |
|---|---|---|---|
| `swift_alma` | INVARIANT | Pine port, fold-3 chop regime kills scaling | Task #111 WF fail |
| `donchian_gold` | RISK_SCALED L=15 | **Return-hungry, high-edge** — 5/6 profitable folds, +140%/yr | Task #115 validation ✅ |
| `vol_momentum_gold` | MARGIN_CAPPED L=10-15 | **Risk-efficient, low-return** — Calmar 4.93 but only 24%/yr under RISK_SCALED | Task #114 + #115 ✅ |

---

## Strategy status

```mermaid
graph LR
    subgraph "Production — live paper"
        P1["donchian_ensemble_adx<br/>crypto"]
        P2["bb_rsi_mr_opt<br/>crypto"]
        P3["vol_momentum<br/>crypto"]
        P4["funding_carry<br/>live"]
    end

    subgraph "Gold — deployable post merge"
        G1["donchian_gold<br/>RISK_SCALED L=15<br/>+140%/yr"]
        G2["vol_momentum_gold<br/>MARGIN_CAPPED<br/>Calmar 4.93"]
    end

    subgraph "Research-only / shelved"
        R1["swift_alma<br/>WF Calmar 0.49"]
    end

    subgraph "Graveyard"
        D1["candle_burst_hunter"]
        D2["news_spike_fade"]
        D3["hedged_structure_play"]
        D4["btc_neutral"]
        D5["wf_ema"]
    end

    subgraph "Registry-only"
        E1["ema_crossover"]
        E2["funding_mean_reversion"]
        E3["clenow_momentum"]
    end

    classDef prod fill:#d4edda,stroke:#0a7a3a,color:#000
    classDef gold fill:#fff3cd,stroke:#856404,color:#000
    classDef research fill:#cce5ff,stroke:#004085,color:#000
    classDef dead fill:#f8d7da,stroke:#721c24,color:#000
    classDef reg fill:#e2e3e5,stroke:#383d41,color:#000

    class P1,P2,P3,P4 prod
    class G1,G2 gold
    class R1 research
    class D1,D2,D3,D4,D5 dead
    class E1,E2,E3 reg
```

**Detailed status:**

| Strategy | Location | Status | Best backtest metric | Notes |
|---|---|---|---|---|
| `donchian_gold` | `src/strategies/trend_following/donchian_gold.py` | ✅ DEPLOYABLE | WF Calmar +11.7 / +140%/yr at L=15 RISK_SCALED | Task #115 empirically validated |
| `vol_momentum_gold` | `src/strategies/momentum/vol_momentum_gold.py` | ✅ DEPLOYABLE | WF Calmar +4.93 at L=10 MARGIN_CAPPED | Diversifier role |
| `swift_alma` | `src/strategies/trend_following/swift_alma.py` | 🔬 Research-only | 1y Calmar +2.14, WF continuous Calmar +0.49 | Pine port, fold-3 regime dependency |
| `donchian_ensemble_adx` | `src/strategies/trend_following/donchian_ensemble.py` | ✅ Live paper | Sharpe 2.32 crypto baseline | Portfolio 30% |
| `bb_rsi_mr_opt` | `src/strategies/day_trading/bb_rsi_mr.py` | ✅ Live paper | — | Portfolio 40% |
| `vol_momentum` | `src/strategies/momentum/vol_momentum.py` | ✅ Live paper | — | Portfolio 30% (crypto) |
| `funding_carry` | `src/strategies/carry/funding_carry.py` | ✅ Live paper | — | Funding-rate capture |
| `candle_burst_hunter` | `src/strategies/aggressive/candle_burst_hunter.py` | ⚰️ Graveyard | — | Needs tick data fundamentally |
| `news_spike_fade` | `src/strategies/aggressive/news_spike_fade.py` | ⚰️ Graveyard | — | Bar-level can't capture tick fade |
| `hedged_structure_play` | `src/strategies/aggressive/hedged_structure_play.py` | ⚰️ Graveyard | — | Design mismatch with gold trend |
| `ema_crossover` | `src/strategies/scalping/ema_crossover.py` | 📋 Registry | — | Not yet validated |
| `funding_mean_reversion` | `src/strategies/carry/funding_mean_reversion.py` | 📋 Registry | — | Not yet validated |
| `clenow_momentum` | `src/strategies/momentum/clenow_momentum.py` | 📋 Registry | — | Not yet validated |

---

## Workflow — deep backtest (interactive)

```mermaid
sequenceDiagram
    actor User
    participant CLI as deep_backtest.py
    participant TUI as Interactive TUI
    participant P as Pipeline
    participant R as Report

    User->>CLI: python3 scripts/deep_backtest.py donchian_gold
    CLI->>TUI: stdout is TTY + questionary available?
    TUI-->>User: Select windows [☐1mo ☐3mo ☒1y ☐2y]
    User-->>TUI: ☒1y
    TUI-->>User: Select timeframes [☐1m ☐5m ☐15m ☒1h ☐4h]
    User-->>TUI: ☒1h
    TUI-->>User: Select fees / leverages / modes / WF
    User-->>TUI: ... picks ...
    TUI-->>User: Run 5 cells × 1 mode, ~30s. Proceed?
    User-->>TUI: yes
    TUI->>P: DeepBacktestConfig
    P->>P: Phase 0 preflight (probe)
    P->>P: Phase 1 matrix (5 cells)
    P->>P: Phase 2 sanity checks
    P->>P: Phase 2.5 hand-trace (if RISK_SCALED)
    P->>P: Phase 4 walk-forward (6 folds)
    P->>P: Phase 5 verdict (mode-aware gate)
    P->>R: HTML + PDF + heatmaps + CSV + JSON
    R-->>User: VERDICT: DEPLOYABLE / RESEARCH_ONLY / FAILED
```

---

## Workflow — live trading (paper, main branch)

```mermaid
sequenceDiagram
    participant L as launchd
    participant E as TradingEngine
    participant W as Watchdog
    participant RS as Risk Server (ZMQ)
    participant RC as Risk Client
    participant S as Strategy
    participant P as PaperExecutor
    participant DB as trades.db

    L->>E: spawn src.main
    L->>W: spawn watchdog
    L->>RS: spawn risk_server
    E->>RS: ZMQ connect
    loop every tick
        E->>S: on_candle(Candle)
        S-->>E: Signal | None
        E->>RC: check(Signal)
        RC->>RS: ZMQ req/rep
        RS-->>RC: allow / scale / reject
        RC-->>E: decision
        alt allowed
            E->>P: place_order(Signal)
            P->>DB: write trade row
        end
    end
    W-->>L: heartbeat stale > 1800s → kill E
```

---

## Strategy ingestion pipeline

```mermaid
graph LR
    subgraph "Inputs"
        PINE[Pine Script]
        MQL[MQL4/5]
        NL[Natural language]
        WH[Webhook JSON]
        YML[Raw YAML]
        PY[Python framework]
    end

    subgraph "Parsers"
        PP[pine_parser]
        MP[mql_parser]
        NP[nl_parser]
        WP[webhook_parser]
        YP[yaml_parser]
        PYP[py_parser]
    end

    IR["Strategy YAML IR<br/>(intermediate representation)"]
    CG["Code generator"]
    BS["BaseStrategy subclass"]
    BT["Backtest"]

    PINE --> PP
    MQL --> MP
    NL --> NP
    WH --> WP
    YML --> YP
    PY --> PYP
    PP & MP & NP & WP & YP & PYP --> IR
    IR --> CG
    CG --> BS
    BS --> BT
```

---

## Directory structure

```
algo-trading/
├── src/
│   ├── main.py                           # TradingEngine entry point
│   ├── config.py                         # TOML config loader (msgspec)
│   ├── shadow_orchestrator.py            # ParquetReplayFeed driver (G.2h.2)
│   ├── data/
│   │   ├── feeds/binance_ws.py           # Binance WebSocket feed
│   │   ├── feeds/icmarkets_feed.py       # IC Markets cTrader Open API (Phase 4)
│   │   ├── feeds/parquet_replay_feed.py  # Historical replay as async feed
│   │   ├── candle_builder.py             # Tick → OHLCV
│   │   ├── feature_engine.py             # Technical indicators (ta library)
│   │   ├── storage.py                    # SQLite + Parquet storage
│   │   └── downloader.py                 # Historical data downloader
│   ├── strategies/
│   │   ├── base.py                       # BaseStrategy interface + leverage_range
│   │   ├── router.py                     # StrategyRouter + STRATEGY_REGISTRY
│   │   ├── trend_following/
│   │   │   ├── donchian_ensemble.py      # Crypto Donchian (TV-parity)
│   │   │   ├── donchian_gold.py          # Gold Donchian + session filter
│   │   │   └── swift_alma.py             # Pine SWIFTALGO port
│   │   ├── day_trading/bb_rsi_mr.py      # BB+RSI Mean Reversion (crypto)
│   │   ├── momentum/vol_momentum.py      # Crypto momentum
│   │   ├── momentum/vol_momentum_gold.py # Gold vol-targeted momentum
│   │   ├── momentum/clenow_momentum.py   # Clenow trend (registry)
│   │   ├── carry/funding_carry.py        # Funding carry (live)
│   │   ├── carry/funding_mean_reversion.py # Funding MR (registry)
│   │   ├── scalping/ema_crossover.py     # EMA scalp (registry)
│   │   └── aggressive/                   # Tier 5 (graveyard)
│   │       ├── candle_burst_hunter.py
│   │       ├── news_spike_fade.py
│   │       └── hedged_structure_play.py
│   ├── backtest/
│   │   ├── engine.py                     # Event-driven crypto engine (TV-parity)
│   │   ├── leveraged_engine.py           # Gold CFD engine (G.2a.4)
│   │   ├── book.py                       # LeveragedPosition + Book + stop-out (G.2a)
│   │   ├── costs.py                      # IC Markets spread + commission (G.2a.2)
│   │   ├── fee_profiles.py               # Named broker fee profiles (task #106)
│   │   ├── path.py                       # Brownian bridge + M1PathModel (G.2a.3/G.5b)
│   │   ├── structure_levels.py           # PDH/PDL, Fib, swing H/L (G.2e)
│   │   ├── deep_backtest.py              # Generic pipeline (tasks #112/#114/#115/#79)
│   │   ├── deep_backtest_interactive.py  # questionary TUI (task #114)
│   │   ├── deep_backtest_report.py       # HTML + PDF + heatmap + attribution (task #79)
│   │   ├── metrics.py                    # compute_metrics() SSOT
│   │   ├── result_store.py               # Triple output (DB + JSON + HTML)
│   │   ├── charts.py                     # 14 Plotly renderers
│   │   ├── report.py                     # Jinja2 HTML report
│   │   ├── protocols.py                  # 7 test protocols
│   │   ├── validator.py                  # Tiered validation (lite→research)
│   │   ├── walk_forward.py               # Walk-forward optimizer
│   │   └── quantstats_bridge.py          # QuantStats tearsheets
│   ├── m3s/                              # Master Money Management System (Phase 3b-2)
│   │   ├── compounder.py                 # risk_scalar + leverage damping (G.2b)
│   │   ├── hooks.py                      # request_leverage facade (G.2b)
│   │   ├── leverage_grants.py            # SQLite append store (G.2b)
│   │   ├── leverage_budget.py            # Floor + Sharpe-weighted allocator (G.2h.4)
│   │   ├── aggressive_compounder.py      # Tier 5 fixed-% (G.2e)
│   │   ├── portfolio_view.py             # RCU versioned snapshot (G.2c)
│   │   ├── allocator.py                  # HRP-lite + Ledoit-Wolf
│   │   ├── portfolio.py                  # PortfolioTracker + rolling stats
│   │   ├── regime.py                     # Regime detection + mode switching
│   │   ├── evaluation.py                 # Purged CV + Bayesian Kelly
│   │   ├── edge_decay.py                 # Strategy edge-decay detection
│   │   └── signal_filter/                # Phase 3c meta-labeling (LightGBM + LR)
│   ├── risk/
│   │   ├── manager.py                    # 7-check chain + mode resolution
│   │   ├── server.py / client.py         # ZeroMQ transport
│   │   ├── kelly_sizer.py
│   │   ├── circuit_breakers.py
│   │   ├── kill_switch.py
│   │   ├── fat_finger.py
│   │   └── inline_leverage.py            # In-process fast path (G.2c)
│   ├── research/
│   │   ├── strategy_ir.py                # Strategy YAML IR schema
│   │   ├── codegen.py                    # IR → Python codegen
│   │   ├── catalog.py                    # Strategy catalog
│   │   └── parsers/                      # 6 format parsers
│   ├── execution/
│   │   ├── base.py                       # Executor interface
│   │   ├── paper_executor.py             # Paper trading
│   │   └── icmarkets_executor.py         # cTrader executor (Phase 4)
│   ├── utils/
│   │   ├── types.py                      # Signal, Fill, LeverageGrant
│   │   ├── instruments.py                # XAUUSD metadata (G.0b)
│   │   ├── config.py                     # TOML loader (msgspec)
│   │   └── logger.py                     # structlog setup
│   └── dashboard/app.py                  # Streamlit 5-page dashboard
├── scripts/
│   ├── backtest.py                       # Unified crypto backtest CLI
│   ├── deep_backtest.py                  # Deep backtest CLI (tasks #112/#114)
│   ├── deep_backtest_compare.py          # Cross-run side-by-side compare CLI (task #79)
│   ├── walk_forward_donchian_gold.py     # Bespoke WF (task #86)
│   ├── walk_forward_vol_momentum_gold.py # Bespoke WF (task #90)
│   ├── walk_forward_swift_alma.py        # Bespoke WF (task #111)
│   ├── swift_full_matrix.py              # SWIFT 240-cell matrix (task #109)
│   ├── import_strategy.py                # Multi-format strategy import CLI
│   ├── dashboard.py                      # Streamlit launcher
│   ├── run_verification.py               # Engine verification suite
│   ├── g3_preflight.py                   # Pre-flight for G.3 paper clock
│   ├── watchdog.py                       # Engine heartbeat + kill
│   └── preflight.py                      # Pre-paper-launch checks
├── config/
│   ├── settings.toml                     # Main + [m3s_gold] gold bucket
│   ├── strategies.toml                   # Strategy parameters
│   ├── risk.toml                         # Risk dials + [leverage] gates
│   ├── broker_fees.toml                  # Fee profile registry (task #106)
│   ├── news_calendar.csv                 # NFP/FOMC/CPI windows
│   └── report_template.html              # Jinja2 HTML template
├── tests/                                # 1227 tests passing (feat/gold-refactor)
├── docs/
│   ├── LEVERAGE_STRATEGY_DESIGN.md       # Leverage mode design principles
│   ├── BROKER_FEES.md                    # Fee schedule research
│   ├── M3S_SPEC.md                       # M3S phase spec
│   └── STRATEGY_DEVELOPMENT_PROCESS.md   # 7-stage dev flow
├── reports/                              # Generated backtest reports (gitignored)
├── research/                             # Research documents
├── alpaca/                               # Alpaca MCP server (US equities)
├── tradingview-mcp-jackson/              # TradingView MCP server
├── ARCHITECTURE.md                       # Live module registry + gotchas
├── STATE.md                              # Session continuity tracker
├── ROADMAP.md                            # Phase status (single source of truth)
├── MASTER_PLAN.md                        # Full project context
└── CLAUDE.md                             # Claude instructions + doc hygiene
```

---

## MCP servers

Both servers are registered in `~/.claude.json` under `mcpServers`. Claude picks them up on startup.

| Server | Config key | Entry point | Purpose |
|---|---|---|---|
| Alpaca | `"alpaca"` | `alpaca/server.js` | Place/manage trades on Alpaca paper/live |
| TradingView | `"tradingview"` | `tradingview-mcp-jackson/src/server.js` | Read charts, indicators, Pine Script (78 tools) |

**Alpaca tools:** `get_account`, `place_order`, `get_positions`, `close_position`, `get_orders`, `cancel_order`, `get_quote`.

**TradingView tool categories:** chart state / data extraction / Pine Script editing / drawings / alerts / symbol + layout control / replay / batch-run / UI automation.

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Data classes | msgspec |
| Logging | structlog |
| Indicators | `ta` library (NOT pandas-ta) |
| ML | scikit-learn (LR) + LightGBM (meta-labeling shadow) |
| Storage | SQLite + Parquet |
| Charts | Plotly |
| Reports | Jinja2 HTML + fpdf2 PDF |
| TUI | questionary (deep_backtest interactive) |
| Dashboard | Streamlit |
| Market data (crypto) | Binance WebSocket + REST |
| Market data (gold) | Dukascopy parquet + IC Markets cTrader (Phase 4) |
| Config | TOML |
| Transport (risk) | ZeroMQ |

---

## Tested & proven

**Test suite:** 1276 tests passing on `feat/gold-refactor`. Includes:

- 50 deep_backtest framework tests (task #112 + #114 + #115 + #79)
- 43 strategy storage tests (task #117 + #118-#130 follow-up sweep)
- 7 leverage invariance tests (task #110)
- 20 TV parity tests (task #104)
- 11 M3S leverage tests (task #67)
- 27 Brownian bridge + M1PathModel tests (G.2a.3 + G.5b)
- 32 CFD Book margin tests (G.2a)
- 20 IC Markets cost model tests (G.2a.2)
- ~1065 other unit + integration tests

**Live operations:** paper trading deployed via launchd on `main`. Watchdog with data-staleness detection (task #97). M3S + meta-labeling in shadow mode (Phase 3b-2, Phase 3c). Pre-flight checks (task #89).

**Empirically validated gold deployments** (task #115, 2026-04-15):
- `donchian_gold` at RISK_SCALED L=15 → **WF continuous Calmar +11.7, +140%/yr annualized**, 5/6 profitable folds
- `vol_momentum_gold` at MARGIN_CAPPED L=10 → WF continuous Calmar +4.93, 6/6 profitable folds (from task #114 cross-validation)
- `swift_alma` → research-only (fold-3 chop regime kills scaling, WF Calmar +0.49)

---

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the authoritative phase table. **Phase G is merge-ready** for 2026-04-17 ~09:30.

Next items in the queue:
- **Task #62** — merge window 2026-04-17 ~09:30 (in progress)
- **Task #116** — research better leveraged strategy using task #113 + #115 findings (pending, triggered on demand)
- **Task #78** — G.6 adaptive leverage governor ML model (pending, Phase 5 tie-in, needs live trade history)
- **Task #81** — Phase 4 live smoke test (blocked on Spotware KYC)
- **Task #74** — G.3 30-day paper clock on IC Markets cTrader demo (blocked on Phase 4 + tuning)

Recently shipped (2026-04-15 Day 2): #109 SWIFT matrix · #110 leverage validation · #111 SWIFT WF · #112 Deep Backtest framework · #113 leverage research · #114 leverage_mode + TUI · #115 Phase 2.5 zero-tolerance · #116 empirical validation · **#79 G.7 attribution dashboard**

---

*Last updated: 2026-04-15 — Phase G tasks #101-#116 complete on feat/gold-refactor, merge scheduled 2026-04-17. 1227 tests passing.*
