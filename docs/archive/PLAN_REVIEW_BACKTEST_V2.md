> ⚠️ **ARCHIVED — COMPLETED (Session 8, moved Session 21)**
> One-time plan review, concerns addressed. See [ROADMAP.md](../../ROADMAP.md) for current status.

---

# Plan review — Institutional backtest + multi-format ingestion + showcase (v2)

> **Status (April 12, 2026):** IMPLEMENTED. All 5 phases (A-E) of the reviewed plan are complete. The concerns raised in this review (MC underspecification, coverage claims, scope risk) were addressed during implementation. See `STATE.md` for details.

**Document reviewed:** pasted plan “Institutional-Grade Backtesting, Multi-Format Ingestion & Strategy Showcase”  
**Purpose:** Honest rating, mistakes/gaps, and additive requirements (not a rewrite of the full plan).

---

## Overall rating

| Dimension | Score | Note |
|-----------|-------|------|
| Direction (triple output, single metrics, traceability) | **9/10** | Strong engineering invariants; reduces drift. |
| Phasing (A→F) | **8/10** | Phase A first is correct; D–E still huge. |
| Statistical rigor | **6/10** | PSR/DSR named but MC/shuffle and DSR inputs underspecified. |
| “Any format” ambition | **5/10** | IR + codegen is right pattern; coverage claims are overstated. |
| Feasibility (solo + time) | **5/10** | Tier 1 alone is a large product; full table is multi-team scale. |
| Ops/security | **5/10** | LLM parsers, dynamic import, secrets not addressed. |

**Net: ~7/10** as a north-star plan with **fixable technical mistakes** and **scope risk** if “Tier 1” is treated as trivial.

---

## Mistakes / issues to fix (concrete)

### 1. Data fingerprint (Invariant 3) — fragile as written

`hashlib.sha256(ohlcv_df.to_csv().encode()).hexdigest()[:12]` is **not stable** across:

- pandas version, `to_csv` float formatting, column order, index name, timezone string form  
- accidental row reordering with same data  

**Better:** canonical fingerprint, e.g. hash of:

- `symbol`, `timeframe`, `start`, `end`, **row count**  
- hash of **normalized** content: `df[["open","high","low","close","volume"]].sort_index().to_parquet()` bytes **or** `pandas.util.hash_pandas_object` on sorted columns (document choice)

Truncating to **12 hex chars** (~48 bits) increases **collision risk** for many runs; prefer **full SHA-256** in DB, optional short prefix in logs.

### 2. “Atomic” triple output — not guaranteed

`save_run()` can **INSERT** then fail on JSON/HTML write → DB row without artifacts (or vice versa if order changed).

**Better:** insert run as **PENDING**, write files, then **COMMIT** `COMPLETE` + paths; or write JSON/HTML to temp paths then **rename** + single DB transaction; document recovery for partial failure.

### 3. Monte Carlo “10K trade shuffles” / “randomized entry/exit ±3 bars”

Still **statistically questionable** for path-dependent strategies (position sizing, stops, overlapping trades).  

**Better:** document **block bootstrap on returns** (or returns of round-turn trades with block length L) as primary; if shuffling trades, label output **“structural stress only, not confidence interval for live Sharpe”**.

### 4. Deflated Sharpe “instant”

DSR needs **number of trials / tests** and a defensible **variance of Sharpe estimate** (or returns series + assumptions). Not always “instant” if `n_trials` is unknown (common in research).

**Better:** require `n_trials` and `var_sharpe` (or estimator) in config; if missing, **omit DSR** or report **range** with sensitivity note.

### 5. PSR on “any prior result” (lite `psr_check`)

PSR is computed from **returns** (or needs return series). “Prior result” must store **per-bar or daily returns**, not only scalar Sharpe.

**Better:** persist **return series** or enough equity curve to recompute returns identically to `compute_metrics`.

### 6. `compute_metrics(equity, trades)` — alignment details

Sharpe/Sortino on **equity pct_change** vs **bar returns** must match engine assumptions (RF rate, annualization, bars per year).  

**Better:** pass **`returns: pd.Series`** from engine (or bar timestamps + resampling rule) as single input; document **annualization factor** in metrics dict metadata.

