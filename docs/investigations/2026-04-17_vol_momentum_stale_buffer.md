# Investigation: vol_momentum reports stuck momentum = -1.61

**Date:** 2026-04-17
**Investigation file:** `~/.claude/plans/yes-do-a-deepinvestigation-merry-horizon.md` (Priority 2)
**Diagnostic:** `scripts/diagnose_vol_momentum_ada.py`

## Symptom

vol_momentum's live ADAUSDT signals over an 18-hour window all reported `momentum = -1.60 ± 0.02` despite ADA's price being range-bound at $0.2551–$0.2612 (a flat market). Confidence pegged at 1.0 on every signal.

## Diagnostic result

`scripts/diagnose_vol_momentum_ada.py` pulls the last 200 1h ADA candles directly from Binance, instantiates the production `VolMomentumStrategy` class, and replays. Result: **momentum drifts naturally in the −0.005 to +0.020 range** — exactly what one would expect on a flat week.

**Conclusion: the strategy code itself is correct.** When fed proper recent data, it produces sane momentum values.

## What's wrong

The math is unambiguous:
- Live signal: `close = 0.2496`, `momentum = -1.6142`
- `momentum = log(close / close_168_bars_ago)`
- → `close_168_bars_ago = 0.2496 × exp(1.6142) = $1.2542`

ADA was last at $1.25 in **November 2024**. The live engine's `vol_momentum._closes[0]` (the oldest entry in the 169-slot deque) is holding a price from 17 months ago.

## Why parquet warmup alone doesn't fully explain it

`data/historical/ADAUSDT_1h.parquet` ends at 2026-04-01 00:00 UTC. Warmup loads `tail(200)` from this file, giving bars 2026-03-23 → 2026-04-01. The implied warmup momentum is `log(0.2446 / 0.2676) = -0.0899`, not −1.61.

So **the parquet alone wouldn't produce the bug**. Something else is also feeding vol_momentum's `_closes` with bars from late 2024. Possible culprits to investigate (NOT yet checked):

1. **`BinanceDownloader.download(...)` fallback path** in `src/data/warmup.py:91-103` — if parquet load somehow returns 0 rows, the downloader runs with `start = now - lookback_min`. A bug in `start_str` formatting could cause it to fetch from 2024 instead of 7 days back.
2. **CandleBuilder backfill** — between warmup end and first live tick, something might be reconstructing missed bars from a stale source.
3. **A separate historical-fetch path** in the strategy router or feature engine that we haven't traced yet.
4. **Persistent strategy state** — although Python deques don't auto-pickle, some component might be restoring state from disk.

## Operational impact

- vol_momentum keeps emitting SHORT signals at every rebalance because momentum is locked deeply negative
- After the FatFingerGuard fix (commit `e36e075`), these signals will start filling — and they'll get **stopped out by the actual flat market**, generating losses
- This is now a HIGHER priority than originally rated

## Recommended next actions

**Immediate (before more trades fill):**
1. Refresh `data/historical/*_1h.parquet` files so warmup loads truly recent data. The parquet should end yesterday, not 2026-04-01.
2. After parquet refresh, restart the engine and verify vol_momentum's first signal has momentum value consistent with the diagnostic (small magnitude).

**If the bug persists after parquet refresh:**
3. Add temporary `_closes[0]` and `_closes[-1]` debug logging to vol_momentum.on_features and capture one full warmup cycle.
4. Trace the candle ingestion path for vol_momentum's deque — look for any path that loads pre-2025 bars.

**Until fixed:**
- Disable vol_momentum live execution OR
- Set `vol_momentum.enabled = false` in `config/strategies.toml` to prevent stop-out losses

## Files referenced

- `src/strategies/momentum/vol_momentum.py:84-98` — `_compute_momentum` (math is correct)
- `src/data/warmup.py:75-145` — warmup orchestration
- `src/data/downloader.py` — Binance historical downloader (suspect)
- `src/data/feature_engine.py` — feature pipeline that hands candles to strategies
- `data/historical/ADAUSDT_1h.parquet` — verified ends 2026-04-01, not stale enough to explain $1.25 alone
- `scripts/diagnose_vol_momentum_ada.py` — the diagnostic itself, reusable for any symbol
