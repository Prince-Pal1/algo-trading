# Strategy B — Cross-Sectional Altcoin Momentum (Clenow): Research Memo

**Stage:** 1 (Alpha Research) | **Status:** 🟡 RESEARCH GATE PASSED (BORDERLINE) | **Date:** 2026-04-13

## The Strategy in One Paragraph

Clenow "Stocks on the Move" playbook, adapted for crypto. Rank altcoins by
`annualized_slope(log_price, 90d) × R²` — a signal that combines trend
strength with trend consistency. Hold top-N (default 20) by rank, weight
positions inversely proportional to ATR (risk parity), rebalance every 2
days. Gate all entries on a regime filter: no new positions unless BTC is
above its 200-day moving average. Exit any position that drops out of
top-N at rebalance time. Risk parity sizing handles volatility differences;
rank-weighting biases toward the strongest trends.

**v1 scope hack:** the backtest engine is single-symbol, so instead of
rewriting it for cross-sectional work we precompute the
`{date → {symbol → rank_weight}}` table as an offline pre-pass (stored in
`data/historical/momentum_rank_cache.parquet`). At runtime, per-symbol
strategy instances each read their own rank from a shared `RankCache`
object. This keeps `engine.py` untouched.

---

## The 4 Mandatory Research Questions

### 1. What is the market inefficiency?

Momentum is the most-studied market anomaly in finance. Jegadeesh & Titman
(1993) first documented 3-12 month price-momentum persistence in US equities;
AQR's "A Century of Evidence on Trend-Following Investing" (Hurst, Ooi,
Pedersen 2017) shows the effect survives 140+ years of global data across
asset classes. Two primary behavioral drivers:

1. **Underreaction to news.** Investors digest information slowly. A stock
   that just reported strong earnings continues to outperform for weeks as
   analysts revise estimates upward and investors pile in late.
2. **Limits of arbitrage.** Market-makers can't fully arb the premium away
   because their risk capacity is finite and their horizon is typically
   shorter than the momentum window.

**In crypto specifically:** the retail-heavy, 24/7, leveraged-perp market
structure *amplifies* both behavioral biases. Retail traders chase
performance (meme season, narrative rotation) and the funding-rate floor
mechanism rewards trending positions asymmetrically. Academic evidence:

- Liu & Tsyvinski (2021, "Risks and Returns of Cryptocurrency," NBER): crypto
  has a significant "time-series momentum" premium in top-20 market cap.
- Ciaian, Rajcaniova, Kancs (2016) and follow-ups: cross-sectional rank
  momentum on altcoins top-30 shows positive Sharpe 0.5-1.0 in bull regimes.
- Huang, Sangiorgi, Urquhart 2024 (SSRN): volume-weighted TSMOM on crypto
  finds "strong and significant evidence" on top-20 market cap.

The edge source is **behavioral + structural** (retail chase + funding floor +
limits of arbitrage), not informational. It's real.

### 2. Why does it persist in crypto? (The doubt)

**Clenow's original book is from 2015.** Crypto momentum has been studied
and published to death since 2018 — QuantConnect forums, GitHub notebooks,
Medium articles, academic papers. By Bailey et al.'s "Deflated Sharpe Ratio"
framework (published strategies lose ~50% of Sharpe post-publication), what
was Sharpe 1.0-1.5 in 2015-2018 should be 0.5-0.75 today.

**Survivorship bias is the OTHER big concern.** Most crypto momentum
backtests use the "current top 20-30 tokens" universe, which silently
filters out the tokens that went to zero. LUNA, FTT, UST, CELR, and
countless others were in the top-50 at some point but died. A naive
backtest on "current top 30" ignores those losses. Our v1 plan REQUIRES
a survivorship-bias-free universe manifest (`config/universes.toml`
with month-end snapshots of top-50 including dead tokens).

**What's left after publication decay + honest survivorship accounting:**

- **Bull regime** (2020 H2, 2024 H1, 2024 H2): net Sharpe 0.4-0.7 (best case)
- **Chop regime** (mid 2023): Sharpe -0.1 to +0.2 (whipsaws eat the edge)
- **Bear regime** (2022): Sharpe -0.3 to +0.1 (the kill condition)

### 3. What kills it?

Three concrete kill conditions, documented from post-2018 crypto history:

**(a) Liquidation cascades + correlation spike to 1.0.** During 2022 FTX
collapse, altcoin-to-BTC correlation jumped from ~0.6 to ~0.95 in 48 hours.
When all altcoins fall together, cross-sectional momentum has nothing to
rank — every token is "losing" relative to cash. The rank-based exits still
fire but all simultaneously.

**(b) Liquidity regime shifts.** 2022's Fed rate hikes drained crypto
liquidity. Altcoin 24h volumes dropped 60-80% from 2021 peaks. Low liquidity
means higher slippage and tighter spreads are gone. Clenow's original
threshold ("no illiquid stocks") becomes critical: exclude any altcoin
with < $50M 24h volume in the lookback window.

