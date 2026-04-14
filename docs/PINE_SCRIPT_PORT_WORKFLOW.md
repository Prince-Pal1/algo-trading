# Pine Script Port Workflow

This document is the canonical process for porting a TradingView Pine Script strategy into our Python architecture. It has TWO distinct phases that answer TWO separate questions:

1. **Phase 1 — Port correctness** (Stage 0 of the strategy dev process): *Is my Python implementation logically equivalent to the Pine Script?*
2. **Phase 2 — Strategy viability** (Stages 1–7): *Does this strategy actually make money under realistic execution costs?*

Conflating these two questions leads to the SWIFT journey of 2026-04-14 (8 commits, 6 hours of investigation) where we couldn't tell if our port was wrong, our data was wrong, or the strategy was genuinely killed by fees. The framework below prevents that.

---

## Phase 1 — Port correctness (TV Parity Validation)

### Step 1: Prince provides the Pine Script source

"Here's a Pine Script. Port it."

The source usually has the form of `strategy(...)` declaration, one or more indicators, and entry/exit logic via `strategy.entry()` / `strategy.exit()`. Many Pine Scripts are 500-1000+ lines but **>80% of that is visual overlay** (boxes, labels, supply/demand zones, pivot S/R levels, multi-TF Keltner bands, fractal divergences, etc). The actual trading logic is usually ~50 lines.

**Your first job is to identify the real trading logic.** Search the Pine source for:
- `strategy.entry(` — the entry calls
- `strategy.exit(` — the exit calls with SL/TP
- `strategy.close(` — manual closes
- `ta.crossover(` / `ta.crossunder(` / `ta.change(` — typical signal detection
- `request.security(sym, higher_tf, ..., lookahead=barmerge.lookahead_on)` — ALERT: this is a **repainting pattern** that inflates backtests. See the "lookahead trap" section below.

Ignore everything else. `plot(...)`, `plotshape(...)`, `box.new(...)`, `line.new(...)`, `label.new(...)` are visual only.

### Step 2: Write the Python port

Subclass `BaseStrategy` in `src/strategies/<category>/<name>.py`. The port MUST include:

**`on_features(symbol, timeframe, features) -> Signal | None`** — the production signal path. Non-repainting (reads data up to the current bar only). Handles SL/TP exits, position tracking, etc.

**`detect_signals_lookahead(cls, ohlc_df, *, anchor_segments=None, **config) -> pd.DataFrame`** — the Pine parity classmethod. See `SwiftAlmaStrategy.detect_signals_lookahead` (`src/strategies/trend_following/swift_alma.py`) as the reference implementation.

The classmethod takes a whole DataFrame of OHLC bars and returns it with `le_trigger` / `se_trigger` boolean columns added. It replicates Pine's semantics:
- `lookahead=barmerge.lookahead_on` on `request.security()` → use `tv_parity.apply_lookahead_alma_cross` or similar helper
- DST anchor shifts → use `tv_parity.resample_with_dynamic_anchor` with the segments from `tv_parity.detect_alt_anchor_segments`
- Same-bar reversal flip on opposite signals — handled by `tv_parity.simulate_reversal_strategy`

The production path (`on_features`) and the parity path (`detect_signals_lookahead`) share the same indicator math (ALMA / Bollinger / whatever) but use different data-access patterns. The classmethod's job is ONLY to produce the same signals Pine would produce on the same data — not to manage SL/TP, commissions, or sizing.

### Step 3: Prince exports TV data

Prince opens the TradingView chart with the Pine Script applied, sets the backtest window, then:

1. **Chart data export**: chart menu → three dots / camera icon → "Export chart data..." → saves as CSV. **Requires TV Pro or higher subscription.** If unavailable, switch the chart symbol to a free source like `OANDA:XAUUSD` and re-run the backtest there.

