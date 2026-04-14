# Broker Fee Profiles & Cost Modeling

This is the canonical reference for broker fee assumptions used in backtesting. Every backtest must select a fee profile explicitly — there is no implicit default for "realistic mode" anymore, because the wrong defaults killed multiple strategies in the past (see ARCHITECTURE.md gotcha 2026-04-14, the 90× slippage bug).

## TL;DR — pick a profile before backtesting

```python
from src.backtest.fee_profiles import make_fee_model
from src.backtest.leveraged_engine import LeveragedBacktestEngine

# Production-style backtest on cTrader gold (default for IC Markets)
engine = LeveragedBacktestEngine(
    fee_model=make_fee_model("ic_markets_ctrader_xauusd_normal"),
    ...
)

# Cheaper alternative — MT4 saves ~$53/trade for gold
engine_mt4 = LeveragedBacktestEngine(
    fee_model=make_fee_model("ic_markets_mt4_xauusd_normal"),
    ...
)

# Stress test — 2× normal spread + slip
engine_stress = LeveragedBacktestEngine(
    fee_model=make_fee_model("ic_markets_ctrader_xauusd_stress"),
    ...
)

# Pine Script port validation only (Stage 0, NOT for production)
engine_pine = LeveragedBacktestEngine(
    fee_model=make_fee_model("pine_zero_cost"),
    ...
)
```

## The 7 profiles currently defined

| Profile | Broker | Platform | Instrument | Scenario | Per-trade cost (1 lot @ $4500) |
|---|---|---|---|---|---|
| `ic_markets_ctrader_xauusd_normal` | IC Markets | cTrader | XAUUSD | normal | ~$36 spread+commission, ~$94 with slip on busy days |
| `ic_markets_ctrader_xauusd_news_active` | IC Markets | cTrader | XAUUSD | news_active | as above, plus 5× widening during NFP/CPI/FOMC windows |
| `ic_markets_ctrader_xauusd_stress` | IC Markets | cTrader | XAUUSD | stress | ~2× normal — for sensitivity analysis |
| `ic_markets_mt4_xauusd_normal` | IC Markets | MT4 | XAUUSD | normal | ~$16 spread+commission (~3.86× cheaper than cTrader) |
| `ic_markets_ctrader_fx_majors_normal` | IC Markets | cTrader | FX majors | normal | ~$6 round trip on EURUSD-class pairs |
| `ic_markets_mt4_fx_majors_normal` | IC Markets | MT4 | FX majors | normal | ~$7 round trip — comparable to cTrader for FX |
| `pine_zero_cost` | (none) | (none) | (any) | pine_faithful | $0 — TradingView strategy tester defaults |

## Calibration sources

