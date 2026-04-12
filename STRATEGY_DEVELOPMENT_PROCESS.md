# Strategy Development Process

**Version:** 1.0 | **Last Updated:** April 12, 2026 | **Status:** Living document — update after every strategy cycle

This document codifies the 7-stage hedge fund-style strategy development process used in this project. It was derived from executing the full cycle on 5 strategies (3 alive, 2 killed) across Sessions 10-11, producing a portfolio Sharpe improvement from 1.748 to 2.318 (+33%).

**Rule: Never skip stages. Never deploy without passing all gates. Update this document when you learn something new.**

---

## Quick Reference Checklist

Use this before declaring any strategy "done":

- [ ] Stage 1: 4 research questions answered (inefficiency, persistence, killers, decay)
- [ ] Stage 2: Strategy implemented with `BaseStrategy.on_features() -> Signal | None`
- [ ] Stage 3: Backtest on 12 symbols, ≥1 symbol with Sharpe > 0 and ≥5 trades
- [ ] Stage 4: Lite validation 2/2 pass on top symbols, Standard 3/4+ pass
- [ ] Stage 5: Grid search on sl_atr_mult first, then top 2 params. PSR > 0.5. Fee sensitivity survives 0.08%
- [ ] Stage 6: Portfolio Sharpe improves. Cross-strategy correlation < 0.3
- [ ] Stage 7: Walk-forward OOS positive. Results in DB. STATE.md updated. Obituary if killed.

---

## Overview & Philosophy

### Why a process?

Without a disciplined process, strategy development devolves into:
- Chasing paper results that don't survive real fees
- Overfitting to noise via endless parameter tweaking
- Abandoning strategies without understanding why they failed
- Never killing dead strategies (sunk cost)
- Building portfolios from correlated strategies that blow up together

### Core principles

1. **Research before code** — Understand the inefficiency before writing a single line
2. **Evidence-based kills** — Every dead strategy gets an obituary with root cause
3. **Statistical rigor** — PSR, DSR, Monte Carlo, not just Sharpe
4. **Anti-overfitting by default** — Fee sensitivity, walk-forward OOS, deflated Sharpe
5. **Portfolio thinking** — Individual Sharpe matters less than cross-strategy correlation
6. **Iterate then stop** — 3 consecutive rounds of <5% improvement = convergence

---

## The 7 Stages

### Stage 1: Alpha Research

**Purpose:** Answer "Is there a real edge here?" BEFORE writing any code.

**The 4 mandatory questions:**

| # | Question | What it prevents |
|---|----------|-----------------|
| 1 | What is the market inefficiency? | Building strategies with no theoretical edge |
| 2 | Why does this inefficiency persist? | Chasing edges that were arbitraged away |
| 3 | What market conditions kill it? | Deploying in the wrong regime |
| 4 | What is the expected post-publication decay? | Trusting paper Sharpe ratios at face value |

**Actions:**
- Web search for recent papers (2024-2026) on the strategy type
- Check for structural market changes (e.g., BTC ETF Jan 2024 broke cointegration)
- Estimate publication decay: assume 50% minimum, 80%+ for well-known strategies
- Document all findings — even negative results are valuable

**Pass criteria (Research Gate):**
- Clear, articulable inefficiency
- Expected decay < 80%
- No known structural break that kills the edge
- At least one plausible market condition where it should work

**Fail action:** Kill with documented obituary. Do NOT proceed to implementation.

**Pitfalls from experience:**
- Cherry-picked results in papers are the norm (WF-EMA Sharpe 1.252 was best of 81 combos)
- "Reported Sharpe 2.3" often becomes -1.2 on your data (BTC-Neutral MR)
- If EMA crossover is the most published strategy in existence, it has zero alpha left
- Check if the market regime has changed — crypto went from trending (2020-2021) to mean-reverting (2024-2026) on intraday

---

### Stage 2: Implementation

