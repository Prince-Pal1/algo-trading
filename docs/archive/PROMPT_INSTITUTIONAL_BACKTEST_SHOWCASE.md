> ⚠️ **ARCHIVED — COMPLETED (Session 8, moved Session 21)**
> One-time spec, fully delivered. See [ROADMAP.md](../../ROADMAP.md) for current status.

---

# Prompt for Claude Code — Institutional Backtest & Showcase (Revised Spec)

> **Status (April 12, 2026):** EXECUTED. All 5 phases implemented in Session 8. This file is the original prompt/spec — kept for reference.

Copy everything below the line into Claude Code (or attach this file). Use it as the **authoritative refinement** of the original “Institutional-Grade Backtesting & Strategy Showcase System” idea.

---

## Role

You are a senior quant engineer. Your job is to **plan and implement** (or advise on) an institutional-style backtesting validation and results showcase layer for the repo at `/Users/prince/algo-trading/`, integrating with existing pieces:

- `BaseStrategy` / `strategy.on_features() -> Signal` (identical backtest vs live intent)
- Custom backtest engine (`src/backtest/engine.py` if present)
- `StrategyValidator` + protocols (`src/backtest/validator.py`)
- Strategy research catalog (`src/research/catalog.py`, `config/catalog.toml`)
- SQLite `validation_results` (or equivalent) linked to catalog entries
- Stack: Python 3.12+, msgspec, structlog, TOML, SQLite; async where it helps I/O; **CLI** entry (`python -m ...`)

Do **not** assume files exist without checking the tree. Adapt paths to match reality.

---

## North Star (Keep)

- Tiered testing: **lite** (dev sanity), **standard** (regular CI), **intense** (pre-live gate), **research** (deep, optional).
- Structured **run metadata + artifacts** (not only ASCII tables): versioning, comparison over time, export JSON/HTML.
- Visualization: **Plotly** for interactive; **matplotlib** optional for static; prefer **self-contained HTML** early over heavy hosted frameworks.
- Modular design: **one protocol per testing mode**, **one renderer per chart family**, shared **metrics** module to avoid drift.
- Literature alignment: Bailey & López de Prado themes — **overfitting**, **multiple testing**, **purged CV**, **deflated / probabilistic Sharpe** where feasible.

---

## Critical Guardrails (From Prior Review — Must Respect)

### 1. Scope — Ship in Vertical Slices

The full original spec is **too large for one PR**. Implement in **ordered releases** with working software after each:

| Slice | Goal |
|-------|------|
| **v0.1** | Canonical **metrics** module + **run manifest** (msgspec/dataclass): strategy version hash, param hash, data fingerprint (symbol, timeframe, source, date range), git commit or artifact id, engine version. **Hybrid DB**: indexed columns for query + JSON blob for nested detail. Export **JSON** + one **Plotly HTML** template: equity + underwater (drawdown). |
| **v0.2** | **Lite + Standard** protocols only; CLI `validate --strategy ... --mode lite|standard`. |
| **v0.3** | **Comparison** (multi-run): overlay equity, side-by-side table; **parameter sensitivity** with **hard evaluation budget** (max grid points; consider 1-D sweeps or Sobol/LHS for high-D — not full Cartesian explosion). |
| **v0.4** | **Walk-forward / OOS** with **purging + embargo** documented and implemented (López de Prado-style leakage control). No “optimize on full sample then pretend OOS.” |
| **v0.5** | **Monte Carlo**: use a **named, defensible** method — e.g. **block bootstrap** on returns (choose block length, document autocorrelation tradeoff). **Do not** implement naive “shuffle trades” or randomize timestamps in ways that break path dependence unless you explicitly model path-independent statistics and document limitations. |
| **v1.0** | **Stress windows** defined **per asset class** (crypto crashes ≠ only lens for XAUUSD/forex — include relevant macro shocks for each). **Regime** tests require a **frozen regime definition** (rule code + parameters) stored in run metadata. |

Defer to post-v1 unless trivial add-on: full **factor attribution** (needs explicit factor set per asset class), **hedge-fund-style capacity** (needs microstructure or honest slippage-notional sensitivity only).

### 2. Statistics & Methodology

- **Walk-forward + parameter search**: nested design; **purge** overlapping samples; **embargo** after events; report **IS vs OOS degradation** without cherry-picking only the best fold.
- **Pass/fail “traffic lights”**: avoid binary theater. Prefer **graded status** + **reason codes**; thresholds may depend on strategy class (trend vs mean reversion). If using **Probabilistic Sharpe** or **Deflated Sharpe**, cite definition and inputs.
- **Correlation / factors**: any “vs SPX/VIX” for non-equity strategies must be **justified** (hypothesis, alignment, lags). Prefer a **small benchmark set** over noisy many-series mining.
- **Capacity**: v1 = **slippage / fee sensitivity + max notional assumptions** unless real LOB data exists. Do not claim AUM capacity without a model.

### 3. Engineering

- **Async**: use for **I/O** (download, DB, serving). **CPU-bound** sweeps / MC / WFO: use **process pool** or joblib-style parallelism; do not claim asyncio accelerates numpy loops.
- **Single source of truth** for metrics: same functions feed **validator**, **CLI tables**, **plots**, **exports**.
- **Reproducibility**: record **random seeds**, library versions, and deterministic sweep ordering where applicable.
- **CLI** (design): e.g. `backtest validate`, `backtest report --run-id`, `backtest compare --runs`, `backtest export --format html|json`.
- **PDF export**: optional / later; **HTML first** (fewer encoding pitfalls).

### 4. Visualization Architecture

- MVP charts: **equity**, **underwater**, **monthly returns heatmap**, **rolling Sharpe** (or rolling PF), **trade P&L histogram**.
- Phase later: **3D sensitivity surfaces**, **radar/spider** comparison (only after comparison UX is stable).
- Consider **Streamlit** OR static HTML first; add **Dash** only if multi-user hosting is required.

### 5. Catalog Policy

- Catalog entries should declare **eligible modes** / **stress sets** (e.g. crypto-specific vs gold-specific) to avoid running irrelevant or misleading tests at scale.

---

## Deliverables (Updated)

1. **Testing mode catalog** — table: name, tier (lite/standard/intense/research), description, **inputs/outputs**, **assumptions**, **known failure modes**, **runtime budget**, **when to use**, **references** (paper/section where relevant).
2. **DB schema** — hybrid normalized + JSON; versioning graph: `catalog_entry → implementation version → run → protocol results → artifacts (paths to HTML/JSON)`.
3. **Visualization architecture** — chart list by phase, library choice, composer pattern (data layer → figure builders → export).
4. **CLI spec** — subcommands, flags, examples.
5. **File/module tree** — new packages under `src/backtest/`, `src/research/`, `src/reporting/` or similar; keep modules small.
6. **Integration points** — exact imports/calls into `BacktestEngine`, `StrategyValidator`, `catalog.py`.

---

## Your First Actions in Repo

1. List `src/backtest/`, `src/research/`, and config files; confirm what exists vs stub.
2. Propose the **minimal v0.1** diff (files + schema migration) before writing large dashboards.
3. If the original mega-spec conflicts with this document, **this document wins** on phasing and statistical guardrails.

---

## Success Criteria for v0.1

- One command produces a **run record** in SQLite + **JSON export** + **single HTML** with equity + drawdown for one strategy + one dataset.
- Sharpe/max DD in DB **match** chart (same metrics function).
- `structlog` logs run id and data fingerprint.

---

*This prompt incorporates an external review: score the vision highly but scope was too large; add WFO/MC/regime definitions; avoid naive MC; parallelize CPU work; ship slices.*
