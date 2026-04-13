# M3S — Master Money Management System: Plan v1.1 (Phase 3b-2)

**Authors:** Principal Engineer (PE) + Senior Hedge Fund PM (PM), co-writing
**Status:** ✅ APPROVED (Session 22) — sub-phase 0.1 in progress
**Bar:** Fixed-weight 40/30/30 backtest Sharpe 2.318. M3S must beat this in meta-backtest.

---

## v1.1 ADDENDUM (Session 22) — AUTHORITATIVE OVERRIDE

**Read this section first.** It supersedes specific sections of v1 below. The v1 body is preserved as research context — when it conflicts with this addendum, the addendum wins.

### A1 — Retail reality override

Prince is a retailer, not a hedge fund. We keep HF efficiency (Kelly, HRP, HWM gate, vol targeting) but reject HF caution in three places:
1. **More modes, including an aggressive one.** Retail has no capacity constraints, no LP redemption risk, no market impact, no quarterly drawdown scrutiny from capital allocators.
2. **User-configurable mode (CUSTOM).** Prince can dial every knob per-experiment.
3. **Meta-labeling deferred to Phase 3c** — biggest Sharpe upside but 2-week standalone sprint; ship M3S core first.

### A2 — Mode table (OVERRIDES §7)

**4 modes total:** CONSERVATIVE / STANDARD / GROWTH / CUSTOM

| Dial | CONSERVATIVE | STANDARD | **GROWTH** | **CUSTOM** |
|---|---|---|---|---|
| Intent | Capital preservation, post-DD recovery | Default | Exponential equity growth, retail-aggressive | User drives |
| `vol_target_annual` | 0.10 | 0.15 | 0.22 | user (≤0.50 soft cap) |
| `kelly_fraction` | 0.25 | 0.50 | 0.50 | user (>0.75 warns) |
| `max_per_strategy_cap` | 0.25 | 0.40 | 0.50 | user |
| `max_cluster_cap` | 0.40 | 0.65 | 0.75 | user |
| `dd_freeze_threshold` | 0.05 | 0.08 | 0.10 | user |
| `dd_halt_threshold` | 0.10 | 0.12 | 0.15 | user |
| `compound_cadence` | weekly | daily | daily | user (`per_trade` allowed) |
| `compound_every_n_trades` | — | — | — | user (≥1) |
| `auto_demote_dd` | 0.05 | 0.08 | 0.10 | user |
| `auto_demote_target` | (stays) | CONSERVATIVE | STANDARD | user |
| `compound_hwm_gate` | ✅ | ✅ | ✅ | ✅ **locked on** |
| `allocator_method` | inverse_vol | hrp_lite | hrp_lite | user |
| Cold-start equal-weight until | 20 trades/strat | 30 trades/strat | 30 trades/strat | user |

### A3 — Dynamic compounding pace (ALL modes)

Rolling-Sharpe pace dial applies to every mode, with different clamps:

```
compound_pace_scalar = clip(rolling_sharpe_30 / target_sharpe, floor, ceiling)
```

| Mode | Base cadence | Pace floor | Pace ceiling |
|---|---|---|---|
| CONSERVATIVE | weekly | 0.20 | 0.60 |
| STANDARD | daily | 0.25 | 1.00 |
| GROWTH | daily | 0.30 | 1.20 |
| CUSTOM | user | user (`compound_pace_floor`) | user (`compound_pace_ceiling`) |

HWM gate is **globally locked on** in every mode (variance-drag math is universal). Loader ignores `compound_hwm_gate=false` in CUSTOM, logs WARN, forces true.

### A4 — CUSTOM mode safety rails

CUSTOM mode refuses to load unless:
1. `i_accept_custom_mode_risk = true` (literal)
2. `compound_every_n_trades >= 1`
3. `auto_demote_dd_threshold < dd_freeze_threshold`
4. `kelly_fraction <= 1.0` (>0.75 warns)
5. `compound_pace_ceiling >= compound_pace_floor`

CUSTOM always emits `m3s_events` rows with `custom_config_hash` for traceability.

### A5 — 7 Tier 1 additions (NEW)

Items 1–3 are Prince-originated, 4–7 are from Session 22 research.

| # | Item | Sub-phase | LOC | Source |
|---|---|---|---|---|
| 1 | Regime detection → auto mode switching | 0.9 | ~300 | Session 22 design |
| 2 | Strategy edge decay detection | 0.2 ext | ~50 | Session 22 design |
| 3 | Signal conviction-weighted sizing | 0.5 ext | ~80 | Session 22 design |
| 4 | **Ledoit-Wolf covariance shrinkage** | 0.4 ext | ~50 | Ledoit-Wolf 2004/2020 JPM/AoS |
| 5 | **CVaR tail-risk position scaling** | 0.3 ext | ~80 | Rockafellar-Uryasev 2000 |
| 6 | **Purged & Embargoed K-Fold CV** | 0.10 | ~200 | Lopez de Prado AFML ch.7 |
| 7 | **Bayesian fractional Kelly from param uncertainty** | 0.10 | ~60 | Baker-McHale 2013 |

**Deferred to Phase 3c:** Meta-labeling (Lopez de Prado AFML ch.3) — +0.2–0.5 Sharpe but requires its own LightGBM training pipeline and 2-week sprint. Gated on sub-phase 0.10 (Purged CV) being in place.

### A6 — Sub-phase breakdown (10 sub-phases, OVERRIDES §11 Phase 0)

Each sub-phase = own git commit + own test gate + (where applicable) own backtest gate. Prince reviews each before the next starts.