**Purpose:** Build the strategy correctly, following the project's patterns.

**Implementation pattern:**
```python
# File: src/strategies/<category>/<strategy_name>.py
class MyStrategy(BaseStrategy):
    def __init__(self, name, markets, timeframe, risk_profile, max_risk_per_trade, **params):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)
        # Store params as instance attributes
        
    def on_features(self, symbol, timeframe, features: pd.Series) -> Signal | None:
        # EXIT logic first (always check exits before entries)
        # ENTRY filters (cooldown, regime, thresholds)
        # ENTRY signals (LONG / SHORT)
        # Return Signal or None
```

**Checklist:**
- [ ] Inherits from `BaseStrategy`
- [ ] `on_features()` returns `Signal | None`
- [ ] Exit logic before entry logic
- [ ] Stop loss and take profit in every Signal
- [ ] Risk-based position sizing via `risk_pct`
- [ ] Cooldown between trades (`cooldown_bars`)
- [ ] Max hold timeout (`max_hold_bars`)
- [ ] Added to `STRATEGY_REGISTRY` in `scripts/backtest.py`
- [ ] Indicators listed in registry entry

**Register in STRATEGY_REGISTRY:**
```python
"my_strategy": {
    "factory": lambda tf: MyStrategy(
        name="my_strategy", markets=["BTCUSDT"], timeframe=tf,
        # all params with defaults
    ),
    "indicators": ["atr_14", "rsi_14", ...],  # what feature engine needs
},
```

**Pitfalls from experience:**
- `ta.DonchianChannel` includes current bar's high/low — must use PREVIOUS bar's channel for breakout detection
- Strategy entry conditions often act as regime filters already — don't stack redundant overlay filters
- Always include `long_only` and filter params as constructor args for future optimization

---

### Stage 3: Initial Backtest

**Purpose:** Does the strategy produce any positive results at all?

**Commands:**
```bash
# Run on all 12 symbols (BTC, ETH, BNB, ADA, DOT, XRP, SOL, AVAX, NEAR, DOGE, APT, LINK)
python3 -m scripts.backtest run my_strategy --symbol BTCUSDT --tf 1h
python3 -m scripts.backtest run my_strategy --symbol ETHUSDT --tf 1h
# ... repeat for all 12
```

**Pass criteria (Smoke Gate):**
- Sharpe > 0 on at least 1 symbol
- At least 5 trades on at least 1 symbol (not degenerate)
- No obvious bugs (check trade log for impossible entries/exits)

**Fail action:** 
- If 0/12 symbols positive → kill (likely no edge)
- If bugs found → fix and re-run
- If < 5 trades everywhere → loosen filters slightly, re-test

**What to record:**
- Sharpe per symbol (sorted descending)
- Number of trades per symbol
- Top 5-6 winner symbols (these become your validation universe)

**Pitfalls from experience:**
- BTC is too efficient for most strategies — exclude from MR universe
- Altcoins (DOT, BNB, ADA) mean-revert well; majors (BTC, ETH) less so
- 730 days (2 years) is the standard test window — shorter windows are unreliable

---

### Stage 4: Tiered Validation

**Purpose:** Stress-test the strategy with statistical rigor.

**Tier 1: Lite (< 1 min per symbol)**
```bash
python3 -m scripts.backtest validate my_strategy --symbol DOTUSDT --tf 1h --tier lite
```
- `smoke`: 30-day sanity check
- `spot_check`: 90-day window, ≥ 5 trades, basic metrics

**Tier 2: Standard (1-10 min per symbol)**
```bash
python3 -m scripts.backtest validate my_strategy --symbol DOTUSDT --tf 1h --tier standard
```
- Everything in Lite, plus:
- `monte_carlo`: 10K trade shuffles, P(profit) ≥ 60%, median P&L > 0
- `crash_stress`: Survive ≥ 3/5 known crypto crash events (COVID, LUNA, FTX, China ban, 3AC)

