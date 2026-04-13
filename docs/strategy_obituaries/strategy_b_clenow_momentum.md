# Strategy B — Cross-Sectional Altcoin Momentum (Clenow): OBITUARY

**Date of death:** 2026-04-13 (Session 22)
**Stage killed:** B.3 (Initial Backtest Gate)
**Verdict:** 💀 KILLED — data-driven, not emotional. Pre-committed fallback engaged.

---

## How it died

B.1 research gate passed **borderline** — blended forward Sharpe estimate
was 0.15-0.35, above the 0.15 floor but well below any "strong alpha"
threshold. B.1 explicitly flagged this as the most-likely kill point and
set a strict B.3 KILL criterion: `aggregated cross-sectional Sharpe < 0.1`.

B.3 real-data backtest measurement:

| Metric | Value | Gate | Verdict |
|---|---|---|---|
| Rebalances | 90 | — | — |
| Total trades | 28 | > 0 | pass |
| Cross-sectional total return | +0.06% | — | flat |
| Average per-symbol Sharpe | **−0.124** | > 0.1 | **FAIL** |
| Worst per-symbol Sharpe | −0.897 (XRPUSDT) | — | — |
| Best per-symbol Sharpe | +0.701 (BNBUSDT) | — | — |

**Per-symbol results** (2 years, weekly rebalance, top-5 cutoff):

| Symbol | Sharpe | Return | Trades |
|---|---:|---:|---:|
| BNBUSDT | +0.701 | +0.09% | 7 |
| DOGEUSDT | +0.340 | +0.03% | 3 |
| DOTUSDT | +0.244 | +0.02% | 2 |
| ETHUSDT | +0.168 | +0.03% | 3 |
| AVAXUSDT | −0.005 | 0.00% | 3 |
| ADAUSDT | −0.568 | −0.03% | 2 |
| SOLUSDT | −0.401 | −0.04% | 5 |
| NEARUSDT | −0.702 | −0.02% | 1 |
| XRPUSDT | −0.897 | −0.02% | 2 |

4 positive, 5 negative — classic scatter with no net edge.

## Root cause hypothesis

1. **Publication decay.** Clenow is from 2015; crypto momentum has been
   studied and published to death since 2018. The 50% Sharpe decay
   (Bailey et al. "Deflated Sharpe") empirically holds — what was
   Sharpe 1.0-1.5 in 2015-2018 is now essentially zero at retail scale.

2. **Small universe + limited timeframe.** v1 test used 9 symbols and 2 years
   of resampled 1h→1d data, not the 30-50 symbol / 6-year ideal. The edge
   might appear with a larger universe (more dispersion for the ranker to
   exploit), but the baseline result is too weak to justify the additional
   data-engineering investment.

3. **Regime mismatch.** 2024-04 → 2026-04 covers a mixed bull/chop period
   where cross-sectional dispersion was compressed. Clenow works best when
   the top 5-10 coins outperform the rest by wide margins; in a "rising tide
   lifts all boats" regime, the rank-based approach produces whipsaws.

4. **Weekly rebalance with short lookback.** v1 used 60-day lookback at
   7-day rebalance cadence. This is short for Clenow (original uses 90-day
   lookback, weekly for equities). Adjusting the cadence per Clenow's
   original rules might help, but the signal already shows it's not strong
   enough to survive the noise.

## What went right

- The pipeline (universe manifest → rank cache builder → RankCache
  primitive → ClenowMomentumStrategy → per-symbol backtest aggregation)
  **works end-to-end**. The kill is a signal-quality verdict, not an
  infrastructure verdict.
- The B.2 RankCache primitive is reusable for future rank-based strategies
  (cross-sectional mean reversion, factor rotation, etc.).
- The B.3 backtest gate was honest — the strategy got its chance and
  failed cleanly with a specific number.

## Lessons learned (update STRATEGY_DEVELOPMENT_PROCESS.md Lessons Log)

1. **Published momentum strategies have severe decay in crypto.** Clenow's
   2015 rules don't survive 2024-2026 retail-scale testing with realistic
   costs. Next attempts should start with stronger filters (regime, liquidity,
   momentum quality) before running any backtest.

2. **Borderline research gates should trigger stricter downstream gates.**
   B.1 passed at Sharpe 0.15 floor. The B.3 threshold of 0.1 was just
   barely achievable. Future borderline research decisions should
   explicitly raise B.3 thresholds to compensate for the low prior.

3. **Cross-sectional strategies need dense universes.** 9 symbols is too
   few for momentum ranking to dominate idiosyncratic risk. Minimum viable
   is 15-20 for alpha decomposition.

4. **Scope hacks that work** (rank cache + per-symbol aggregation pattern)
   should be documented for reuse even when the strategy they exercised
   was killed. The `RankCache` primitive and `build_momentum_rank_cache.py`
   builder are kept in the repo for future strategies.

## What's kept

- `src/strategies/ranking.py` — `RankCache` primitive, `compute_clenow_score`,
  `rank_weights_from_scores`. **Reusable for future rank-based strategies.**
- `scripts/build_momentum_rank_cache.py` — offline builder. **Reusable.**
- `config/universes.toml` — survivorship-bias-aware altcoin manifest.
  **Reusable.**
- `src/strategies/momentum/clenow_momentum.py` — strategy file. Kept but
  registered-and-disabled in `config/strategies.toml` (`enabled = false`).
  Future re-attempts can tweak parameters without rebuilding scaffolding.
- Test suite under `tests/test_strategies/test_ranking.py`,
  `test_clenow_momentum.py`, `test_momentum_backtest.py` — 38 tests,
  all still pass, **kept green so the infrastructure doesn't rot.**

## What's killed

- Config section `[clenow_momentum]` in `config/strategies.toml` stays
  with `enabled = false`. Never deployed to paper.
- Rank cache file `data/historical/momentum_rank_cache.parquet` is NOT
  produced from a full builder run (the test creates it in memory only).
- Sub-phases B.4 and B.5 are **skipped entirely** — no validation work,
  no M3S integration, no shadow mode deployment.

## Recovery path (if revived in the future)

1. Download 30+ altcoin 1d OHLCV for 6+ years including delisted tokens
2. Re-run `build_momentum_rank_cache.py` with the full universe
3. Re-run B.3 backtest gate — if aggregated Sharpe > 0.3 on the larger
   universe, proceed to B.4 with tightened regime filters
4. Consider meta-labeling (Phase 3c) as a filter to improve signal precision
5. If still weak: pivot to a different diversifier — funding rate momentum
   (rank by funding delta, not level) or carry-momentum hybrid

## Pre-committed fallback (per plan §Resolved Decisions)

Prince explicitly pre-committed to "accept 4-strategy book" (A + existing 3)
if Strategy B killed at its research gate. This is now the operational
outcome. Phase 3b-3 ships with:

- **Existing 3 strategies:** bb_rsi_mr_opt, donchian_ensemble_adx, vol_momentum
- **New strategy A:** funding_carry (Sharpe 2.584 on synthetic, gate passed)
- **Strategy B:** killed at B.3, obituary filed, no portfolio contribution

The 4-strategy book is what we ship.

---

**Rest in peace, Clenow Cross-Sectional Altcoin Momentum v1. You gave it your
best shot with 9 symbols and a 2-year window. We'll remember you when we
download 30+ symbol / 6-year daily data.** 🪦
