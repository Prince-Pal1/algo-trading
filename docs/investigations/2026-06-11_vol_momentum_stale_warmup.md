# vol_momentum "edge decay" — actually a stale warmup-cache bug (2026-06-11/12)

## Trigger

Prince asked why vol_momentum (the only active crypto strategy, previously
PF 3.60) was suddenly losing: −$89.99 over 30 days across 56 round trips at
45% WR, with the book locked ~100% short (54/56 trips).

## Verdict

**SYSTEMATIC, not logical.** The strategy was not seeing the market. Its
momentum input was poisoned by a two-month-stale warmup cache re-injected on
every engine restart.

## Root cause chain

1. **Warmup parquet cache froze 2026-04-17.** `data/historical/
   {DOGE,ADA,DOT}USDT_1h.parquet` were last written during Session 23 prep.
   Nothing in the live path refreshes them. (`XAUUSD_1h.parquet` similarly
   froze 2026-05-06.)
2. **Warmup had a row-count check but no recency check**
   (`src/data/warmup.py`): `if len(df) < min_candles: download()`. The cache
   had 17,927 rows, so the fresh-download fallback never fired. Every
   restart primed indicator/strategy buffers with April candles.
3. **Engines restart 2–3×/day** (watchdog kicks on stale heartbeats; the Mac
   still slept occasionally — Session 26's `caffeinate -i` blocks idle sleep
   but not lid-close/forced sleep). vol_momentum's 168-bar momentum deque
   needs 7 uninterrupted days to flush the stale prefix; it got ~10 hours.
4. **"Momentum" became the price gap to April.** With the deque holding
   April prices at the reference index, `momentum = now/close[t-168] − 1`
   measured the April→June market drop, not 7-day momentum. Verified
   arithmetically against live signal metadata (2026-06-10 14:00 UTC):

   | Symbol | live px | stale cache px | gap arithmetic | observed "momentum" |
   |---|---|---|---|---|
   | DOGE | 0.0846 | 0.0989 | −14.5% | −10.1% |
   | DOT  | 0.9498 | 1.3170 | −27.9% | −30.7% |
   | ADA  | 0.1649 | 0.2583 | −36.2% | −42.7% |

   Crypto fell since April ⇒ permanently negative momentum ⇒ structurally
   locked short, churning shorts on noise in chop. Gross P&L of period-C
   shorts was only −$17; **fees ($36) were two-thirds of the −$53 loss**.
5. **Phantom signal pollution (secondary bug).** Warmup replays candles
   through `StrategyRouter.on_features`, which logged every replay signal to
   the live `signals` table at stale prices (the engine discarded them for
   execution — `paper_executor` guard — and never audited them). 458 phantom
   rows in `trades.db`, 14 in `trades_gold.db`. These almost derailed this
   investigation ("the engine is dropping LONGs" was the first, wrong,
   hypothesis).

## Gold exposure (caught before damage)

`XAUUSD_1h.parquet` was stale at 4618 (May 6) while live gold traded ~4208
(−8.9%). donchian_gold (enabled, armed, zero fills since the Session 27
recovery) primes its Donchian channel from the same warmup path — live bars
under a stale channel look like a breakdown. The recency gate now skips
warmup instead of feeding poison when the cache is stale and no download is
available.

## Evidence quality notes

* The April "PF 3.60 winner" status rests on **7 round trips, +$152 of the
  +$111 net from a single DOT long**. Not a meaningful sample.
* **vol_momentum's live track record to date is unusable for judging its
  edge**: the only clean period is ~Apr 16–27 (n=7); everything after
  May 18 ran on poisoned momentum. The 30-day "losing streak" says nothing
  about the strategy's true edge. Quarantine these stats.
* Live signals vs phantoms are distinguishable by (a) offset into the hour
  (<5s crypto live; phantoms land at restart wall-clock) and (b) presence of
  a `signal_audit` row (audit only writes on the live path). On gold,
  audit linkage was essential: cTrader trendbars arrive minutes late, so
  offset alone would have misclassified ~200 live rows.

## Fixes shipped (2026-06-12)

1. `src/data/warmup.py` — recency gate: cache must be ≤3 bars old to be used
   directly; stale → fresh download (persisted back to parquet via merging
   `ParquetStore.save`); download unavailable → cache tolerated up to
   `stale_tolerance_min` (default `max(10 bars, 60h)` — floor covers gold
   weekend closure); beyond tolerance → **warmup skipped loudly**
   (`warmup_skipped_stale_cache` WARNING), strategies fill from live candles.
2. `src/data/warmup.py` — router storage detached during replay (restored in
   `finally`): warmup signals are no longer written to the `signals` table.
3. `scripts/operational/purge_warmup_phantom_signals.py` — one-shot purge of
   the 472 phantom rows (DB backups + CSV audit trail in `data/`). Rule:
   `offset ≥ 2min AND no signal_audit match`.
4. `scripts/launchd/*.plist` ×4 — `caffeinate -i` → `caffeinate -i -s`
   (also blocks system sleep on AC). Residual: battery + lid-close can still
   sleep; harmless now that warmup can't poison buffers.
5. XAUUSD 1h cache re-downloaded fresh from Dukascopy (full 2y so backtests
   keep their history — note: `scripts/download_xauusd.py` REPLACES the
   parquet, it does not merge).
6. Tests: `tests/test_data/test_warmup.py` 4 → 12 (recency gate ×4,
   tolerance override, suppression ×2; legacy tests made network-free — they
   had silently begun hitting live Binance).

## Follow-ups

* Re-judge vol_momentum (and the risk-bump scale-up question) only after
  ≥30 days of clean-data trades. Until then its live stats are quarantined.
* Gold engine warmup has no Binance fallback for XAUUSD — a stale-cache
  Sunday relaunch beyond 60h tolerance now skips warmup (safe but slow to
  re-arm: donchian_gold needs ~240 live bars). Proper fix: cTrader trendbar
  backfill in `ICMarketsFeed`. Tracked in ROADMAP.
* Periodic parquet refresh (cron or engine-side) would make restarts cheap
  and deterministic instead of download-dependent.

## Lessons (added to ARCHITECTURE.md Known Gotchas + memory)

* **A cache with enough rows is not a cache with the right rows.** Any
  warmup/backfill path must validate recency, not just length.
* **Replay paths must be side-effect-free.** Warmup wrote to a live table
  because the router couldn't tell replay from live; suppress side effects
  structurally (detach the writer), don't rely on downstream guards.
* **Restart frequency turns a small data bug into a permanent one.** The
  poisoned deque would have self-healed in 7 days of uptime; 10h restart
  cadence made it chronic.