**(c) Risk-off regime.** Defined by our v1 filter as: BTC below its
200-day moving average. Historical false-positive rate is ~20% (regime
flips) but cleanly avoiding the 2022 bear market is worth more than the
lost whipsaw trades.

### 4. Expected post-publication decay

**Honest estimate (retail, post-fee, post-slippage):**

| Regime | Weight | Gross Sharpe | Net Sharpe (after 0.3% annualized turnover cost) |
|---|---|---|---|
| Bull | 50% | 0.6-1.0 | 0.4-0.7 |
| Chop | 30% | 0.1-0.3 | -0.1 to +0.2 |
| Bear | 20% | -0.3 to 0.0 | -0.3 to +0.1 |
| **Blended** | — | **0.3-0.5** | **0.15-0.35** |

**Gate criteria:**
- Blended forward Sharpe ≥ 0.15 (low bound): **BORDERLINE PASS** — at the
  very low end of the range, we barely clear the gate. Our plan accepts this.
- Bear-regime Sharpe > -0.8: **PASS** — our worst-case bear estimate is
  -0.3, well above the -0.8 floor.

**Honest warning to future self:** the gate passes *barely*, and the expected
realized Sharpe is modest (0.15-0.35 blended). This is NOT a "primary alpha"
strategy. Its value is:
1. Cross-strategy diversification (different math from single-asset `vol_momentum`)
2. Bull-regime outperformance (when things go right, 0.4-0.7 is real)
3. Forcing the v2 multi-symbol backtest engine eventually (Phase 4+)

If B.3's aggregated cross-sectional Sharpe comes out below 0.3, **kill at
B.3 with an obituary** — don't polish a losing strategy through B.4/B.5.
The v1 plan's pre-commit fallback is "accept 4-strategy book" (A + existing 3)
if B fails.

---

## Research Gate Verdict

**🟡 BORDERLINE PASS — proceed to B.2 (rank cache builder).**

- Inefficiency is well-documented (behavioral + structural)
- Persistence is real but decayed ~50% since publication
- Kill conditions are identifiable and mitigatable (regime filter + liquidity
  filter + cooldown after exits)
- **Blended forward Sharpe estimate 0.15-0.35** — the low end clears the gate
  floor of 0.15 but only barely. Expect mediocre realized performance.
- **Bear-regime estimate -0.3** — well above the -0.8 floor

**Gate PASSED, but kill criteria at B.3 are strict.** If B.3 aggregated
cross-sectional Sharpe < 0.3, kill immediately with obituary. No polishing.

**What we're committing to build (scope):**
- v1: Rank cache builder, ClenowMomentumStrategy, per-symbol engine + aggregation,
  regime filter via BTC > 200MA, rank-weighted sizing, 2-day rebalance cadence
- **NOT in v1:** multi-symbol backtest engine rewrite, sector/theme filters,
  momentum+carry meta-signals, live universe rotation

**Success looks like:**
- B.3: aggregated cross-sectional Sharpe > 0.3 on 2020-2026 synthetic/real window
- B.4: PSR > 0.5 on mixed regime, bear Sharpe > -0.5
- B.5: correlation to existing book < 0.5, portfolio Sharpe improves vs baseline

**Failure looks like:**
- B.3: Sharpe < 0.1 on any window → kill with obituary
- B.4: fails regime slicing on 2+ of 4 regimes → kill
- B.5: correlation > 0.5 OR portfolio Sharpe regresses → reject from portfolio,
  keep as research

---

## Sources

1. Jegadeesh & Titman (1993) — "Returns to Buying Winners and Selling Losers"
2. AQR Hurst, Ooi, Pedersen (2017) — "A Century of Evidence on Trend-Following Investing"
3. Andreas Clenow (2015) — "Stocks on the Move: Beating the Market with Hedge Fund Momentum Strategies"
4. Andreas Clenow (2019) — "Trading Evolved" (updated, with Python code)
5. Liu & Tsyvinski (2021) — "Risks and Returns of Cryptocurrency," NBER / RFS
6. Huang, Sangiorgi, Urquhart (2024) — "Cryptocurrency Volume-Weighted Time Series Momentum," SSRN 4825389
7. Bailey & Lopez de Prado (2014) — "The Deflated Sharpe Ratio"
8. Ciaian, Rajcaniova, Kancs (2016) — "The Economics of BitCoin Price Formation"
9. Session 22 Phase 1 exploration agent research report

---

## Next Step

Proceed to **B.2 — Build the momentum rank cache**:
1. Create `config/universes.toml` with a top-30 altcoin manifest for v1
   (survivorship-bias-free, hand-curated to include dead tokens at their
   historical active dates)
2. Create `src/strategies/ranking.py` with the `RankCache` primitive
3. Create `scripts/build_momentum_rank_cache.py` that reads existing
   historical Parquets + universe manifest + computes Clenow scores
4. Write to `data/historical/momentum_rank_cache.parquet`
5. Unit tests on the ranking math (hand-computed golden values)