2. **Trade export**: strategy tester panel → "List of trades" → download icon → saves as CSV. **Free on all plans**, contains entry/exit timestamps + prices + P&L per trade but NOT the underlying OHLC bars.

For maximum validation fidelity, export BOTH. The chart CSV gives bar-by-bar signal comparison (strongest check); the trades CSV gives trade-level P&L comparison (useful as a sanity check).

**Make sure the chart window has loaded the full backtest period before exporting.** TV exports whatever is currently in chart memory. For a 1-year backtest, scroll the chart back to the start date first so all bars are loaded.

### Step 4: Run `scripts/tv_parity_validate.py`

```bash
PYTHONPATH=. python3 scripts/tv_parity_validate.py \
    --strategy-module src.strategies.<category>.<name> \
    --strategy-class <StrategyClass> \
    --config-json '{"param1": value, "param2": value}' \
    --tv-chart-csv "~/Downloads/<chart_export>.csv" \
    --tv-trades-csv "~/Downloads/<trades_export>.csv" \
    --alt-tf-min 40 \
    --initial-equity 1000000 \
    --position-pct 0.10
```

The script:
1. Loads the TV chart CSV (OHLC + indicator state columns like `Long`/`Short`)
2. Detects alt-TF anchor segments from TV's entry timestamps (handles DST transitions)
3. Calls your strategy's `detect_signals_lookahead` classmethod on the same bars
4. Compares bar-by-bar: for every bar where TV fired an entry, does your classmethod also fire at the same bar in the same direction?
5. Optionally simulates a full reversal strategy and compares trades against TV's trades CSV
6. Prints a pass/fail verdict and writes a Markdown report at `reports/tv_parity/<StrategyClass>_parity.md`

**Pass criterion**: ≥ 99.0% bar-by-bar match (allows ≤ 1% margin for warmup truncation if the CSV starts mid-alt-bar).

### Step 5: If FAIL, diagnose the gap

Common issues and their fixes:

| Symptom | Root cause | Fix |
|---|---|---|
| ~50% match, random misses | Wrong core indicator logic | Re-read Pine source, check formula (e.g., ALMA vs EMA, wrong length, wrong sigma) |
| All entries 10-20 min earlier/later | Wrong alt-TF anchor | Check `detect_alt_anchor_segments` output — does it match your strategy's alt-TF multiplier? |
| All entries in first few bars missing | Warmup truncation | Export a wider TV range (start 2-3 alt bars earlier than the first expected signal) |
| ~95% match with 5-10% consistent miss | Partial DST handling | Verify the anchor segments include every DST transition in your date range |
| 0% long match, 0% short match | Backwards sign convention | Swap le_trigger / se_trigger in your classmethod |
| Half match rate with 40-min time offsets | Missing `lookahead_on` | Use `apply_lookahead_alma_cross` (or equivalent) instead of bar-by-bar streaming |
| 100% match on bars but P&L differs | TV uses different position sizing | Match `--initial-equity` and `--position-pct` to Pine's `default_qty_value` |

### Step 6: Re-run until 100%

Iterate on the port until the validator returns PASS. Only then proceed to Phase 2.

---

## Phase 2 — Strategy viability (Realistic Backtest)

Once Phase 1 confirms your port is equivalent to Pine Script, the strategy is trusted. Now you test it with REAL-world constraints.

### Step 7: Run through LeveragedBacktestEngine with realistic cost model

```python
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.backtest.costs import ICMarketsMetalFeeModel
from src.strategies.<category>.<name> import <StrategyClass>
import pandas as pd

strategy = <StrategyClass>(...)
engine = LeveragedBacktestEngine(
    fee_model=ICMarketsMetalFeeModel(),  # real spread + commission
    # m3s=None, path_model=None → defaults
)
df = pd.read_parquet("data/historical/XAUUSD_5m.parquet")
result = engine.run(strategy, df, symbol="XAUUSD", timeframe="5m", leverage=10.0)
print(f"Return: {result.total_return_pct:+.2f}%, Max DD: {result.max_dd_pct:.2f}%")
```

