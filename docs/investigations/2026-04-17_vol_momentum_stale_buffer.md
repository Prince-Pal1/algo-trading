# Investigation: vol_momentum reports bogus momentum (cross-symbol contamination)

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

## REAL ROOT CAUSE (found after parquet refresh didn't help)

**After refreshing all 9 live-symbol parquets to end at today's last closed bar and restarting**, the momentum values remained nonsensical:

| Symbol | Current close | Reported momentum | Implied `past_close` | Reality check |
|---|---:|---:|---:|---|
| DOGEUSDT | $0.0989 | +0.0687 | $0.0924 | Plausible recent DOGE |
| ADAUSDT | $0.2505 | **+0.9331** | **$0.0985** | ← DOGE's current price! |
| DOTUSDT | $1.1640 | **+1.5567** | **$0.2457** | ← ADA's current price! |

The "past close" values are **cross-contamination** from other symbols. The pattern is unmistakable: ADA is reading DOGE's current price as its "168 bars ago" reference; DOT is reading ADA's.

**The cause:** `config/strategies.toml::vol_momentum.markets = ["DOGEUSDT", "ADAUSDT", "DOTUSDT"]`. The router calls `strategy_cls.from_config("vol_momentum")` **once** (`src/strategies/router.py:102`) to produce a **single `VolMomentumStrategy` instance**, then calls `router.register(strategy)` which maps that one instance to all three (symbol, timeframe) keys (`router.py:36-46`). Every subsequent `strategy.process(symbol, …)` hits the same object.

Meanwhile the strategy's state is class-instance attributes:
- `self._closes: deque` (at `vol_momentum.py:82`) — one deque shared across symbols
- `self._position: str` (at `BaseStrategy` — inherited)
- `self._bars_since_exit`, `self._bars_in_position`, `self._bars_since_rebalance`

When DOGE, ADA, DOT candles all feed into the same deque, the 168-bar lookback lands on whatever symbol happened to write 168 appends ago — which is a completely different symbol. Hence the wrong "past price."

## Scope of the bug

This likely affects **every multi-symbol strategy** in the codebase. Quick audit of `config/strategies.toml`:

| Strategy | Markets | Same-instance-multi-symbol? |
|---|---|---|
| `bb_rsi_mr` | 5 altcoins | YES (but strategy has no rolling buffer beyond single-bar features, so less fragile) |
| `donchian_ensemble_adx` | multiple | YES |
| `vol_momentum` | 3 altcoins | **YES — confirmed broken** |
| `funding_carry` | 1 synthetic | not affected |

**bb_rsi_mr's low signal rate may be partially explained by this too** — if the `_position` state bleeds across symbols, a LONG position on ETHUSDT prevents a LONG signal on ADAUSDT. That aligns with the "1 signal in 5 days" observation, though the strict RSI<25 + ADX<20 parameters are probably the dominant factor.

## What's been done (2026-04-17 session)

1. **Parquet refresh** (commit pending): `scripts/refresh_historical_parquets.py` re-downloads 60 days of 1h bars for all 9 live symbols. Useful regardless — parquets were 6-7 days stale.
2. **vol_momentum disabled** in `config/strategies.toml` to prevent stop-out losses now that the FatFingerGuard fix lets bad signals fill.
3. **Investigation trail preserved** — this file + `~/.claude/plans/yes-do-a-deepinvestigation-merry-horizon.md`.

## Proper fix (separate PR — NOT yet implemented)

Refactor multi-symbol strategies to key their internal state by symbol. Concrete plan:

**Option A — Per-symbol dicts inside each strategy**:
```python
# In VolMomentumStrategy.__init__:
self._closes: dict[str, deque[float]] = {}
self._position: dict[str, str] = {}
self._bars_since_exit: dict[str, int] = {}
# ...

def _closes_for(self, symbol: str) -> deque[float]:
    if symbol not in self._closes:
        self._closes[symbol] = deque(maxlen=max(self.momentum_window, self.vol_lookback) + 1)
    return self._closes[symbol]
```
Pros: minimal change, contained. Cons: every multi-symbol strategy needs the same pattern; easy to forget in a new strategy.

**Option B — Router instantiates one strategy per symbol**:
In `router.py::from_config`, detect `len(markets) > 1` and instantiate N copies, each scoped to one market. No code changes to existing strategies.
Pros: fixes the class of bugs globally; future strategies don't need to think about it. Cons: requires factoring strategy config builders to accept a single-market subset (minor).

**Recommendation: Option B.** It's the architectural fix, not a per-strategy workaround. The cost is a single change to the router and it eliminates an entire category of future bugs.

## Files to touch for Option B

- `src/strategies/router.py:81-110` — `from_config` factory: when the strategy's `markets` list has >1 entry, build N instances.
- `src/strategies/base.py` — BaseStrategy.from_config may need a `markets_override` kwarg.
- `tests/test_strategies/` — regression tests that prove per-symbol instances have independent state.
- `tests/test_router.py` (may not exist) — test that multi-market config yields N strategy instances.

## Files to touch for Option A (if Option B rejected)

- `src/strategies/momentum/vol_momentum.py` — convert `_closes` + `_position` + `_bars_*` to dicts keyed by symbol.
- `src/strategies/momentum/vol_momentum_gold.py` — same refactor (inherits).
- `src/strategies/trend_following/donchian_ensemble.py` — audit + likely the same fix.
- `src/strategies/day_trading/bb_rsi_mr.py` — audit (may not need if only uses current-bar features).
- Per-strategy tests.

## Files referenced

- `src/strategies/momentum/vol_momentum.py:84-98` — `_compute_momentum` (math is correct)
- `src/data/warmup.py:75-145` — warmup orchestration
- `src/data/downloader.py` — Binance historical downloader (suspect)
- `src/data/feature_engine.py` — feature pipeline that hands candles to strategies
- `data/historical/ADAUSDT_1h.parquet` — verified ends 2026-04-01, not stale enough to explain $1.25 alone
- `scripts/diagnose_vol_momentum_ada.py` — the diagnostic itself, reusable for any symbol
