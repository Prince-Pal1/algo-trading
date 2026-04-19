# 🪦 stat_arb_eth_sol (+ BTC/LTC + ETH/BNB fallbacks) — Stage 1 kill

**Killed:** 2026-04-19 (Session 23 Day 2, Strategy 2 of the 3-strategy diversification cycle)
**Category:** REGIME (structural break — cointegration broken on all major crypto pairs post-2022)
**Sibling:** `strategy_btc_neutral_mr` obituary (same root cause, same family of failures)

## The claim

Crypto major-pair stat-arb (Ernie Chan ch.5 playbook) — identify two highly-correlated L1 tokens, estimate cointegration via Johansen, z-score the residual spread, trade mean reversion to equilibrium. The canonical ETH/SOL pair tested in Lopez-Ordieres et al. 2023 showed Sharpe 1.4 on 2020–2023. Published target Sharpe 1.4 haircut to 1.0 live.

## What killed it

Stage 1 cointegration testing (`scripts/research_stat_arb_eth_sol.py`) on 4.3y of 1h data per pair:

| Pair | Engle-Granger p | Rolling 90d corr (min) | Half-life | Windows cointegrated at 5% |
|---|---:|---:|---:|---:|
| **ETH / SOL** (primary) | 0.157 | 0.091 | 67.8 days | 4% of 50 windows |
| **ETH / BNB** (fallback) | 0.248 | **−0.313** | 92.1 days | 18% of 50 windows |
| **BTC / LTC** (fallback) | 0.923 | **−0.587** | 43.4 days | 10% of 50 windows |

Stage 1 gates required:
- Engle-Granger p < 0.01 (strong cointegration) — **ALL FAIL**
- OU half-life 3–14 days — all 3 pairs have half-lives > 40 days
- Rolling min correlation > 0.7 — ETH/BNB and BTC/LTC actually go NEGATIVE
- Rolling windows cointegrated at 5% ≥ 85% — no pair exceeded 18%

## Root cause (structural, not fixable)

All three pairs show the same pattern: stable cointegration pre-2022 breaks apart as the crypto market matures and assets develop their own narratives. Per-pair attribution:

- **ETH/SOL**: Solana's 2024 meme-season rally + DePIN narrative + the March 2024 outage created periods where SOL had zero beta to ETH. Cointegration window that was stable 2020-2021 fragments into disjoint regimes in 2024-2026.
- **ETH/BNB**: BNB is a centralized-exchange token (Binance's fortunes) while ETH is a decentralized-L1 narrative. After the 2023 CZ/Binance regulatory actions, the two tokens trade on entirely different drivers — correlations periodically go negative.
- **BTC/LTC**: LTC is the classic "abandoned altcoin" — retail interest is ~zero in 2024-2026 vs the 2017-2018 peak. Dominant BTC-ETF flows don't propagate to LTC. The two assets no longer respond to the same macro-crypto drivers.

## Sibling obituary

This failure exactly matches `docs/strategy_obituaries/strategy_btc_neutral_mr.md` (killed Session 7): "BTC ETF approval (Jan 2024) broke BTC-altcoin cointegration. LSE 2026 paper confirms structural break in spot-futures basis." Both strategies assumed stationarity of a multi-asset relationship that post-2022 crypto has structurally abandoned.

## Revival conditions

1. **New-generation tight-cointegration pair** — not the obvious "two big L1s" pattern. Examples worth researching: WBTC/BTC (wrapped on Ethereum, should track perfectly — but the edge is arb'd in seconds by MEV bots), ETH/stETH (Lido staking derivatives). None of these are on our current broker universe.
2. **Dynamic-cointegration approach** — Kalman filter beta estimation instead of static OLS. Permits the cointegration vector to drift. But the BTC-Neutral obituary's LSE 2026 paper explicitly says "rolling-OLS adapts to noise, z-score reverts mathematically but price doesn't move profitably" — so Kalman may just over-adapt to noise.
3. **Wait for a new regime** — 2026+ may bring a return to herd-correlated altcoins. Re-check every 6 months by re-running this Stage 1 script. If cointegration stability rises above 50% on any pair, reopen the investigation.

## Do NOT re-attempt without

- A tight-cointegration pair with published 2024+ evidence of persistence (not 2017-2020 papers that predate the ETF regime).
- At least 2 consecutive 6-month periods where the chosen pair's Engle-Granger p < 0.05 in EVERY 90-day rolling window.
- Ability to short at least one leg — spot-only Binance forces synthetic approaches that have worked poorly in backlog #4 prior attempts.

## Files

- Research script: `scripts/research_stat_arb_eth_sol.py` (reusable for future pair tests)
- Stage 1 reports: `reports/stat_arb_stage1/eth_sol.json`, `eth_bnb.json`, `btc_ltc.json`
- Existing strategy code: `src/strategies/stat_arb/pairs_trading.py` (keep — Johansen test + OU fitting are reusable primitives for any future pair-trade strategy)
- Sibling obituary: `docs/strategy_obituaries/strategy_btc_neutral_mr.md` (same family)

## Session 23 Day 2 impact on the 3-strategy plan

Strategy 2 slot is now free. Pivoting the budget to a different backlog item rather than stopping at 2 strategies. Recommendation: **asian_range_breakout** (XAUUSD 15m) — config skeleton exists, reuses session-filter logic from donchian_gold, complements the gold book which currently has two mid-session overlap strategies.
