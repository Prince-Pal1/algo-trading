# 🪦 asian_range_breakout — Stage 1 kill

**Killed:** 2026-04-19 (Session 23 Day 2, pivot from stat-arb kill)
**Category:** REGIME (edge absent in 2024-2026 gold regime)
**Config:** was `config/strategies.toml [asian_range_breakout]`, `enabled = false`

## The claim

During the London session (07:00–11:00 UTC), XAUUSD often breaks out of the range established in the Asian session (00:00–07:00 UTC). The canonical playbook: enter on first break above the Asian high (or below the Asian low), SL at the Asian mid, TP at 1× range. Published on countless retail-forex blogs with claimed ~60% hit rates.

## What killed it

Stage 1 research (`scripts/research_asian_range_breakout.py`) on 2y of XAUUSD 15m data (2024-04 → 2026-04):

- **Breakout frequency**: 71.4% of days had a breakout ✅ (pattern exists)
- **Hit rate on 1× range**: 33.5% ❌ (gate was 55%)
- **Naive simulation (SL 0.5× range, TP 1× range)**: mean PnL −0.039 range-units, Sharpe **−0.87**, 36.5% win rate ❌

Exhaustive parameter sweep — all variants failed:

| Variant | Mean PnL | Sharpe | Win % |
|---|---:|---:|---:|
| Small Asian range (<p20) | −0.087 | −0.131 | 28.8% |
| Mid range | −0.043 | −0.070 | 35.7% |
| Large range (>p80) | +0.019 | +0.034 | 46.6% |
| 0.3 SL / 2.0 TP | −0.062 | −0.112 | 20.4% |
| **0.5 SL / 2.0 TP (best found)** | **+0.015** | **+0.019** | **36.2%** |
| 0.7 SL / 1.5 TP | −0.001 | −0.001 | 43.1% |
| 1.0 SL / 1.0 TP | −0.038 | −0.047 | 48.5% |

The "best" config (0.5 SL / 2.0 TP) nets +0.015 range-units per trade pre-fees — about 0.3 pips on a typical $20 Asian range. IC Markets cTrader commission is 0.3 pips/side = 0.6 pips RT + 0.3 pips spread-slip = ~1 pip total cost. **Edge is at best break-even; net-of-cost is negative.**

## Root cause (regime-specific, potentially temporary)

Gold's 2024-2026 regime was **exceptionally trend-driven** — XAUUSD rallied from ~$2000 to ~$4800 over the period. Strategies that trade *range breakouts* perform poorly in strong trend regimes because:

1. The Asian range is compressed relative to the full-day range. Both London AND NY generate moves that are multiples of the Asian range, so "breakout" happens routinely in BOTH directions on the same day.
2. First-move-of-London is frequently reversed when NY opens (07:00 UTC break → 13:30 UTC reversal). The TP=1× range often isn't hit before the reversal takes the SL out.
3. Session-filter-based patterns rely on **range regime** (Asian quiet, London/NY noisy). A sustained bull trend makes Asian NOT quiet — the Asian range in 2024-2026 averaged ~$19 which is bigger than full-day ranges used to be in 2019-2021.

## Revival conditions

- Gold enters a **ranging regime** (30-day ATR/price < 0.5%; currently it's 1.5%+). Retest quarterly.
- A confirmation filter is added (e.g., "only take the breakout if price has been within 1 ATR of the Asian range for the last 5 bars"). Not worth the filter-stacking overfitting risk right now.
- A different base symbol is chosen (EURUSD? USDJPY?). The Asian range pattern may have more edge on currencies where Asian IS the quiet session; XAUUSD's Asian session is already noisy in a bull regime.

## Do NOT re-attempt without

- Regime check: XAUUSD 30-day ATR/price < 0.5% AND 90-day trend-direction flat.
- Or a completely different base symbol.
- Alternative: if we ever want a gold mean-reversion strategy, use Bollinger+RSI style on the 15m chart (similar to `bb_rsi_mr` on altcoins), NOT session-based breakout.

## Files

- Research script: `scripts/research_asian_range_breakout.py` (reusable for any session-breakout pattern on any symbol)
- Report: `reports/asian_range_stage1/report.json`
- Config section: `config/strategies.toml [asian_range_breakout]` — keep `enabled = false`; do not delete (config is documentation of the attempt).

## Session 23 Day 2 impact

Strategy 2' slot is now **also killed**. Strategy 2 (stat-arb pairs) killed earlier today for the same Stage-1-no-edge reason, but different root cause (cointegration break, not regime mismatch).

**Net position after Session 23 Day 2:**
- Strategy 1 funding_mean_reversion: SHIPPED to shadow mode ✅
- Strategy 2 stat_arb_pairs: KILLED (cointegration broken)
- Strategy 2' asian_range_breakout: KILLED (no edge in current gold regime)
- Strategy 3 liquidation_cascade: NOT STARTED

The "50% strategy mortality rate" prior baked into the plan was accurate — we're hitting it. Moving to Strategy 3 (liquidation cascade) directly without another pivot attempt, as that's the last novel-edge-source candidate in the backlog that fits our infrastructure.