### 7. Dynamic import / codegen (`generate_and_load`)

**Security:** executing generated code is **RCE** if IR comes from untrusted Pine/NL.  

**Better:** sandbox policy: only load from **trusted** `strategies/imported/<id>/strategy.py` after human review; or restricted template codegen **only** (no arbitrary Python). Log `hash(strategy.py)` in run metadata.

### 8. LLM parsers (natural language, Pine, MQL)

Non-deterministic; same input can yield different IR.  

**Better:** store **model id**, **prompt version**, **temperature**, **output IR hash**; optional **human_approved** flag before catalog promotion to production tier.

### 9. Strategy YAML IR — “captures any strategy”

Many strategies need **state machines**, **multi-leg**, **options**, **session filters**, **order types**, **pyramiding** — IR will **either** explode **or** fail on real-world scripts.

**Better:** document **IR v1 scope** (e.g. single-symbol, long/flat, indicator + crossover family + basic stops). Version IR schema (`ir_version: 1`).

### 10. `python_framework_parser.py` — “AST → IR” for many frameworks

One AST parser cannot cover Freqtrade + Zipline + QC + VectorBT without **per-framework** extractors.  

**Better:** rename to package `parsers/python/` with **freqtrade.py**, **backtrader.py**, etc., sharing utilities — or start with **one** framework.

### 11. Crash events TOML — `btc_drop_pct = -58` for “COVID Black Thursday”

**Fact check:** -58% is not a standard label for that window for BTC (and dates span 2 days). Use **verifiable** series or drop narrative %; store **symbol-specific** windows (BTC vs XAUUSD vs SPX).

**Better:** `crash_events.toml` keyed by **symbol** + optional **benchmark_id**; validate dates against your **actual** downloaded data coverage.

### 12. Async `run_monte_carlo` / heavy protocols

CPU-bound work under `async def` without **executors** blocks the event loop.

**Better:** `asyncio.to_thread()` or `ProcessPoolExecutor` for sweeps/MC; keep async for I/O-bound steps only.

### 13. SQLite concurrency

Streamlit/dashboard + CLI writes: enable **WAL**, `timeout=`, and **single writer** discipline; consider **read replica** copy for dashboard if needed.

### 14. QuantStats bridge

Adds dependency weight; check **license** and **overlap** with custom `compute_metrics` (avoid two Sharpe definitions in one report). Prefer **one** primary metrics source; QuantStats as **optional** appendix with disclaimer.

### 15. CLI path inconsistency

Plan uses `python -m scripts.backtest` — ensure `scripts/` is a **package** (`__init__.py`) or use `python scripts/backtest.py` / console entry point in `pyproject.toml`. Pick **one** pattern and document it.

### 16. Conflict with project MASTER_PLAN

Master plan deprecates **FreqTrade** as primary stack but plan includes Freqtrade import — fine as **ingestion only**, but **document** “import only, not runtime dependency of core engine.”

---

## Additions recommended (add to plan text)

1. **IR versioning + supported feature matrix** (what IR v1 can/cannot express).  
2. **Trust model** for generated code (review gate, no `eval` of arbitrary LLM Python).  
3. **Canonical data fingerprint** spec + full hash in DB.  
4. **Transaction boundaries** for triple output + recovery semantics.  
5. **MC methodology** paragraph (block bootstrap vs shuffle; path-dependence disclaimer).  
6. **DSR/PSR inputs** explicitly listed in config/schema.  
7. **Per-asset crash catalog** (crypto vs gold vs equity).  
8. **Performance:** protocol budget caps (max grid size, max CPCV paths default).  
9. **Testing:** invariant tests (DB = JSON = HTML) as **automated pytest**, not manual.  
10. **Optional:** `pyproject.toml` `[project.scripts]` entry `backtest=...` for stable CLI.

---

## Revised rating after fixes

If the items above are incorporated into the written plan: **~8/10** executable; without them, **~6.5/10** (drift, wrong stats, security holes).

---

*Reviewer notes aligned with prior architecture review: phased delivery, single metrics source, honest statistics, CPU vs async separation.*