This uses our canonical data source (Dukascopy XAUUSD) and IC Markets Raw commission model. It's a DIFFERENT data source than TV's Vantage Gold Spot, so absolute prices may differ by 0.2-0.3%, but the alpha evaluation is still meaningful.

For more rigor, run a matrix across timeframes × leverage levels (see `scripts/backtest_swift_alma_matrix.py` as a template).

### Step 8: Compare Phase 1 (Pine parity) vs Phase 2 (realistic) results

If the numbers differ drastically:

| Phase 1 (Pine) | Phase 2 (Realistic) | Interpretation |
|---|---|---|
| +X% | +Y% with Y ≈ X | Strategy has genuine alpha; minor cost haircut |
| +X% | +Y% with Y < X/2 | Strategy has weak alpha that barely survives costs; borderline |
| +X% | -Y% | Strategy has gross edge but loses to commissions. Needs lower frequency OR zero-commission broker |
| +X% | -100% | Strategy is `lookahead_on` repainting — impossible to trade live |

### Step 9: Graveyard if not viable

If Phase 2 shows the strategy is unprofitable under realistic costs, add it to the graveyard:

```python
from src.strategies.graveyard import GraveyardEntry, record_kill

record_kill(GraveyardEntry(
    strategy_name="<name>",
    kill_category="PREMISE",  # or TIMING / NOISE / INFRA / REGIME
    kill_reason_short="...",
    root_cause_long="...",
    revival_conditions="...",
    ...
))
```

See the SWIFT graveyard entry (`data/trades.db :: strategy_graveyard`) as an example.

### Step 10: If viable, proceed to Stages 1-7 of STRATEGY_DEVELOPMENT_PROCESS.md

- Stage 1: Walk-forward validation
- Stage 2: Parameter robustness
- Stage 3: Monte Carlo / bootstrap
- Stage 4: Out-of-sample test
- Stage 5: Deflated Sharpe
- Stage 6: Paper trading
- Stage 7: Live

---

## The lookahead trap

Pine Script's `request.security(sym, higher_tf, expression, lookahead=barmerge.lookahead_on)` is the single most common source of INFLATED TradingView backtests. It means: at every chart bar inside a still-forming higher-TF bar, the function returns the FINAL value of that HTF bar — which is future data in a historical backtest.

**In a live chart, this causes the indicator to REPAINT** as the HTF bar forms. What looked like a signal at 00:00 disappears or moves to 00:40 by the time the HTF bar actually closes. You can't trade repainting signals in real time.

**In a TV backtest, the final HTF bar close IS known** (it's just a future chart bar), so the indicator fires at the chart bar instead of waiting for the HTF bar to close. This lets the strategy "front-run" its own signals by up to one HTF bar's worth of time.

The SWIFT investigation (commits `d30d0bd`, `491d0a0`, `df8c26f`, `e529e56`) showed this definitively: SWIFT's TV stats (88% win, PF 24, +$2.2M) come ENTIRELY from the `lookahead_on` repainting. Non-repainting execution produces ~37% win rate and net-negative returns after IC Markets commissions.

**When you see `lookahead_on` in a Pine Script, assume the backtest is inflated.** Our framework lets you REPLICATE the repainting behavior for parity validation (Phase 1) while running the REAL non-repainting behavior for viability testing (Phase 2).

---

## Reference implementation

See `src/strategies/trend_following/swift_alma.py` for a complete example of:
- `on_features()` — production path with non-repainting alt-TF aggregation
- `detect_signals_lookahead()` classmethod — Pine-faithful repainting path for parity validation
- 3-tier TP ladder via virtual leg accounting
- Same-bar reversal flip handling

And `reports/tv_parity/SwiftAlmaStrategy_parity.md` for what a 100% pass report looks like.