| # | Name | LOC | Tests | Backtest gate | Depends on |
|---|---|---|---|---|---|
| **0.1** | Skeleton + state layer | ~250 | ~15 | — | — |
| **0.2** | Portfolio tracker + edge-decay monitor (+Tier 1 #2) | ~450 | ~30 | — | 0.1 |
| **0.3** | Compounder + 4 modes + CVaR scaling (+Tier 1 #5) | ~430 | ~40 | **BT #1:** compounder on golden trades | 0.1 |
| **0.4** | Allocator HRP-lite + Ledoit-Wolf shrinkage (+Tier 1 #4) | ~450 | ~35 | **BT #2:** allocator vs fixed-weight | 0.2 |
| **0.5** | Hooks + conviction scorer (+Tier 1 #3) | ~280 | ~23 | — | 0.2, 0.3, 0.4 |
| **0.6** | Scheduler + SQLite persistence + migrations | ~300 | ~15 | — | 0.1, 0.5 |
| **0.7** | Meta-backtest simulator | ~350 | ~10 | **BT #3:** full M3S vs baselines, Prince picks default mode | 0.6 |
| **0.8** | Wire into main.py (DISABLED) | ~230 | ~5 smoke | — | 0.7 |
| **0.9** | Regime detector + auto mode switching (+Tier 1 #1) | ~300 | ~20 | **BT #4:** regime switching vs static modes | 0.8 |
| **0.10** | Purged CV + Bayesian fractional Kelly (+Tier 1 #6,#7) | ~260 | ~20 | **BT #5:** per-strategy Sharpe distributions | 0.4 |

**Totals:** ~3,300 LOC, ~213 tests, 5 backtest gates.

**Rule:** No sub-phase ships without its test gate green. No backtest gate passes without matching the v1 success criteria (Sharpe ≥ 2.35 on the comparison baseline for the relevant backtest).

### A7 — Answered open questions (§16)

| # | Question | Answer |
|---|---|---|
| 1 | Capital base | $10k paper nominal — same as live engine |
| 2 | Rebalance day | Sunday 23:59 UTC for weekly; daily = 23:59 UTC |
| 3 | Signal corr window | 30d × 1h = 720 bars. OK for v1, re-evaluate after sub-phase 0.7 |
| 4 | Auto-thaw | No auto CONSERVATIVE→STANDARD. Manual Prince command only. |
| 5 | LLM advisor | Deferred to Phase 3c, never in decision loop |
| 6 | Withdrawal | v1 stub, real $ target deferred to live phase |
| 7 | Strategy death | Rolling 30d Sharpe < 0 AND 60d < 0.3 → weight→0.02 (auto). Full kill manual. |
| 8 | Kelly/intuition split | Capital=Kelly (risky=less), compound pace=rolling-Sharpe (risky=slower) |
| 9 | UX vocabulary | Drop "Fortress/Balanced/Assault". Use CONSERVATIVE/STANDARD/GROWTH/CUSTOM |

### A8 — Updated config schema (OVERRIDES §10)

```toml
[m3s]
enabled = false                 # sub-phase 0.8 ships with this false
phase = "shadow"
fallback = "fixed_weight"
state_db = "data/m3s.sqlite"

[m3s.mode]
current = "STANDARD"            # GROWTH after Prince approves BT#3
aggression_dial = 0.5

[m3s.rebalance]
cadence = "weekly"
weekly_anchor_utc = "SUN 23:59"
daily_anchor_utc  = "23:59"
min_trades_per_strategy = 20

[m3s.allocator]
method = "hrp_lite"
covariance_estimator = "ledoit_wolf"   # Tier 1 #4
signal_corr_window_bars = 720
signal_corr_cluster_threshold = 0.7
cash_buffer_min = 0.05
kelly_fraction_default = 0.50
kelly_uncertainty_aware = true         # Tier 1 #7

[m3s.compounder]
vol_target_annual = 0.15
vol_lookback_days = 30
hwm_gate = true
cvar_enabled = true                    # Tier 1 #5
cvar_confidence = 0.95
cvar_window_days = 60

[m3s.modes.CONSERVATIVE]
vol_target_annual = 0.10
max_per_strategy_cap = 0.25
max_cluster_cap = 0.40
dd_freeze_threshold = 0.05
dd_halt_threshold = 0.10
compound_cadence = "weekly"
compound_pace_floor = 0.20
compound_pace_ceiling = 0.60
auto_demote_dd_threshold = 0.05
auto_demote_target_mode = ""
kelly_fraction = 0.25
allocator_method = "inverse_vol"

[m3s.modes.STANDARD]
vol_target_annual = 0.15
max_per_strategy_cap = 0.40
max_cluster_cap = 0.65
dd_freeze_threshold = 0.08
dd_halt_threshold = 0.12
compound_cadence = "daily"
compound_pace_floor = 0.25
compound_pace_ceiling = 1.00
auto_demote_dd_threshold = 0.08
auto_demote_target_mode = "CONSERVATIVE"
kelly_fraction = 0.50
allocator_method = "hrp_lite"

[m3s.modes.GROWTH]
vol_target_annual = 0.22
max_per_strategy_cap = 0.50
max_cluster_cap = 0.75
dd_freeze_threshold = 0.10
dd_halt_threshold = 0.15
compound_cadence = "daily"
compound_pace_floor = 0.30
compound_pace_ceiling = 1.20
auto_demote_dd_threshold = 0.10
auto_demote_target_mode = "STANDARD"
kelly_fraction = 0.50
allocator_method = "hrp_lite"

[m3s.modes.CUSTOM]
i_accept_custom_mode_risk = false    # must be literally true to load
vol_target_annual = 0.20
kelly_fraction = 0.50
max_per_strategy_cap = 0.50
max_cluster_cap = 0.70
dd_freeze_threshold = 0.10
dd_halt_threshold = 0.15
compound_cadence = "daily"           # "per_trade" | "daily" | "weekly"
compound_every_n_trades = 1
compound_pace_floor = 0.25
compound_pace_ceiling = 1.00
auto_demote_enabled = true
auto_demote_dd_threshold = 0.12
auto_demote_target_mode = "STANDARD"
auto_demote_cooldown_hours = 48
auto_promote_back_threshold = 0.03
allocator_method = "hrp_lite"
aggression_dial = 1.0

[m3s.regime]                         # Tier 1 #1 (sub-phase 0.9)
enabled = true
classifier = "vol_adx_corr"
btc_vol_window_days = 30
adx_threshold = 25
crisis_correlation_threshold = 0.85
auto_mode_switch = true
mode_map.low_vol_trend = "GROWTH"
mode_map.normal      = "STANDARD"
mode_map.high_vol    = "CONSERVATIVE"
mode_map.crisis      = "CONSERVATIVE"

[m3s.edge_decay]                     # Tier 1 #2 (sub-phase 0.2)
enabled = true
sharpe_decay_threshold = 0.5         # 50% of lifetime Sharpe
winrate_decay_threshold = 0.2        # 20% drop
persistence_days = 14
auto_halve_enabled = true
auto_pause_enabled = true

[m3s.conviction]                     # Tier 1 #3 (sub-phase 0.5)
enabled = true
multiplier_floor = 0.5
multiplier_ceiling = 1.3
scorer = "distance_volume_mtf"

[m3s.evaluation]                     # Tier 1 #6,#7 (sub-phase 0.10)
purged_cv_embargo_pct = 0.01
kelly_uncertainty_floor_fraction = 0.20   # new strat = ⅕-Kelly
kelly_uncertainty_ceiling_fraction = 0.50 # mature strat = ½-Kelly
deflated_sharpe_alpha = 0.05

[m3s.advisor]                        # Phase 3c
enabled = false

[m3s.withdrawal]                     # v1 stub
enabled = false
```

### A9 — Updated test/success targets (§13/§17)

- Test count: ~150 → **~213** (40 new for Tier 1 #4–#7 + 20 regime + 3 CUSTOM + others)
- LOC: ~1,500 → **~3,300**
- Success criteria: unchanged (Sharpe ≥ 2.35 on meta-backtest), plus: Tier 1 #4 shrinkage must reduce allocation turnover ≥ 20% vs raw covariance; Tier 1 #5 CVaR must de-lever ≥ 30% during synthetic crash injection; Tier 1 #1 regime switching must not regress vs static STANDARD by more than 0.05 Sharpe on the BT#4 window.

### A10 — Sub-phase 0.1 scope (IN PROGRESS, Session 22)

**Goal:** skeleton + state layer only. Nothing wired into `main.py`. Nothing touches `src/risk/`.

**Files created:**
- `src/m3s/__init__.py` — public exports
- `src/m3s/types.py` — msgspec frozen structs: `PortfolioSnapshot`, `StrategySnapshot`, `AllocationDecision`, `CompoundState`, `ModeConfig`, `M3SEvent`
- `src/m3s/state.py` — SQLite store (two tables per §6), boot-safe init, put/get/event append/event query, migrations
- `src/m3s/modes.py` — `Mode` enum, `ModeConfig` dataclass, presets for CONSERVATIVE/STANDARD/GROWTH/CUSTOM, TOML loader with CUSTOM validation (5 safety rails from A4)

**Tests created (~15):**
- `test_types.py` — msgspec roundtrip, frozen invariants, numpy compat
- `test_state.py` — init idempotent, put/get scalar, put/get JSON, event append, event query by time, replay order
- `test_modes.py` — preset loading for all 4 modes, CUSTOM refuses without opt-in, CUSTOM rejects compound_every_n_trades<1, CUSTOM rejects demote-after-freeze config, CUSTOM force-enables HWM gate

**NOT in 0.1:** allocator, compounder, scheduler, hooks, regime, edge-decay, conviction, CVaR, shrinkage, purged CV, Bayesian Kelly — all later sub-phases.

---

## END v1.1 ADDENDUM — v1 body follows as research/reference context


---

## 1. Research Findings Summary (1 page)

**A — Capital allocation.** The literature converges on three ideas for multi-strategy allocation: (a) **fractional Kelly** (typically ¼ to ½) because full Kelly is catastrophically sensitive to estimation error on the mean — "errors in means are ~20× more important than errors in covariances" (MacLean/Thorp/Ziemba, "Good and Bad Properties of the Kelly Criterion," Berkeley); (b) **risk parity / Equal Risk Contribution** (AQR, "Understanding Risk Parity" and "Risk Parity, Risk Management, and the Real World") which require dynamic re-estimation and leverage but reliably outperform naive equal-weight out-of-sample; and (c) **Hierarchical Risk Parity (HRP)** (Lopez de Prado, "Building Diversified Portfolios that Outperform Out-of-Sample," JPM 2016; SSRN 2708678) which is **specifically designed to work on ill-conditioned or singular covariance matrices** — this is the right choice for 3 strategies with <6 months of live P&L. HRP does not require matrix inversion, uses clustering, and is robust to the cold-start problem.

**B — Compounding.** No respectable practitioner uses naive full geometric compounding on every trade. The dominant pattern at CTAs and multi-strat funds is **volatility-targeted notional with high-water-mark gating on the compounding step** — you scale notional to hit a target vol, and you only let the capital base grow above previous HWM. "Reduce capital when making subsequent losses; do not compound gains above HWM during drawdowns" (Mt. Cook Financial, qoppac.blogspot.com on HWM-based sizing). CTA rebalance cadences are weekly to monthly with 3-12 month vol lookbacks — **not per-trade** (CME "Managed Futures and Volatility"; SG CTA Index rebalances annually). Per-trade compounding is a retail pathology.

**C — LLM as advisor.** 2024–2025 literature (Frontiers in AI 2025 "LLMs in equity markets"; arXiv 2510.05533 "The New Quant"; SSRN 5934015 "LLMs for Quantitative Investment Research: Practitioner's Guide") is nearly unanimous: **LLMs belong in research, signal generation, constraint elicitation, and narrative-layer commentary — not in the real-time sizing loop.** MarketSenseAI and AlphaAgents treat LLMs as an *upstream* input that feeds a *classical optimizer under exposure and turnover controls*. **No production fund lets an LLM decide a position size.** This directly contradicts the current `M3S_SPEC.md` positioning of "The PhD" as an every-30-minute authoritative advisor.

**D — Correlation.** Chan (Algorithmic Trading, Wiley 2013) is explicit: "estimates of correlations can be even worse than that of mean and variance... one shortcut is to assume correlation is zero except when strategies are very similar, in which case assume 100%." Correlations go to 1 in crisis (BIS 2008 "Evaluating correlation breakdowns"; Swan Global "The Correlation Conundrum"). **Signal correlation (are we exposed at the same time, same direction?) is the right metric for same-market strategies, not PnL correlation** (Quantfish Research, "Why PnL Correlation Fails for Same-Market Strategy Portfolios") — this is a critical finding because all three live strategies trade crypto altcoins, partially overlapping symbols.

**E — What changed the design vs what confirmed it.**
- **Changed:** (1) The LLM advisor should be **v2, not v1**. (2) "4 modes" collapses to **2 modes + 1 dial**. (3) BASE vs PROFIT pool is **replaced by HWM-gated compounding with vol targeting** — same intent, half the state, industry-standard. (4) **Signal correlation, not PnL correlation**, for the correlation cap. (5) Prince's "risky=bigger capital, slower compound" intuition **partially inverts Kelly** — details in §7/§8.
- **Confirmed:** Fractional Kelly (½ Kelly max), HRP-style clustering for allocation, drawdown-gated scale-down, weekly rebalance cadence, structured-JSON LLM output if/when advisor is built.

**Disagreement between personas:** PE wants to ship v1 without the LLM at all. PM agrees on cutting the LLM from the decision loop but wants a **shadow advisory** running from day 1 so we have a 3-month track record before promotion. **Resolution proposed:** §11 rollout phases — LLM enters in phase 2 as read-only shadow, never in phase 1.

---

## 2. Compounding Taxonomy Table

| # | Policy | Formula (in one line) | When it works | When it fails | Compute cost | M3S use? |
|---|---|---|---|---|---|---|
| 1 | **Full geometric (per-trade)** | `size_t = equity_t × risk_pct` | Stable edge, long horizon, low vol | Drawdowns compound downward just as fast; retail pathology | O(1) | **No.** Reject. |
| 2 | **Fixed-fractional, periodic resync** | `equity_base ← equity` every N days; size off `equity_base` | Simple, predictable, prevents intra-period feedback loops | Lag during strong runs; discontinuity at resync | O(1) | **Fallback only.** |
| 3 | **BASE/PROFIT pool (Prince's sketch)** | Two explicit pools; profits → PROFIT; PROFIT compounds | Conceptually intuitive; psychologically clean | Extra state, extra bugs, no real benefit over #5 when HWM is tracked | O(1) | **No** — superseded by #5. |
| 4 | **Volatility-targeted notional** | `notional = equity × (vol_target / realized_vol)` | CTAs, managed futures — produces stationary risk profile | Needs clean vol estimate; estimator noise near zero vol | O(window) | **Yes — portfolio layer.** |
| 5 | **HWM-gated compounding** | `base_t = max(base_{t-1}, equity_t)` only if no DD cap breached; else `base_t = base_{t-1}` | Mimics HF fee mechanics; blocks compounding during DD; simpler than pools | Needs explicit rule for recovery beyond HWM | O(1) | **Yes — portfolio default.** |
| 6 | **Vol-adjusted compounding** | `compound_pct = f(rolling_Sharpe)`, e.g., 0 below 1.0, 1.0 above 2.0 | Rewards edge, not luck; reduces overfitting to streaks | Sharpe is noisy on small samples; lag | O(window) | **Yes — per-strategy layer.** |
| 7 | **Drawdown-gated compounding** | If `dd > dd_threshold`: `compound_pct = 0` | Hard safety rail against doubling down | Whipsaws if threshold too tight | O(1) | **Yes — combined with #5.** |
| 8 | **Withdrawal policy** | `if equity > 1.5 × HWM_base: withdraw (equity - 1.2×HWM_base)` | Actually banks profit; prevents give-back | Out of scope for paper engine, required for live | O(1) | **v1 stub; v2 enforce.** |

**Default M3S compounding = #5 (HWM-gated) + #4 (vol-targeted notional) + #7 (DD gate) + optional #6 per-strategy.** This is the standard CTA stack. Numbers 1, 3 are rejected. Number 8 is a v1 stub.

---

## 3. Architecture Diagram

```
               ┌──────────────────────────────────────────────────────┐
               │                    TradingEngine                    │
               │                    (src/main.py)                    │
               └───────────────────────┬──────────────────────────────┘
                                       │ on candle
                        ┌──────────────▼──────────────┐
                        │     StrategyRouter          │
                        │  (bb_rsi_mr_opt, donchian,  │
                        │   vol_momentum → Signal)    │
                        └──────────────┬──────────────┘
                                       │ Signal
                   ┌───────────────────▼───────────────────┐
                   │            M3S Allocator              │  ◄── reads
                   │  (src/m3s/allocator.py, in-process)   │     M3S state
                   │  - per-strategy weight w_i            │     (SQLite)
                   │  - scales signal.risk_pct × w_i       │
                   └───────────────────┬───────────────────┘
                                       │ Signal (risk_pct scaled)
                                       ▼
                        ┌─────────────────────────────┐
                        │   RiskClient (ZMQ)          │
                        │   → RiskServer process      │
                        │     (manager.py — 7 checks) │
                        │   NOTHING CHANGED HERE      │
                        └──────────────┬──────────────┘
                                       │ RiskDecision
                                       ▼
                        ┌─────────────────────────────┐
                        │   PaperExecutor → Fill      │
                        └──────────────┬──────────────┘
                                       │ Fill / trade_close
                                       ▼
               ┌──────────────────────────────────────────────────────┐
               │            M3S Portfolio Tracker                    │
               │        (src/m3s/portfolio.py)                       │
               │  - equity, HWM, drawdown                            │
               │  - per-strategy rolling Sharpe / vol / trade count  │
               │  - correlation matrix (signal-level, rolling)       │
               └──────────────────┬───────────────────────────────────┘
                                  │ (daily close, scheduled)
                                  ▼
               ┌──────────────────────────────────────────────────────┐
               │                M3S Scheduler (async)                │
               │  1. recompute allocation (HRP-lite)                 │
               │  2. update compounding base (HWM-gated)             │
               │  3. publish new weights to Allocator                │
               │  4. (v2) run LLM advisor, log shadow recommendation │
               └──────────────────────────────────────────────────────┘
```

**Key invariant:** M3S sits **in front of** the RiskClient, not inside the risk process. It can only *shrink* a signal's risk_pct and *shift* weights between strategies. It cannot bypass any risk gate. The ZMQ risk server is untouched.

---

## 4. Module List (minimum viable)

Under `src/m3s/`:

| File | One-line responsibility |
|---|---|
| `portfolio.py` | Live portfolio state: equity, HWM, drawdown, per-strategy rolling Sharpe/vol/trade count, signal-exposure matrix |
| `allocator.py` | Compute per-strategy weights (HRP-lite: inverse-vol within signal-correlation clusters) |
| `compounder.py` | HWM-gated compounding base + vol-targeted notional scalar |
| `mode.py` | Mode definitions (Conservative / Standard + aggression dial), resolution, safe transitions |
| `scheduler.py` | Async weekly-close task: recompute allocation, update HWM, publish weights, log decision |
| `state.py` | SQLite persistence, event log, crash recovery, migrations |
| `hooks.py` | Single integration surface: `M3S.on_signal(sig)`, `M3S.on_fill(fill)`, `M3S.on_trade_close(...)` |
| `meta_backtest.py` | Offline simulator: replays historical per-strategy trade logs through M3S to validate vs fixed-weight |

**Deferred to v2:** `advisor.py` (LLM), `telegram.py`. Correlation logic folds into `portfolio.py` (40 lines of rolling numpy, doesn't deserve its own module).

**8 files, not the original 14.** Resist module bloat.

---

## 5. Per-Module Interface Sketches

### `portfolio.py`
```python
class PortfolioSnapshot(msgspec.Struct, frozen=True):
    ts_ms: int
    equity: float
    hwm: float
    drawdown_pct: float
    per_strategy: dict[str, "StrategySnapshot"]
    signal_corr: dict[tuple[str, str], float]

class StrategySnapshot(msgspec.Struct, frozen=True):
    name: str
    n_trades_30d: int
    rolling_sharpe_30d: float
    realized_vol_30d: float
    exposure_series: list[int]
    pnl_30d: float

class PortfolioTracker:
    def on_fill(self, fill: Fill, strategy: str) -> None: ...
    def on_trade_close(self, strategy: str, pnl: float, ts_ms: int) -> None: ...
    def on_bar_exposure(self, strategy: str, symbol: str, sign: int) -> None: ...
    def snapshot(self) -> PortfolioSnapshot: ...
```

### `allocator.py`
```python
class AllocationDecision(msgspec.Struct, frozen=True):
    ts_ms: int
    weights: dict[str, float]
    method: str                # "cold_start_equal" | "inverse_vol" | "hrp_lite"
    inputs_hash: str
    reasoning: str

class Allocator:
    def __init__(self, mode: Mode, min_history_trades: int = 20): ...
    def compute(self, snapshot: PortfolioSnapshot) -> AllocationDecision: ...
    # Cold-start path: equal weights until min_history_trades per strategy
    # Mature path:
    #   1. cluster strategies by signal_corr > 0.7
    #   2. within cluster: inverse-vol weight
    #   3. across clusters: inverse-vol at cluster level
    #   4. apply mode.max_per_strategy_cap and mode.max_cluster_cap
    #   5. residual → cash buffer
```

### `compounder.py`
```python
class CompoundState(msgspec.Struct):
    base_equity: float
    hwm: float
    last_updated_ts_ms: int
    mode: str

class Compounder:
    def risk_scalar(self, snapshot: PortfolioSnapshot) -> float:
        """Returns multiplier in [0.3, 1.5] applied to Signal.risk_pct.
        Logic:
          - scalar = vol_target / realized_portfolio_vol  (clamped 0.5..1.5)
          - if drawdown > mode.dd_freeze_threshold: scalar *= 0.5
          - if drawdown > mode.dd_halt_threshold:   scalar = 0.0
        """

    def update_base(self, snapshot: PortfolioSnapshot) -> CompoundState:
        """Called at weekly close.
           if snapshot.drawdown_pct == 0 and snapshot.equity > state.hwm:
               state.hwm = snapshot.equity
               state.base_equity = snapshot.equity
           else:
               pass  # frozen — do not compound during DD
        """
```

### `mode.py`
```python
class Mode(enum.Enum):
    CONSERVATIVE = "CONSERVATIVE"
    STANDARD = "STANDARD"

@dataclass(frozen=True)
class ModeConfig:
    name: Mode
    vol_target_annual: float
    max_per_strategy_cap: float
    max_cluster_cap: float
    dd_freeze_threshold: float
    dd_halt_threshold: float
    rebalance_cadence: str
    risk_mode_hint: str          # underlying RiskMode to request
    aggression_dial: float       # 0..1 continuous dial within the mode
```

### `hooks.py`
```python
class M3S:
    """Single integration surface touched by main.py."""
    def on_signal(self, sig: Signal) -> Signal:
        """MUTATES sig.risk_pct:
             sig.risk_pct *= allocator.weights[sig.strategy_name] * compounder.risk_scalar()
           Returns mutated signal. NEVER rejects — only shrinks.
           If shadow_mode=True: logs proposed risk_pct but returns original.
        """
    def on_fill(self, fill: Fill, strategy: str) -> None: ...
    def on_trade_close(self, strategy: str, pnl: float, symbol: str, ts_ms: int) -> None: ...
    def on_bar(self, strategy: str, symbol: str, exposure: int) -> None: ...
    def snapshot(self) -> PortfolioSnapshot: ...
    async def run_scheduler(self) -> None: ...
```

---

## 6. State and Persistence

**Single SQLite database.** Two tables.

```sql
CREATE TABLE m3s_state (
    namespace TEXT NOT NULL,     -- 'compound' | 'allocation' | 'mode'
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,     -- JSON
    updated_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE m3s_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,    -- 'allocation' | 'compound_update' | 'mode_transition'
    payload TEXT NOT NULL,       -- JSON versioned
    inputs_hash TEXT
);
```

**Crash recovery:** boot reads `m3s_state`. If any key is missing, fall back to fixed-weight 40/30/30 and log `STARTUP_DEGRADED`. **Never fail closed on missing M3S state** — fail to fixed-weight. The risk server is the real safety rail.

**No BASE/PROFIT pool tables.** HWM-gated compounding reduces two pools to `(hwm, base_equity)` — two scalars.

---

## 7. Mode Definitions Table

**Decision: 2 modes + 1 continuous `aggression_dial`, not 4.**

| Dial | **CONSERVATIVE** | **STANDARD** |
|---|---|---|
| When used | First 60 days live; after any >8% DD | Default after 60+ trades, positive rolling Sharpe |
| `vol_target_annual` | 10% | 20% |
| `max_per_strategy_cap` | 0.25 | 0.40 |
| `max_cluster_cap` | 0.40 | 0.65 |
| `dd_freeze_threshold` | 0.05 | 0.08 |
| `dd_halt_threshold` | 0.10 | 0.12 |
| `rebalance_cadence` | weekly | weekly |
| `risk_mode_hint` | DEFENSIVE | BALANCED |
| `aggression_dial` default | 0.3 | 0.5 |
| `allocator_method` | `inverse_vol` | `hrp_lite` |
| `llm_advisor` (v2) | shadow only | shadow → advisory after 3mo |
| Cold-start | equal until 20 trades/strategy | equal until 30 trades/strategy |

**Why 2 not 4:**
1. **Fortress + Balanced** differ only by numeric dials → collapse to one mode + dial.
2. **Assault** (2% risk/trade, full Kelly) contradicts every research source. Full Kelly on 3 strategies with <6 months history is negative-EV. **Reject outright.**
3. **AI Adaptive** isn't a mode, it's a mechanism (advisor adjusting dial). Collapses to "STANDARD with LLM-driven aggression_dial". Ship v1 without.

**Mode transitions:**
- Manual (Prince): always allowed.
- Auto STANDARD → CONSERVATIVE: triggered when `drawdown_pct > 0.08` for 24h.
- Auto CONSERVATIVE → STANDARD: **never.** Prince's explicit command required. ("Re-upping after a drawdown is the most emotional decision; never automate it.")

---

## 8. Per-Mode Compounding Policy

**Portfolio level (both modes):**
```
At weekly close (Sunday 23:59 UTC):
    if drawdown_pct <= mode.dd_freeze_threshold:
        if equity > state.hwm:
            state.hwm = equity
            state.base_equity = equity   # compound
    else:
        frozen — no compounding during drawdown
```

**Per-signal risk scalar (every signal):**
```
portfolio_scalar = clamp(vol_target / realized_vol_30d, 0.5, 1.5)
if drawdown_pct > mode.dd_freeze_threshold:
    portfolio_scalar *= 0.5
sig.risk_pct *= allocator.weights[sig.strategy_name] × portfolio_scalar
```

### Prince's "risky = bigger capital, slower compound" intuition — validate or refute

**Verdict: partially inverts Kelly, but contains a defensible core.**

- Kelly says: allocate capital proportional to `edge / variance`. High-variance (risky) strategy → **less** capital, not more.
- Prince's intuition says: risky strategy → **more** capital.
- **These contradict on the capital axis.**
- **BUT** on the compounding axis, Prince is correct and under-appreciated: high-variance strategies have less-reliable Sharpe estimates, so compounding from their gains is more likely to be streak-luck than edge → compound slower. This is exactly vol-adjusted compounding (#6 in §2).

**Resolution:**
- **Capital allocation follows Kelly/HRP** — risky strategies get *less* notional, not more. (Prince's intuition overridden here.)
- **Compounding pace follows Prince's intuition** — per-strategy compound rate is `f(rolling_Sharpe)`. The strategy with the cleanest realized Sharpe compounds fastest. Risky high-variance strategy → noisier Sharpe → slower compounding. ✅ Prince's intuition, expressed correctly.

PM emphasis: write this rule down now, because in month 3 when `vol_momentum` has a 15% month, we will be tempted to let it compound aggressively.

---

## 9. Integration Hooks (Line-Level Pointers)

All changes additive and reversible. **Nothing in `src/risk/` is modified.**

**1. `src/main.py`** — 5 insertion points:
- Line ~32: `from src.m3s.hooks import M3S`
- Line ~70 (init): `self.m3s: M3S | None = None`
- Line ~151 (signal loop, before `self.risk_client.check_signal(sig)`):
  ```python
  if self.m3s:
      sig = self.m3s.on_signal(sig)
  decision = self.risk_client.check_signal(sig)
  ```
- Line ~170 (fill handler): `self.m3s.on_fill(fill, sig.strategy_name)`
- Line ~189 (init section): construct `M3S` if `cfg.m3s.enabled`
- Line ~382 (task launch): `asyncio.create_task(self.m3s.run_scheduler())`

**2. `src/strategies/router.py`** — one new line: emit per-bar exposure sign to `m3s.on_bar(...)` after each strategy's `on_candle` returns.

**3. `config/m3s.toml`** — NEW FILE. See §10.

**4. `src/risk/*`** — UNCHANGED. Sacred.

**5. `src/utils/storage.py`** — add two table migrations (m3s_state, m3s_events).

**6. `scripts/m3s_cli.py`** — NEW. One-command ops: `m3s status`, `m3s freeze`, `m3s thaw`, `m3s replay`, `m3s meta-backtest`.

**Total diff estimate:** ~80 lines in existing files, ~1500 lines of new `src/m3s/` code, ~200 lines of tests per module.

---

## 10. Config Schema — `config/m3s.toml`

```toml
[m3s]
enabled = true
phase = "shadow"            # "shadow" | "advisory" | "authoritative"
fallback = "fixed_weight"
state_db = "data/m3s.sqlite"

[m3s.mode]
current = "CONSERVATIVE"
aggression_dial = 0.3       # 0.0..1.0 continuous

[m3s.rebalance]
cadence = "weekly"          # research-backed default
weekly_anchor_utc = "SUN 23:59"
min_trades_per_strategy = 20

[m3s.allocator]
method = "hrp_lite"
signal_corr_window_bars = 720
signal_corr_cluster_threshold = 0.7
cash_buffer_min = 0.05

[m3s.compounder]
vol_target_annual = 0.15
vol_lookback_days = 30
hwm_gate = true
dd_freeze_threshold = 0.06
dd_halt_threshold = 0.10

[m3s.modes.CONSERVATIVE]
vol_target_annual = 0.10
max_per_strategy_cap = 0.25
max_cluster_cap = 0.40
dd_freeze_threshold = 0.05
dd_halt_threshold = 0.10
risk_mode_hint = "DEFENSIVE"
allocator_method = "inverse_vol"

[m3s.modes.STANDARD]
vol_target_annual = 0.20
max_per_strategy_cap = 0.40
max_cluster_cap = 0.65
dd_freeze_threshold = 0.08
dd_halt_threshold = 0.12
risk_mode_hint = "BALANCED"
allocator_method = "hrp_lite"

[m3s.advisor]                # v2 — disabled at launch
enabled = false
model_routine = "claude-haiku-4"
model_daily = "claude-sonnet-4"
daily_cost_cap_usd = 3.0
invocation = "daily_close_only"
fail_open = true

[m3s.withdrawal]             # v1 stub
enabled = false
target_overflow_ratio = 1.2
destination = "log_only"
```

**Hard rule:** M3S reads config at boot and on explicit reload. **No runtime file mutation by M3S.** A system that rewrites its own config is un-debuggable.

---

## 11. Rollout Phases

**Phase 0 (week 0): merged, disabled.** Code in, `enabled=false`, unit tests green, no runtime effect.

**Phase 1 — SHADOW (weeks 1–4)**
- `phase = "shadow"`, `enabled = true`
- M3S logs proposed scaled risk_pct and weights to `m3s_events`, **returns the original signal unchanged**
- Allocator + compounder + portfolio tracker all run fully
- Dashboard shows "what M3S would have done" vs what actually happened
- **Gate to Phase 2:**
  - 4 weeks continuous shadow, zero crashes
  - Meta-backtest shows projected Sharpe ≥ 2.318 on shadow window
  - Zero instances of proposed risk_pct exceeding RiskConfig caps
  - Reconstructed equity matches paper executor equity within ≤ 0.5%

**Phase 2 — ADVISORY (weeks 5–8)**
- M3S *shrinks* risk_pct on approved signals (only downward; never up)
- Weights applied at weekly rebalance
- LLM advisor wired in as read-only shadow — logs JSON, never touches decision loop
- **Gate to Phase 3:**
  - 4 weeks live scaling, portfolio equity ≥ shadow-baseline equity
  - No false DD freezes
  - Prince explicit approval after reviewing 4 weeks of decision logs

**Phase 3 — AUTHORITATIVE (weeks 9+)**
- M3S can request RiskMode flips via `risk_client.set_mode()` — only CONSERVATIVE→DEFENSIVE, STANDARD→BALANCED. **Never AGGRESSIVE. Ever.**
- LLM advisor (if enabled) remains read-only; promoting it is a separate decision Prince owns.

**One-command rollback:** `python -m scripts.m3s_cli freeze` → sets `phase=shadow`, `mode=CONSERVATIVE`, halts new weight publication. Existing positions untouched.

---

## 12. Meta-Backtest Design

**Problem:** Before M3S touches live paper, prove it beats fixed-weight 40/30/30 (Sharpe 2.318) on historical trade logs.

**Design:**
1. **Data:** Per-strategy trade logs from Session 11 optimized backtest, 2023–2025.
2. **Simulator (`src/m3s/meta_backtest.py`):** Replay trades chronologically across the 3 strategies. At each trade, update portfolio state as if paper executor just produced the fill. Run scheduled rebalances (weekly close) between trades. Scale trade PnL by `weight × scalar`.
3. **Baselines:** (a) fixed-weight 40/30/30, (b) inverse-vol, (c) M3S CONSERVATIVE, (d) M3S STANDARD.
4. **Metrics:** Portfolio Sharpe, MDD, Calmar, time-in-drawdown %, turnover, cash buffer utilization.
5. **Stress test:** Inject synthetic -10% month on bb_rsi_mr_opt; verify DD freeze + weight shift.
6. **Pass bar:** STANDARD mode must achieve **Sharpe ≥ 2.32 AND MDD ≤ baseline MDD × 1.10.** If M3S raises Sharpe at the cost of worse drawdown, reject.

Meta-backtest runs in CI on every commit to `src/m3s/`.

---

## 13. Test Plan

| Tier | File | Count | What |
|---|---|---|---|
| Unit | `test_portfolio.py` | ~25 | HWM updates, rolling Sharpe/vol, signal corr, snapshots |
| Unit | `test_allocator.py` | ~30 | Cold-start, inverse-vol, HRP-lite golden vectors, caps, cash buffer, NaN handling |
| Unit | `test_compounder.py` | ~20 | HWM gate, vol-target scalar bounds, DD freeze, crash recovery |
| Unit | `test_mode.py` | ~10 | Mode resolution, aggression dial interpolation, refused transitions mid-DD |
| Unit | `test_state.py` | ~15 | SQLite migrations, event append idempotency, replay |
| Integration | `test_hooks.py` | ~15 | `on_signal` mutates risk_pct correctly, shadow mode unchanged, fake RiskClient |
| Simulation | `test_meta_backtest.py` | ~10 | Beats fixed-weight on golden 2023–2025 dataset; synthetic DD correct |
| Property (Hypothesis) | `test_properties.py` | ~12 | sum(weights)+cash==1, no weight > cap, scaled_risk ≤ original |
| Chaos | `test_chaos.py` | ~10 | Advisor down, ZMQ disconnect, SQLite locked, corrupted state, clock skew |
| Parity | `test_parity.py` | ~6 | Meta-backtest ≡ live M3S on same trade stream (bit-exact) |

**~150 new tests total.**

---

## 14. Observability

**Structured logs (structlog JSON):**
- `m3s.signal_scaled` — strategy, original/scaled risk_pct, weight, scalar, phase
- `m3s.allocation_recomputed` — weights, method, inputs_hash, reasoning
- `m3s.compound_update` — old_base, new_base, hwm, dd_pct, frozen
- `m3s.mode_transition` — old/new/reason/initiator
- `m3s.advisor_shadow` (v2) — prompt_hash, response, cost, latency

**Streamlit dashboard — new M3S page:**
- Current mode + aggression_dial (headline)
- Current weights (bar chart vs fixed-weight baseline)
- HWM line + equity line
- DD % with freeze/halt thresholds overlaid
- Per-strategy rolling Sharpe (30d) + realized vol
- Signal correlation heatmap (3×3)
- Shadow delta (phase 1): "M3S would have scaled last 20 signals by X%"
- Last 10 allocation decisions with `reasoning`

**Alerts (log-ingest; Telegram v2):**
- `M3S_STATE_CORRUPT`
- `M3S_DD_FREEZE`
- `M3S_DD_HALT_APPROACH`
- `M3S_CLUSTER_COLLAPSE`
- `M3S_ALLOCATION_DIVERGED` (proposed vs published > 20%)

---

## 15. Rollback Plan

**One command, any phase, any time:**
```bash
python -m scripts.m3s_cli freeze --reason "manual"
```

**Action:**
1. Writes `phase=shadow`, `mode.current=CONSERVATIVE` to `config/m3s.toml`
2. Sends `RELOAD_M3S_CONFIG` signal to running engine
3. M3S stops publishing new weights
4. Existing positions **not** touched (liquidating on rollback is worse than the bug)
5. Risk server continues untouched
6. Logs `M3S_FROZEN` event

**Reversal:** `m3s thaw --confirm` (explicit confirm flag required).

---

## 16. Open Questions for Prince

1. **Capital base.** Paper nominal $10k as the compounding base, or virtual larger base for stress-testing the math?
2. **Rebalance day.** Sunday 23:59 UTC — or Monday open local time?
3. **Signal correlation window.** 30 days × 1h bars = 720 observations. OK or too short?
4. **Auto-thaw.** Confirm no auto CONSERVATIVE→STANDARD — or give exact rule (e.g., "30 days clean above HWM").
5. **LLM v2 scope.** When advisor comes in: (a) comment only, (b) nudge dial ±0.1, or (c) propose mode for approval? Recommendation: (a) until 3mo shadow history, then (b).
6. **Withdrawal target.** Real dollar target for v2? ("After +30% take 20% off.")
7. **Strategy death criteria.** Default: rolling 30d Sharpe < 0 AND 60d Sharpe < 0.3 → weight→0.02. Kill is manual. OK?
8. **The Prince-intuition split (§8).** Confirm: capital follows Kelly (risky = less), compounding follows your instinct (risky = slower).
9. **UX vocabulary.** Preserve 4-mode names externally ("Fortress" = CONSERVATIVE+dial=0.0, "Assault" = REFUSED) or drop them entirely?

---

## 17. Success Criteria

**Quantitative (all required before phase 3):**
- Meta-backtest portfolio Sharpe ≥ **2.35** on 2023–2025 data
- Meta-backtest MDD ≤ baseline MDD × 1.10
- Meta-backtest Calmar ≥ baseline Calmar
- Phase 2 equity after 8 weeks ≥ parallel fixed-weight shadow
- Zero instances of M3S producing `risk_pct` > original
- Zero instances of risk server rejecting an M3S-upscaled signal (proves M3S only shrinks)
- Scheduler missed-rebalance rate < 1%
- Test suite 100% green, ≥ 85% coverage on `src/m3s/`

**Qualitative:**
- Prince can explain any day's M3S decision in **one sentence** backed by a log line
- Signal-to-fill trace readable in < 60 seconds from logs
- Rollback is one command and works under stress
- No state Prince is afraid to inspect manually in SQLite

---

## 18. Out of Scope for v1

1. **LLM advisor in decision loop.** Shadow-only in phase 2.
2. **Telegram bot.** Phase 3 minimum.
3. **Options Greeks risk.** Phase 6.
4. **Cross-venue allocation.** Phase 4 prerequisite.
5. **Tax / withdrawal execution.** v1 stub only.
6. **Portfolio margining.** Not for paper.
7. **Real-money capital.** Phase 3-milestone must pass.
8. **Regime detection via HMM/Hurst.** Rolling-vol clustering only in v1.
9. **Mean-variance / Markowitz.** Rejected — HRP-lite supersedes.
10. **Full Kelly or > ½ Kelly.** Not even configurable.
11. **Intraday / per-trade rebalance.** Weekly only. Explicitly rejected by research.
12. **Plugin framework for allocators.** Build one.
13. **Custom modes beyond CONSERVATIVE/STANDARD.** Use `aggression_dial`.
14. **Auto CONSERVATIVE→STANDARD.** Prince-only.
15. **Auto strategy kill.** Weight-to-min auto; kill manual.

---

## Where Engineer and PM Disagreed

| Issue | PE | PM | Resolution |
|---|---|---|---|
| Number of modes | 1 + dial | Named safe harbor for crisis | **2 + dial** (CONSERVATIVE is the "big red button") |
| LLM in v1 | Delete entirely | Shadow from day 1 for track record | **v1=off, phase 2=shadow, phase 3=Prince decides** |
| Rebalance cadence | Configurable | Weekly only (CTA standard) | **Weekly default, daily allowed in config, per-trade forbidden** |
| Prince's capital intuition | Accept (user simplicity) | Refuse (inverts Kelly) | **Kelly wins on capital axis; Prince wins on compounding axis** |
| Telegram in v1 | Neutral | Distraction from log discipline | **v2** |

Where Prince's intuition was overridden, it was overridden with research citations and a concrete alternative that captures the actual intent. Where confirmed, the rigorous version is implemented.

---

## Sources

- Lopez de Prado — Building Diversified Portfolios that Outperform Out-of-Sample (SSRN 2708678, JPM 2016)
- Antonov, Lipton, Lopez de Prado — Overcoming Markowitz's Instability with HRP (SSRN 4748151, 2024)
- MacLean, Thorp, Ziemba — Good and Bad Properties of the Kelly Criterion (Berkeley)
- AQR — Understanding Risk Parity / Risk Parity, Risk Management and the Real World
- CME — Managed Futures and Volatility
- CME/JPMorgan — Momentum Strategies Across Asset Classes
- Mt. Cook Financial — Different Flavors of High Water Marks
- qoppac — Using maximum drawdowns to set capital sizing
- Quantfish — Why PnL Correlation Fails for Same-Market Strategy Portfolios
- BIS — Evaluating correlation breakdowns during periods of market volatility
- Frontiers in AI 2025 — LLMs in equity markets
- arXiv 2510.05533 — The New Quant: Survey of LLMs in Financial Prediction and Trading
- SSRN 5934015 — LLMs for Quantitative Investment Research: Practitioner's Guide
- arXiv 2508.11152 — AlphaAgents: LLM-based Multi-Agents for Equity Portfolio Construction
- Chan — Quantitative Trading (2009), Algorithmic Trading (2013)
- Hierarchical Risk Parity — Wikipedia