All numbers are research-calibrated from the 2026-04-14 audit (task #105). Sources cited inline in `config/broker_fees.toml`:

- [IC Markets Global — Lowest Spreads](https://www.icmarkets.com/global/en/trading-pricing/spreads) — official Raw spread numbers (XAUUSD avg 0.09 pips, EURUSD avg 0.10 pips), MT4 commission $3.50/lot/side, cTrader commission $3 per $100k notional
- [IC Markets EU — Trading Costs](https://www.icmarkets.eu/en/trading-pricing/trading-costs) — EU regulated entity numbers, distinguishes per-lot vs volume-based pricing for cTrader
- [IC Markets EU Commodity Specification Sheet (PDF)](https://cdn.icmarkets.eu/uploads/Commodity-Specification-Sheet.pdf) — spec sheet showing XAUUSD avg 0.63 pips on EU Raw (wider than Global because EU has different liquidity providers)
- [Best Brokers — IC Markets Spreads & Fees](https://www.bestbrokers.com/reviews/ic-markets/spreads-fees-and-commissions/) — independent third-party verification
- [Databasemart — Forex Slippage Testing Case Study](https://www.databasemart.com/blog/customer-stories-115) — measured EURUSD slippage on cTrader London server: 0.00001 (1 micro-pip)
- [Myfxbook — 99% Slippage-Free Advantage](https://www.myfxbook.com/press-release/-99-slippage-free-advantage/36625) — peer ECN broker (Exness) measured 99% slippage-free on XAUUSD
- [FXNX — Gold News Trading Guide](https://fxnx.com/en/blog/gold-news-trading-nfp-cpi-fed-rates-guide) — documented news-event widening on gold (3-10× spread during NFP/CPI/FOMC)
- [GetKnowTrading — XAUUSD Pip Calculator](https://getknowtrading.com/xauusd-pip-calculator) — pip convention reference

## Calibration choices — why these numbers

| Parameter | Value | Reasoning |
|---|---|---|
| `base_spread_pips` (gold) | **0.30 pips** | Conservative blend between IC Markets Global Raw (0.09) and EU Raw (0.63). Aligned with peer ECN Pepperstone (0.20-0.30). |
| `normal_slip_pips` (gold) | **0.30 pips** | ~30% above the spread baseline because gold has wider tick increments than EURUSD (where databasemart measured 1 micro-pip on cTrader). |
| `atr_vol_mult` | **0.0** | **THE BUG WE FIXED.** Was 0.5 → produced 35.6 pips of slip with median ATR $7. ECN slippage is bounded by order book depth, NOT bar volatility. The ATR scaling never made sense for raw cTrader fills. |
| `news_spread_mult` | **5.0** | Mid-range of documented 3-10× widening on NFP/CPI/FOMC. |
| `news_slip_mult` | **5.0** | Same reasoning as news_spread_mult. |
| `pip_size` (gold) | **0.10** | Verified for IC Markets 2-decimal XAUUSD quote. 1 pip = $0.10 price move = $10 P&L per lot (100 oz). |
| `pip_size` (FX) | **0.0001** | Standard 4-decimal FX quote. |
| `contract_size` (gold) | **100 oz/lot** | Industry standard. |
| `contract_size` (FX) | **100,000** | Standard FX lot size. |
| MT4 commission | **$3.50/lot/side FIXED** | IC Markets official spreads page. Same rate for FX and metals. |
| cTrader commission | **$3 per $100k notional VOLUME-BASED** | IC Markets EU trading-costs page. For gold @ $4500/oz × 100 oz = $450k → $13.50/side per lot. For FX (1 lot ≈ $100k) it equals MT4. |

## cTrader vs MT4 for gold — the 3.86× cost difference

For **gold-heavy strategies**, MT4 is significantly cheaper because cTrader's commission scales with notional value:

| Trade | cTrader | MT4 |
|---|---|---|
| 1 lot XAUUSD @ $4500 | $13.50/side | $3.50/side |
| Round trip per lot | **$27** | **$7** |
| For SwiftAlmaStrategy 1187 trades | $76,143 commission | $18,941 commission |
| As % of $1M starting equity | 7.6% | 1.9% |

Per CLAUDE.md the project deploys on cTrader, but **for gold-heavy strategies MT4 should be reconsidered**. The 5.7 percentage point savings in commission alone is meaningful for any strategy with >100 trades/year.

For **FX**, the platforms are nearly equal (cTrader $3 vs MT4 $3.50 per side per lot at 100k notional), so the platform choice should be driven by execution quality / features rather than fees.

## Leverage interaction — the silent killer

**Broker costs are LEVERAGE-INDEPENDENT in dollar terms** but become a much larger fraction of margin at high leverage. This is the critical conceptual point:

- A 1-lot XAUUSD trade on cTrader costs **$94** regardless of whether you use 1x or 1000x leverage
- The position notional is **$450,000** in both cases
- The MARGIN required differs: $450,000 at 1x vs $450 at 1000x

| Leverage | $94 cost as % of margin |
|---|---|
| 1x | 0.021% |
| 10x | 0.21% |
| 100x | **2.09%** |
| 500x | **10.4%** |
| 1000x | **20.9%** ⚠️ |

At 1000x leverage, **every trade costs 20.9% of your margin**. After ~5 trades you've burned 100% of margin in costs alone — before any market move.

This is why high-leverage scalping with frequent trades is so brutal. People think "1000x = 1000x cheaper trades" but the truth is the opposite: the dollar cost is the same, but as a fraction of risk capital it's amplified by exactly the leverage factor.

**Use the helper to sanity-check this for any new strategy:**

```python
from src.backtest.fee_profiles import cost_as_pct_of_margin

# 1 lot gold @ $4500 = $450k notional, $94 round-trip cost on cTrader
for lev in (1, 10, 100, 500, 1000):
    pct = cost_as_pct_of_margin(94.0, 450_000, lev)
    print(f"{lev:>5}x → {pct:.3f}% of margin per trade")
```

If your strategy generates 100+ trades per year and runs at >100x leverage, the cost-as-margin-percentage will eat your account even with positive gross alpha. Either:

1. **Reduce trade frequency** until cost × frequency < expected return
2. **Reduce leverage** until cost % is sustainable (rule of thumb: cost-per-trade < 1% of margin = OK)
3. **Switch to cheaper platform** (MT4 for gold) to reduce cost-per-trade
4. **Find a strategy with higher per-trade edge** — only profitable if expected return > total round-trip cost

## What the cost model does NOT capture

These are known gaps. The current model is honest about its limits:

- **Negative balance protection markup**: Some EU brokers add a small spread markup on retail accounts for negative balance protection. IC Markets Global (which we use as non-EU residents) doesn't apply this.
- **Margin interest / swaps**: Overnight positions accrue swap rates. Irrelevant for intraday strategies (SWIFT closes within hours), but matters for trend-following gold strategies that hold for days. To model: load swap rates from broker spec sheet and charge per overnight hold.
- **Account-tier slippage**: Some brokers tier slippage by account size — small retail accounts can get worse fills than institutional. IC Markets is an ECN so this is minimal there.
- **Liquidity provider failover**: If a broker's primary LP goes down, fills can be worse for a few seconds. Not modeled.
- **Fast-market widening beyond news windows**: Crash events (e.g., flash crashes, broker outages) can produce slippage well beyond the 5× news multiplier. Not modeled — these are "tail risks" that should be handled by stop-loss design, not cost modeling.
- **Latency-induced slippage**: Backtests assume bar-close fills with no execution latency. Live trading has 30-100ms order routing delay. For HFT strategies this matters; for SWIFT-style 5m bar strategies it's noise-level.

## How to add a new profile

1. Add a new `[profiles.your_profile_name]` table to `config/broker_fees.toml` with all required fields:
   - `description`, `broker`, `platform`, `instrument_class`, `scenario`
   - `sources = [...]` (URLs you used to calibrate)
   - `notes = """..."""` (rationale)
   - `[profiles.your_profile_name.spread]` table with all 7 spread fields
   - `[profiles.your_profile_name.commission]` table with `type` + the right pricing fields
2. Run `python3 -m pytest tests/test_backtest/test_fee_profiles.py` — the test asserts every profile has sources cited and the right structure.
3. Document the calibration in this file under "The 7 profiles" table.
4. Use it: `make_fee_model("your_profile_name")`.

## How to update an existing profile

If broker fees change (rare — IC Markets typically updates pricing once per year), update `config/broker_fees.toml` and bump the `[meta] last_updated` timestamp. The `sources` field should reflect when you re-verified the numbers.

The Python registry is reloaded once per process. Tests don't depend on a persistent cache.

## Stage 0 / Phase 2 workflow integration

When porting a Pine Script (see `docs/PINE_SCRIPT_PORT_WORKFLOW.md`):

- **Stage 0 (Pine port validation)**: use `pine_zero_cost` to reproduce TV's signals. Confirm 99%+ bar-by-bar match.
- **Phase 2 (strategy viability)**: use a real broker profile based on your deployment plan:
  - `ic_markets_ctrader_xauusd_normal` if deploying on cTrader (default)
  - `ic_markets_mt4_xauusd_normal` if deploying on MT4 (cheaper for gold)
  - `ic_markets_ctrader_xauusd_news_active` if the strategy trades through NFP/CPI/FOMC
  - `ic_markets_ctrader_xauusd_stress` to verify the strategy survives 2× cost increases
- **Cross-check with leverage helper**: compute `cost_as_pct_of_margin(per_trade_cost, notional, leverage)` for the strategy's intended leverage. If it exceeds 5%, the strategy is fragile.