**Pass criteria (Validation Gate):**
- Lite: 2/2 protocols pass on top winner symbols
- Standard: 3/4+ protocols pass on top 3 symbols
- Monte Carlo P(profit) ≥ 60%
- Crash stress survival ≥ 3/5 events

**Fail action:**
- If Lite fails → strategy has fundamental problems, go back to Stage 2
- If Standard fails on Monte Carlo → profits may be from a few lucky trades
- If crash stress fails → strategy is fragile, add protective exits

**Pitfalls from experience:**
- bb_rsi_mr spot_check always fails because it generates 0-3 trades per 90-day window — this is by design (strict triple filter), not a bug
- Some strategies are inherently low-frequency — adjust spot_check minimum trade count accordingly

---

### Stage 5: Optimization & Enhancement

**Purpose:** Find optimal parameters and test creative improvements.

**The optimization loop:**
```
5.1  Parameter sensitivity — 2D grid on top 2 params (5x5, ±20%)
5.2  Creative enhancements (test ONE at a time, validate after each)
5.3  Iteration: optimize → re-test → validate → compare baseline
5.4  Stop: 3 consecutive no-improvement rounds (>5% Sharpe threshold)
```

**Step 5.1: Grid Search**

Always start with `sl_atr_mult` — it's the highest-ROI param for EVERY strategy tested so far.

```bash
python3 -m scripts.backtest validate my_strategy --symbol DOTUSDT --tf 1h \
  --mode param_sensitivity_2d --p1 sl_atr_mult --p1base 3.0 --p2 rsi_oversold --p2base 25.0
```

Or use the automated optimizer:
```bash
python3 -m scripts.backtest optimize my_strategy --symbol DOTUSDT --tf 1h --rounds 3
```

**Interpreting grid search results:**
- **Robust**: std < 0.5 AND ≥60% of cells positive → safe to adopt best params
- **Fragile**: std > 0.5 OR < 60% cells positive → keep base params, improvement is likely overfit
- **Zero-effect param**: identical Sharpe across entire row/column → param doesn't matter, skip it

**Step 5.2: Creative Enhancements**

Test ONE enhancement at a time. Run lite validation after each to confirm no regression.

Common enhancements to try (in priority order):
1. **Regime filter** (ADX > 25 for trend, ADX < 20 for MR) — highest ROI historically
2. **Stop loss adjustment** (sl_atr_mult sweep)
3. **Long-only mode** — test but expect it to fail in crypto (shorts are profitable)
4. **Overlay filters** (VPIN, BB squeeze) — usually redundant, test anyway

**Pass criteria (Optimization Gate):**
- PSR > 0.5 on optimized config (strategy is genuinely profitable)
- Fee sensitivity: edge survives at 0.08% commission (0.10% is the real-world gate)
- Grid search robustness: std < 0.5 if adopting grid-optimized params

**Anti-overfitting checks (mandatory):**
```bash
# PSR — is the strategy genuinely profitable?
# (computed automatically in validation runs)

# DSR — is the optimization improvement real?
# DSR = 0.000 is EXPECTED for grid search (selection bias from N=25 trials)
# This means: trust the BASE strategy, but treat grid improvement with skepticism

# Fee sensitivity — does edge survive realistic fees?
# Run at 0.04%, 0.06%, 0.08%, 0.10% — must survive 0.08%
```

