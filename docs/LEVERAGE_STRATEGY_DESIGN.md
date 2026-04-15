# Leverage Strategy Design Principles

**Audience**: Anyone building, tuning, or deploying a trading strategy in this codebase
**Scope**: How leverage interacts with position sizing, and what design choices give you return amplification vs margin efficiency
**Reference research**: [`reports/leverage_strategy_research_2026-04-15.md`](../reports/leverage_strategy_research_2026-04-15.md) (task #113)

---

## 1. The core identity you must internalize

Every strategy in this codebase computes quantity via this formula in `src/backtest/leveraged_engine.py:536-550`:

```
quantity = (equity × risk_pct) / stop_distance
```

Which, given `stop_distance = entry_price × sl_pct`, simplifies to:

```
notional = entry_price × quantity = equity × (risk_pct / sl_pct)
```

**Leverage enters in exactly one place** — `src/backtest/book.py:377-378`:

```
entry_margin = notional / leverage
```

**Consequence**: The `leverage` parameter ONLY affects how much margin is LOCKED. It does NOT affect position size, notional exposure, fill price, commission, or P&L. If two runs produce the same notional, they produce identical P&L regardless of leverage (beyond the margin-fit gate).

This is the identity that determines whether your strategy is leverage-passive or leverage-active.

---

## 2. The six position-sizing modes

Every strategy falls into one of these six modes. Know which one yours is in.

### 2.1 INVARIANT — `risk_pct == sl_pct`

```
notional = equity × (sl_pct / sl_pct) = equity × 1.0
```

**Example**: `swift_alma` (Pine port with `max_risk_per_trade = sl_pct = 0.005`).

**Behavior**: Position size is constant = equity / price. Leverage does nothing. P&L identical at any leverage that fits.

**When to use**: Pine ports, baseline unlevered runs, strict TV-parity research.

**When NOT to use**: Production deployment where you want leverage to matter.

### 2.2 MARGIN_CAPPED — `risk_pct > sl_pct` (via ATR or similar)

```
notional = equity × k     where k > 1 (e.g., k=3 for donchian_gold)
```

**Example**: `donchian_gold` with `max_risk_per_trade=0.02` and `stop_distance = 3 × ATR(14)`. At XAUUSD typical ATR ~10, `k ≈ 3`.

**Behavior**: Notional is `k × equity`. Below leverage `k`, margin required > free margin → position rejected. At leverage ≥ `k`, position fits and P&L is identical.

**The 1×-starvation gotcha**: If your strategy has `k > 1`, running at leverage 1× will reject most signals. This is why donchian_gold fires 2-3 trades at 1× but 10-36 at 10×.

**When to use**: Strategies with edge in the signal, where you want the notional / equity ratio to match your conviction level.

**When NOT to use**: When you want leverage to multiply returns.

### 2.3 VOL_TARGETED — `risk_pct = base × (vol_target / realized_vol)`

**Example**: `vol_momentum_gold` with `vol_target=0.20`, `vol_lookback=240`. Vol scalar clamped to `[0.5, 2.0]`.

**Behavior**: Position size adapts to market regime. In low-vol regimes, positions grow (up to 2× base). In high-vol regimes, positions shrink (down to 0.5× base). Leverage parameter still only affects margin footprint.

**When to use**: Strategies whose edge depends on volatility regime. Momentum in low-vol, mean-reversion in high-vol. Vol-targeting is the industry standard for futures and forex ([Tradeify 2026](https://tradeify.co/post/futures-leverage)).

### 2.4 RISK_SCALED — `risk_pct = base × (leverage / baseline)` *(proposed)*

**Not yet implemented.** This is what the Phase D counter-demo in task #110 did manually (`risk_pct = 0.005 × scale`). The research proposes adding this as a first-class `leverage_mode`.

**Behavior**: Linear leverage amplification. Doubling leverage doubles notional, doubles P&L, doubles drawdown.

**Sweet spot**: Typically around baseline + 50% (half-Kelly region). Past that, drawdown compounding dominates.

**When to use**: Strategies with validated positive edge (OOS Calmar ≥ 1) that you want to amplify.

**When NOT to use**: Low-edge strategies — you'll amplify noise.

### 2.5 KELLY_FRACTIONAL — `risk_pct = kelly_fraction × f*` *(proposed)*

**Not yet implemented for position sizing.** The codebase uses fractional Kelly for strategy ALLOCATION in `src/m3s/evaluation.py` (Bayesian fractional Kelly, Baker-McHale 2013), but not for per-position sizing within a single strategy.

**Math**: For a binary bet with win probability `p`, payoff ratio `b = avg_win / avg_loss`:

```
f* = (b × p - q) / b     where q = 1 - p
risk_pct = kelly_fraction × f*
```

**Fraction choice**:

- **Quarter Kelly** (`f = 0.25`): ~44% of full-Kelly growth at ~25% of variance. "Safety first".
- **Half Kelly** (`f = 0.5`): ~75% of growth at ~50% of variance. Industry standard.
- **Full Kelly** (`f = 1.0`): Maximum theoretical growth, but 20-80% drawdowns routine. Avoid unless risk-tolerant.

**When to use**: After you have solid OOS statistics (win_rate, payoff_ratio, Sharpe) from >100 trades. Apply to strategies with stable edge.

**When NOT to use**: Low sample size (<50 trades). Unstable edge. Non-stationary markets.

### 2.6 DRAWDOWN_BUDGETED — `leverage = f(current_drawdown)` *(proposed)*

**Partially implemented**: `Compounder.target_leverage()` in `src/m3s/compounder.py` ramps leverage based on drawdown via mode-based picking. The proposed mode would formalize this as a first-class config knob.

**Typical ramp**:

| Drawdown | Leverage multiplier |
|---|---|
| < 0 (new HWM) | 1.5× baseline |
| 0-3% | 1.0× baseline |
| 3-5% | 0.7× baseline |
| 5-10% | 0.4× baseline |
| 10-20% | 0.2× baseline |
| ≥ 20% | 0 (pause) |

**When to use**: Compose with RISK_SCALED or VOL_TARGETED for production deployment. The ramp is the guard rail.

**Return-maximizing variant**: Invert the upper ramp — at new HWMs, go to 150-200% of baseline to compound winners harder.

---

## 3. Decision flowchart for picking a mode

```
Is your strategy a Pine port or direct TradingView replica?
  YES → INVARIANT (for parity research) or MARGIN_CAPPED (for production)
  NO → continue
          
Does your strategy have volatility-regime-dependent edge?
  YES → VOL_TARGETED (possibly composed with RISK_SCALED)
  NO → continue
          
Do you have solid OOS statistics (100+ trades, stable win_rate/payoff)?
  YES → KELLY_FRACTIONAL with fraction=0.5 (half-Kelly)
  NO → continue
          
Is your strategy's signal edge validated (OOS Calmar ≥ 1 under MARGIN_CAPPED)?
  YES → RISK_SCALED with baseline=current_deployment_L
  NO → MARGIN_CAPPED (don't amplify noise)

For production deployment of ANY active mode:
  Compose with DRAWDOWN_BUDGETED as the guard rail.
```

---

## 4. Hard rules for leverage-aware strategy design

### Rule 1 — Declare your `leverage_range` tuple

Every strategy MUST set `leverage_range: tuple[float, float]` in its `__init__`. This is advisory metadata that tells M3S, the risk manager, and the deep_backtest framework what range this strategy was tuned for.

```python
class MyStrategy(BaseStrategy):
    def __init__(self, ...):
        super().__init__(
            ...,
            leverage_range=(10.0, 50.0),  # REQUIRED
        )
```

Default is `(1.0, 1.0)` — "this strategy has not been tuned for leverage". Don't ship with the default unless you mean it.

### Rule 2 — Don't hide the `risk_pct / sl_pct` ratio

The notional multiplier `k = risk_pct / sl_pct` determines your strategy's leverage-invariance. Make this explicit in the strategy's class docstring:

```python
class MyStrategy(BaseStrategy):
    """...

    Sizing:
        risk_pct = 0.02 (2% of equity per trade)
        stop_distance = 3 × ATR(14)  (~3 × $10 = $30 at XAUUSD typical)
        → k ≈ 0.02 / (30 / 4500) ≈ 3.0
        → notional ≈ 3 × equity

    Mode: MARGIN_CAPPED (notional independent of leverage; leverage only
    gates margin footprint).
    """
```

### Rule 3 — Tune at your deployment leverage, not at 1×

If you plan to deploy at 10×, tune at 10×. If you plan to deploy at 25× in RISK_SCALED mode, tune at 25×. Don't tune at 1× and extrapolate — the drawdown distribution is NOT linear in leverage at high L.

### Rule 4 — Report cost_pct_of_margin in every backtest

At high leverage, commission becomes a major drag even though it's notional-based. At 1000× on cTrader, commission is 6% of margin per trade (from task #110). A strategy firing 1000 trades/year at 1000× burns 60× its initial margin in fees alone.

The deep_backtest framework reports this as `cost_pct_of_margin` in every cell. Watch it. If it's > 1% at your target leverage, reconsider.

### Rule 5 — Intrabar margin-call risk ≠ end-of-bar stop-out

At high leverage, a position can hit broker stop-out (50% margin level) intrabar EVEN IF the bar closes with the position still alive. This is why the engine uses `worst_adverse_price` (`bar.low` for LONG, `bar.high` for SHORT) to check stop-out per bar.

At leverage L with drawdown D, broker stop-out fires when:
```
(unrealized_loss / entry_margin) > 0.50
(price_move / entry_price) × L > 0.50
price_move > (0.50 / L) × entry_price
```

At L=50, a 1% adverse move wipes margin. At L=25, 2%. At L=10, 5%. The 50% stop-out threshold caps your effective leverage at `L_max ≈ 0.5 / expected_adverse_move_per_bar`.

### Rule 6 — Every deployment leverage recommendation must include drawdown budget

Don't recommend "run at 20×". Recommend "run at 20× with expected max DD of 15% and catastrophic DD tail of 40%". The drawdown budget is how you know whether the leverage fits the capital source (personal account tolerance vs prop firm bounds vs hedge fund allocations).

### Rule 7 — Never run full Kelly

The Kelly criterion maximizes LONG-RUN growth rate, but requires infinite time and no path dependence. Real portfolios have path dependence (can't lose more than 100% of equity) and finite horizons (you need the money sometime).

**Maximum recommended Kelly fraction: 0.5 (half-Kelly).** Prefer 0.25-0.33 in production. Full Kelly is for closed-form theoretical analysis only.

### Rule 8 — Compose modes deliberately

The proposed modes are not mutually exclusive:

```
DRAWDOWN_BUDGETED
    └── RISK_SCALED
            └── VOL_TARGETED (implicit in vol_momentum_gold)
```

Composition is valid. Start from the innermost mode (the strategy's native sizing) and wrap outer modes. The outer modes should never INCREASE the innermost mode's risk — only decrease or hold.

### Rule 9 — Verify leverage variance in Phase 2 before trusting the matrix

If you expect RISK_SCALED behavior (linear return vs leverage) but the Phase 2 sanity check reports all-invariant triplets, something's wrong. Either:
- Your strategy silently dropped the leverage parameter
- The config is on the wrong mode
- Indicators not pre-computed → zero trades → trivially invariant

The deep_backtest framework's Phase 2 catches all three by asserting variance when the mode expects it.

### Rule 10 — Deploy at half the backtest's recommended leverage

Backtests are optimistic. Real markets have slippage, latency, gap risk, and regime shifts that backtests don't capture. Always deploy at HALF the leverage the backtest recommends, for at least 30 days. If performance matches backtest, ramp to full recommendation. If it diverges, investigate before scaling.

---

## 5. Quick reference table

| Mode | Position size vs leverage | Sweet spot L | Typical use | Drawdown profile |
|---|---|---|---|---|
| INVARIANT | Constant | Any | Pine ports, research | Low, strategy-determined |
| MARGIN_CAPPED | Constant above threshold | k = notional/equity ratio | Signal-edge strategies | Low, strategy-determined |
| VOL_TARGETED | Scalable [0.5, 2.0] × base | Any | Regime-dependent alpha | Low-medium, adapts to vol |
| RISK_SCALED | Linear in L | baseline × 1.5 (half-Kelly) | Amplifying validated edge | Medium-high, linear in L |
| KELLY_FRACTIONAL | Constant (Kelly formula) | N/A (formula-determined) | Stable-edge strategies | Medium at half-Kelly |
| DRAWDOWN_BUDGETED | f(dd) | Composed w/ other modes | Production guard rail | Bounded by ramp |

---

## 6. Existing gold strategies — mode assignment

| Strategy | Current mode | File | Recommended mode | Notes |
|---|---|---|---|---|
| `swift_alma` | INVARIANT | `src/strategies/trend_following/swift_alma.py` | Keep INVARIANT | Pine parity; regime filter needed before scaling |
| `donchian_gold` | MARGIN_CAPPED | `src/strategies/trend_following/donchian_gold.py` | **RISK_SCALED** (baseline=10, deploy L=15) | Half-Kelly per task #113 analysis. **Empirically validated 2026-04-15**: WF continuous Calmar +11.7, annual return +140%, 5/6 profitable folds. ✅ DEPLOYABLE. |
| `vol_momentum_gold` | VOL_TARGETED | `src/strategies/momentum/vol_momentum_gold.py` | **MARGIN_CAPPED** (L=10-15, default vol_target=0.20) | **Research's §6.2 RISK_SCALED recommendation was empirically WRONG** — see research report §11. vol_mom's base edge is too modest for the return-biased gate at any RISK_SCALED leverage (L=12 annual 14.6%, L=20 annual 24.2%, gate requires 100%). The strategy is **risk-efficient** (high Calmar) but **low-return**, belongs in MARGIN_CAPPED mode where the gate is `Calmar ≥ 1.0` which it easily passes. Task #114 cross-validation showed DEPLOYABLE at continuous Calmar +4.93. Use as diversifier, not return engine. |

See the full research report at [`reports/leverage_strategy_research_2026-04-15.md`](../reports/leverage_strategy_research_2026-04-15.md) §11 for the empirical validation runs and corrected recommendations.

### Key lesson — mode selection is strategy-specific

The empirical validation surfaced a subtle framework insight: **the Phase 5 verdict gate is mode-specific**, and choosing the wrong mode can make a deployable strategy look like a failed one.

- **Return-hungry, high-edge strategies** → RISK_SCALED with high baseline (donchian_gold fits here)
- **Risk-efficient, low-return strategies** → MARGIN_CAPPED (vol_momentum_gold fits here)
- **Pine ports** → INVARIANT (swift_alma fits here)
- **Vol-regime-dependent** → VOL_TARGETED alone (not composed with RISK_SCALED)

The `leverage_mode` enum is a tool for matching sizing philosophy to strategy edge profile. It's NOT a universal "amplify returns" button. Applying RISK_SCALED to a risk-efficient strategy tests the wrong thing — the gate asks "does this strategy produce enough return to justify aggressive sizing?" and risk-efficient strategies answer "no" even when they're otherwise healthy.

---

## 7. Implementation status

As of 2026-04-15 (task #114):

- ✅ INVARIANT mode — `LeverageMode.INVARIANT` passthrough in `src/backtest/deep_backtest.py`
- ✅ MARGIN_CAPPED mode — `LeverageMode.MARGIN_CAPPED` (default, preserves pre-task-#114 behavior)
- ✅ VOL_TARGETED mode — `LeverageMode.VOL_TARGETED` passthrough (strategy's own vol scalar handles sizing in `vol_momentum_gold`)
- ✅ **RISK_SCALED mode — task #114** — `_apply_leverage_mode()` transforms `max_risk_per_trade` to `base × (L / baseline_leverage)`. Linear return amplification demonstrated: donchian_gold L=10 → +21.62%, L=20 → +45.87% (2.12× linear, 8 trades each).
- ✅ **KELLY_FRACTIONAL mode — task #114** — Computes `f* = (b×p - q) / b` from `kelly_win_rate` + `kelly_payoff_ratio`, multiplies by `kelly_fraction` (default 0.5 = half-Kelly). **Hard-capped at 0.25 absolute risk_pct** to prevent full-Kelly blowups (§4.1 of the research report).
- ❌ DRAWDOWN_BUDGETED mode — **deferred**. Requires per-bar equity-curve state that doesn't fit the single-pass matrix model. Can be added when the framework gets per-bar hooks.

The feature ships with an **interactive TUI** (`src/backtest/deep_backtest_interactive.py`) that prompts the user to tick-select windows / timeframes / fees / leverages / leverage modes via `questionary` before the backtest runs. Falls back to CLI-flag behavior when `questionary` isn't available or stdout isn't a TTY. Multi-mode runs write to separate report directories.

Invocation: `PYTHONPATH=. python3 scripts/deep_backtest.py <strategy>` (fully interactive) or with `--non-interactive` + explicit flags for CI use.

### §8 — Zero-tolerance validation gate (task #115)

For the return-amplifying modes (RISK_SCALED, KELLY_FRACTIONAL), the deep_backtest pipeline runs an additional **Phase 2.5 leverage_mode validation** with HARD-fail assertions that force `verdict = FAILED` regardless of Calmar or return metrics. Plus a Phase 0 preflight read-back probe that catches strategy/config mismatches before any compute is burned.

**Phase 0 read-back probe** — when `leverage_mode in (RISK_SCALED, KELLY_FRACTIONAL)`:
1. Compute expected `risk_pct` via `_apply_leverage_mode(config, baseline_leverage)`
2. Instantiate the strategy with the mode-adjusted params
3. Read back `getattr(strategy, risk_pct_param_name, None)` and compare to expected
4. If the strategy's `__init__` silently ignored or overrode the kwarg → raises `RuntimeError` with a specific error message pointing at the fix needed (either `config.risk_pct_param_name` or the strategy's `__init__`)

**Phase 2.5 `_validate_risk_scaled` — 9 hard-fail assertions**:
1. Trade count at L1 == L2 (signals sizing-independent)
2. Same side per trade
3. Same entry prices (fills depend only on path + fee model)
4. Quantity at L2 ≈ (L2/L1) × L1 per trade (±15% to tolerate compounding drift)
5. P&L at L2 ≈ (L2/L1) × L1 per trade (±15%)
6. Total P&L at L2 ≈ (L2/L1) × L1 aggregate (±10%)
7. Read-back: `strategy.max_risk_per_trade` matches `_apply_leverage_mode` output
8. Margin ratio ≈ 1.0 (notional doubles + leverage doubles cancel)
9. Commission scaling (±15%, catches cost-model contamination)

**Phase 2.5 `_validate_kelly_fractional` — 6 hard-fail assertions + 2 warnings**:
1. Kelly priors required (`kelly_win_rate`, `kelly_payoff_ratio`)
2. Trade count at L1 == L2 (Kelly is leverage-invariant)
3. Quantity identical across L1/L2
4. P&L identical across L1/L2
5. Read-back at L1 matches expected Kelly-computed risk_pct
6. Read-back at L2 matches expected Kelly-computed risk_pct
Warnings: hard-cap clamp (when `f* × fraction > 0.25`) and unrealistic priors (`win_rate > 0.90` or `payoff_ratio > 10`)

**Verdict override**: if Phase 2.5 has any failures, Phase 5 forces `verdict = FAILED` with a reason string listing the first 3 failures. This catches silent sizing-transform bugs BEFORE they produce a DEPLOYABLE verdict on real capital.

**Engine-level margin-rejection counting**: `LeveragedBacktestResult.open_rejected_count` now tracks positions rejected by `book.open_position`'s margin check. Surfaced in `CellResult.rejected_positions` and as `margin_rejection_warnings` in Phase 2 sanity checks (flags cells with > 30% rejection rate as margin-starved).

See `reports/leverage_strategy_research_2026-04-15.md` §7 and `tests/test_backtest/test_deep_backtest.py::TestLeverageModeValidation` for the validation mechanism details and regression tests.

---

## 8. References

- [Full research report](../reports/leverage_strategy_research_2026-04-15.md) — task #113, 2026-04-15
- [Task #110 leverage validation](../reports/swift_leverage_validation_2026-04-15.md) — the Phase D counter-demo showed RISK_SCALED can 10× returns before DD wipeout
- [Task #109 SWIFT matrix](../reports/swift_full_matrix_2026-04-15/index.html) — baseline leverage-invariant SWIFT behavior
- External: Chan 2010, Matthew Downey 2024, Interactive Brokers Risk-Constrained Kelly 2024, Tradeify / Alpha Capital 2026 prop firm guides