**Fail action:**
- If PSR < 0.5 → strategy is not genuinely profitable, go back to Stage 2
- If fee sensitivity fails at 0.08% → edge is too thin for real trading
- If all grid improvements fail robustness → use base params (they're already validated)

**Pitfalls from experience:**
- `dc_short`, `bb_period`, `vol_target`, `rebalance_interval` have ZERO effect — don't waste time
- ADX filter effectiveness is symbol-specific (helps NEAR/DOGE/ETH/ADA, hurts AVAX/DOT)
- Long-only kills both Donchian (-75%) and Vol Momentum (-72%) — crypto shorts are essential
- VPIN overlay is universally useless on strategies that already have entry condition filters
- Grid search improvement passes PSR but DSR = 0 — the improvement itself may be overfit even if the base strategy is genuine. This is EXPECTED and not a problem if you trust the base params.

---

### Stage 6: Portfolio Construction

**Purpose:** Combine strategies for diversification, not just stacking individual winners.

**Steps:**
1. Collect best configs for each validated strategy
2. Run all winners with optimized params on 12 symbols
3. Compute correlation matrix (strategy vs strategy equity curves)
4. Set allocation weights based on Sharpe and inverse-correlation
5. Compute portfolio Sharpe, max DD, Calmar, Sortino

**Commands:**
```bash
# Run optimized strategies
python3 -m scripts.backtest run my_strategy_opt --symbol DOTUSDT --tf 1h
# ... for all winner symbols per strategy

# Compare portfolio
python3 -m scripts.backtest compare <run_id_1> <run_id_2> <run_id_3>
```

**Current portfolio structure (as of Session 11):**

| Strategy | Type | Allocation | Captures |
|----------|------|-----------|----------|
| bb_rsi_mr_opt | Mean reversion | 40% | Ranging markets (ADX < 20) |
| donchian_ensemble_adx | Trend-following | 30% | Breakout/trending markets (ADX > 25) |
| vol_momentum | Momentum | 30% | Directional momentum |

**Pass criteria (Portfolio Gate):**
- Portfolio Sharpe improves over previous best
- Cross-strategy correlation < 0.3 (ideally < 0.1)
- Max drawdown doesn't increase disproportionately
- Each strategy contributes unique regime coverage

**Fail action:**
- If correlation > 0.3 → strategies are redundant, keep only the better one
- If portfolio Sharpe doesn't improve → new strategy doesn't add value, reject from portfolio

**Pitfalls from experience:**
- Near-zero correlation (-0.05 to +0.03) is achievable between MR, trend, and momentum
- Donchian and Vol Momentum correlate moderately (0.28) because both are directional — acceptable but watch it
- 3 uncorrelated edge types is the minimum viable portfolio

---

### Stage 7: Documentation & Anti-Overfitting

**Purpose:** Ensure results are reproducible, not overfit, and documented for future reference.

**Anti-overfitting framework:**

| Check | Tool | What It Catches |
|-------|------|----------------|
| PSR | `probabilistic_sharpe()` in metrics.py | Is the strategy genuinely profitable? (p > 0.5) |
| DSR | `deflated_sharpe()` in metrics.py | Is the optimization improvement real? (corrects for N trials) |
| Walk-Forward OOS | `walk_forward.py` | Would optimized params work on unseen data? |
| Fee Sensitivity | Commission sweep 0.04-0.10% | Does edge survive realistic transaction costs? |
| Monte Carlo | `run_monte_carlo()` protocol | Are profits from skill or lucky trade ordering? |
| Multi-Interval | `StrategyValidator` | Does strategy work across different time windows? |

**Documentation checklist:**
- [ ] `STATE.md` updated with session results
- [ ] `ARCHITECTURE.md` updated with new modules and Known Gotchas
- [ ] Strategy obituary written (if killed)
- [ ] Registry entries updated in `scripts/backtest.py`
- [ ] All backtest runs persisted to DB
- [ ] Memory files updated with key findings

---

## Decision Gates Summary

| Gate | After Stage | Pass Criteria | Fail Action |
|------|------------|---------------|-------------|
| **Research Gate** | 1 | Clear inefficiency, decay < 80%, no structural break | Kill with obituary |
| **Smoke Gate** | 3 | Sharpe > 0 on ≥1 symbol, ≥5 trades | Kill or redesign |
| **Validation Gate** | 4 | Lite 2/2, Standard 3/4+, MC P(profit) ≥ 60% | Redesign or kill |
| **Optimization Gate** | 5 | PSR > 0.5, fees survive 0.08%, grid std < 0.5 | Use base params |
| **Portfolio Gate** | 6 | Portfolio Sharpe improves, correlation < 0.3 | Reject from portfolio |
| **Anti-Overfit Gate** | 7 | WF-OOS positive, DSR context understood | Flag as fragile |

**Kill protocol:** Any strategy that fails a gate gets maximum 3 redesign rounds. If it fails all 3, it's permanently killed with a documented obituary.

---

## Strategy Obituary Template

When killing a strategy, document it here so we never waste time revisiting it without new evidence.

```markdown
### [Strategy Name] — KILLED [Date]

**Reported performance:** Sharpe X.XX (source: [paper/repo])
**Actual performance:** Sharpe X.XX on our data (dates: YYYY-MM to YYYY-MM)

**Root cause of death:**
- [Primary reason — be specific]
- [Contributing factors]

**What we tried (max 3 rounds):**
1. Round 1: [what we changed] → [result]
2. Round 2: [what we changed] → [result]  
3. Round 3: [what we changed] → [result]

**Structural reason it can't be fixed:**
[Why this isn't a param problem but a fundamental edge problem]

**Revival conditions:**
[What would need to change in the market for this to become viable again]
```

**Current obituaries:**

### BTC-Neutral Residual MR — KILLED April 12, 2026

**Reported:** Sharpe 2.3 | **Actual:** -1.2 to -3.5

**Root cause:** BTC ETF approval (Jan 2024) broke BTC-altcoin cointegration. Rolling OLS produces artifactual z-score reversion (the regression adapts to absorb deviation). Z-score reverts mathematically but price doesn't move profitably — mismatch between z-space exits and price-space stops.

**Revival conditions:** BTC ETF outflows causing BTC to re-couple with altcoins, or Kalman filter + OU half-life filter + ADF gate + 10-20 pair portfolio (2-4 week research project).

### WF-EMA Crossover — KILLED April 12, 2026

**Reported:** Sharpe 1.252 | **Actual:** -0.44 to +0.13

**Root cause:** Original paper's result was cherry-picked from 81 combos during 2020-2021 bull run. EMA crossover is the most published strategy in existence — zero alpha remaining. 2024-2026 crypto is "whipsaw intraday, trending on higher TFs" (Pantera Capital).

**Revival conditions:** None for intraday. 6H+ timeframe might work but would be essentially a new strategy.

---

## Lessons Learned

This section grows over time. Each lesson is tagged with when it was discovered.

### Research (Stage 1)

| # | Lesson | Session |
|---|--------|---------|
| R1 | Answer 4 questions BEFORE coding: What inefficiency? Why persist? What kills it? Expected decay? | 11 |
| R2 | Web search for recent papers — market structure changes can permanently kill strategies | 11 |
| R3 | BTC ETF (Jan 2024) was a structural break that killed BTC-altcoin cointegration | 11 |
| R4 | Cherry-picked results in papers are the norm — verify on YOUR data window, not theirs | 11 |
| R5 | Publication decay is ~50% minimum for any public strategy, 80%+ for well-known ones | 10 |

### Implementation (Stage 2)

| # | Lesson | Session |
|---|--------|---------|
| I1 | Always compare PREVIOUS bar features for breakout detection (Donchian `ta` lib includes current bar) | 10 |
| I2 | Strategy entry conditions ARE regime filters — don't stack redundant overlay filters (VPIN lesson) | 11 |
| I3 | Include `long_only`, filter params, and sl_atr_mult as constructor args for future optimization | 10 |

### Optimization (Stage 5)

| # | Lesson | Session |
|---|--------|---------|
| O1 | `sl_atr_mult` is the highest-ROI param to tune for EVERY strategy — always start here | 11 |
| O2 | Many params have ZERO effect: dc_short, bb_period, vol_target, rebalance_interval | 11 |
| O3 | ADX filter effectiveness is symbol-specific — test on 6+ symbols, not just 2-3 | 11 |
| O4 | Long-only kills trend/momentum in crypto — shorts are where the edge lives (-72% to -75%) | 11 |
| O5 | VPIN overlay is universally useless on strategies with existing entry condition filters | 11 |
| O6 | Grid search improvement passes PSR but fails DSR — improvement itself may be overfit | 11 |
| O7 | Fee sensitivity at 0.10% is the real-world gate — edge must survive high fees | 11 |
| O8 | After 3+ failed tuning iterations, research a fundamentally different approach | 9 |

### Portfolio (Stage 6)

| # | Lesson | Session |
|---|--------|---------|
| P1 | Cross-strategy correlation matters more than individual Sharpe | 10 |
| P2 | 3 uncorrelated edge types (MR + trend + momentum) is the minimum viable portfolio | 10 |
| P3 | Near-zero correlation (-0.05 to +0.03) is achievable between different strategy types | 11 |
| P4 | Individual symbol Sharpe doesn't matter much — portfolio correlation matters more | 10 |

### Market Structure

| # | Lesson | Session |
|---|--------|---------|
| M1 | Crypto intraday is ~60-70% mean-reverting, not trending (2024-2026) | 8 |
| M2 | BTC is too efficient for simple scalping — 3 independent papers confirm | 7 |
| M3 | Altcoins (DOT, BNB, ADA) mean-revert well; BTC does not | 9 |
| M4 | 6H+ is the current viable timeframe for trend-following in crypto | 11 |
| M5 | India crypto: 30% tax + 1% TDS kills high-frequency strategies — focus on forex/gold for HFT | 8 |

---

## Process Improvement Log

Track what we'd do differently next time.

| Issue | What Happened | Improvement |
|-------|--------------|-------------|
| VPIN tested 3x | Tested on bb_rsi_mr, donchian, vol_momentum before concluding useless | Stop after 2 negative results on different strategy types |
| Long-only tested twice | Tested on Donchian and Vol Momentum despite crypto shorts being known profitable | Question hypothesis before testing — check if contradicts known findings |
| No WF-OOS in Stage 5 | Only used PSR/DSR, not walk-forward OOS | Make WF-OOS mandatory in Stage 5, not just Stage 7 |
| DSR always 0 for grids | 5x5 grid = 25 trials, DSR correctly flags selection bias | Don't panic when DSR=0 — it means trust base params, not grid improvement |

---

## Infrastructure Reference

| Tool | Command | Purpose |
|------|---------|---------|
| Backtest | `python3 -m scripts.backtest run <strategy> --symbol X --tf 1h` | Run single backtest |
| Validate | `python3 -m scripts.backtest validate <strategy> --tier lite\|standard` | Tiered validation |
| Grid Search | `python3 -m scripts.backtest validate <strategy> --mode param_sensitivity_2d --p1 X --p1base N --p2 Y --p2base M` | 2D parameter sweep |
| Optimize | `python3 -m scripts.backtest optimize <strategy> --symbol X --rounds 3` | Automated multi-round optimization |
| Compare | `python3 -m scripts.backtest compare <id1> <id2> <id3>` | Portfolio comparison |
| Dashboard | `python3 -m scripts.dashboard` | Streamlit interactive UI |
| Import | `python3 -m scripts.import_strategy import --format pine --file X` | Multi-format strategy import |

**Key files:**
- Strategy implementations: `src/strategies/<category>/<name>.py`
- Registry + CLI: `scripts/backtest.py`
- Protocols: `src/backtest/protocols.py`
- Metrics (PSR/DSR): `src/backtest/metrics.py`
- Feature engine: `src/data/feature_engine.py`
- Crash events: `config/crash_events.toml`

---

## Revision History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-04-12 | Initial version — codified from Session 10-11 execution of full 7-stage process |
