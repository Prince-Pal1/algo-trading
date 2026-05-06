# OOS Holdout Lock — 2026-05-06

**Status: PRE-COMMITTED. This file commits BEFORE any Phase 2-6 sweep result file. The git diff is the bias-prevention proof.**

## Why this exists

Strategy parameters in our codebase were tuned with implicit knowledge of recent gold/crypto price action. A 90-day backtest replay window that overlaps the tune epoch is contaminated — the validation isn't independent of the choice. Without a strict OOS holdout pre-committed before sweeps run, the PF > 1.2 / Sharpe > 0.3 verdict gates are meaningless.

This document locks the tune and holdout windows for the three Workstream-A backtest replays so that:

1. The holdout window is OOS to the strategy's original tune epoch (no time-leak).
2. The choice of window cannot be retrofitted to make a strategy "pass" — the windows are committed before any results land.
3. A reviewer (future Prince, future me, or anyone reading the audit trail) can replay the experiment from this lock and reproduce the verdict.

## Hidden constraint surfaced 2026-05-06

The XAUUSD parquet at `data/historical/XAUUSD_1h.parquet` ends at **2026-04-12 16:00 UTC**, but the strategies' tune epoch ends **2026-04-13** (commit `a7f0f11`, 2026-04-14 04:31 +05:30 IST). The first ever OOS bar for vol_momentum_gold and donchian_gold lives in data we don't have on disk yet.

**Therefore, before any A1/A3 sweep runs, the parquet must be backfilled** through at least 2026-05-06. Procedure:

```bash
python scripts/download_xauusd.py --timeframes 1h 5m --years 2.0
# This re-downloads the full 2-year window from Dukascopy.
# Verify the new parquet covers through 2026-05-06:
python3 -c "import pandas as pd; df = pd.read_parquet('data/historical/XAUUSD_1h.parquet'); print('last:', pd.Timestamp(df.timestamp.max(), unit='ms', tz='UTC'))"
```

Recommended: run backfill TODAY as Phase 1.0, immediately before the holdout-wrapper dev work.

## Windows per strategy

### vol_momentum_gold (Workstream A1)

| Field | Value |
|---|---|
| Last tune commit | `a7f0f11` (2026-04-14 04:31 IST = 2026-04-13 23:01 UTC) |
| Tune epoch ends | 2026-04-13 23:00 UTC |
| Tune window | 2024-05-06 → 2025-12-31 (~20.5 months) |
| Gap (held aside, not used) | 2026-01-01 → 2026-04-13 (~3.5 months — the original tune saw this; we exclude it from our re-tune to maximize OOS purity) |
| Holdout window | 2026-04-15 → 2026-05-06 (22 days, all post-tune-epoch live data) |
| Data source | `data/historical/XAUUSD_1h.parquet` (post-backfill) |
| Fee profile | `ic_markets_ctrader_xauusd_normal` |
| Verdict gates | Tune WF Calmar > 1.0 (5 folds) AND Holdout PF > 1.2 |
| Param grid | `momentum_threshold ∈ {1.5, 2.0, 2.5, 3.0}` × `vol_lookback ∈ {10, 20, 30}` × `cooldown_bars ∈ {0, 4, 8, 16}` = 48 cells |

**Rationale for the gap:** The original tune at `a7f0f11` used data through 2026-04-13. Excluding 2026-01-01 → 2026-04-13 from our re-tune means our chosen params don't benefit from the same 3.5-month window the original tuner had. The 22-day holdout 2026-04-15 → 2026-05-06 is then strictly OOS to BOTH the original tune AND our re-tune — the cleanest experimental design available without further data.

**Trade-off accepted:** 22 days is statistically thin. Power on holdout PF estimate is limited. We accept noisier verdict to maintain momentum; if any verdict is borderline (holdout PF in [0.9, 1.4] band), wait one more week and re-validate on extended holdout.

### donchian_gold (Workstream A3)

| Field | Value |
|---|---|
| Last tune commit | Same Phase G epoch (`a7f0f11` and predecessors) |
| Tune epoch ends | 2026-04-13 23:00 UTC |
| Tune window | 2024-05-06 → 2025-12-31 (~20.5 months) |
| Gap (held aside) | 2026-01-01 → 2026-04-13 (~3.5 months) |
| Holdout window | 2026-04-15 → 2026-05-05 (21 days) |
| Data source | `data/historical/XAUUSD_1h.parquet` (post-backfill) |
| Fee profile | `ic_markets_ctrader_xauusd_normal` |
| Verdict gates | Tune WF Calmar > 1.0 (5 folds) AND Holdout Calmar > 1.0 |
| Param grid | `sl_atr_mult ∈ {3.0, 4.0, 5.0, 6.0}` × `tp_atr_mult ∈ {3.0, 5.0, 7.0}` = 12 cells |

Shared tune epoch with vol_momentum_gold; same windows.

### funding_carry (Workstream A2)

| Field | Value |
|---|---|
| Last tune | Stage 7 WF backtest pre-Session 22 enable (commit history of `src/strategies/swing/funding_carry.py` predates the live deploy) |
| Tune epoch ends | 2026-04-08 (last commit before live enable on 2026-04-09) |
| Tune window | 2022-01-01 → 2025-12-31 (~4 years) |
| Gap (held aside) | 2026-01-01 → 2026-04-08 (~3.3 months) |
| Holdout window | 2026-04-10 → 2026-05-05 (26 days) |
| Data source | `data/historical/funding_*.parquet` (assume Binance funding-rate history; verify path exists before A2 runs) |
| Fee profile | `binance_perpetual_futures` (per memory `feedback_select_fee_profile_first` and the strategy's research notes; spot fees would invalidate edge) |
| Verdict gates | Tune WF Sharpe > 0.5 (5 folds) AND Holdout Sharpe > 0.3 |
| Param grid | Default params; we're checking "does the published edge still exist," not retuning |

**A2-specific prerequisite:** Run `scripts/research/funding_carry_signal_divergence.py` BEFORE the backtest replay. If the live-vs-bt diagnostic shows the strategy code disagrees with live decisions on any of the 5 historical fills, halt A2 and pull engine investigation forward (likely feed bug in `FundingSyntheticFeed`).

**Open question:** the funding-rate history parquet path is not yet verified. The wrapper (Phase 1.2) MUST refuse to launch A2 if the data file is missing or doesn't cover both windows. If missing, run `scripts/download_funding_history.py` first.

## What is NOT changing

- **vol_momentum (crypto) is not in this lock.** It's the only winner; no kill verdict needed. Workstream B (universe expansion in Phase 5.1) will get its own lock document next week.
- **funding_mean_reversion (crypto, BTCUSDT 8h) is not in this lock.** Currently in shadow mode; not a triage candidate.

## Verdict workflow

For each strategy, the deep_backtest_with_holdout wrapper (Phase 1.2) will:

1. Run `deep_backtest --strategy <X>` on tune window with WF retune + param grid → produce per-fold OOS metrics.
2. Pick best params per the WF gate.
3. Re-run `deep_backtest --strategy <X> --strategy-params <chosen> --no-wf` on holdout window only.
4. Print combined verdict report: tune WF metrics, holdout metrics, gate status (PASS/FAIL/BORDERLINE).

**Decision rules (HFM hard rules):**

- Both gates pass → re-enable in `config/strategies.toml` with the chosen params + dated comment; one-week trial watch window.
- Tune passes, holdout fails → kill (tune was a fluke; OOS truth wins). Write obituary, ingest to graveyard.
- Tune fails → kill (no edge even with retune). Write obituary, ingest to graveyard.
- Borderline (holdout metric within 10% of gate) → wait one week; re-validate on extended holdout. Do NOT re-enable on the borderline result.

**HFM anti-temptation rule:** ONE sweep per strategy. If the sweep is null, don't iterate "one more thing." Move on.

## Audit trail commitment

This file's commit hash is the start of the audit trail. Subsequent files in `reports/a*/` and `docs/research/2026-05-XX_*.md` chain to this commit — verifiable that no window was retrofitted post-result.

```
git log --oneline docs/research/2026-05-06_oos_holdout_lock.md
# Should show ONE commit dated 2026-05-06, AT OR BEFORE the first commit landing any sweep result file.
```

If this lock is ever modified, the modification commit must include a written justification and an explicit re-baselining of any in-flight verdicts. **Do not silently amend.**

## Sign-off

Lock author: Claude Opus 4.7 (via Prince Pal, sole signing authority).
Lock date: 2026-05-06.

**Amendment 1 — 2026-05-06 (same day, before any sweep result):**
- Tune-window start adjusted from 2024-04-15 → 2024-05-06 to match the actual XAUUSD parquet data start (2-year Dukascopy download via `scripts/download_xauusd.py --years 2.0`).
- Holdout-window end adjusted from 2026-05-06 → 2026-05-05. The post-backfill parquet's max timestamp is 2026-05-06 00:00 UTC, which represents the end-of-day-2026-05-05 bar. Using 2026-05-05 in the wrapper's CLI accurately captures all bars through that day without an off-by-one.
- The no-overlap invariant holds (tune ends 2025-12-31, holdout starts 2026-04-15). No sweep result file existed at the time of this amendment; the audit-trail anchor (`ffa5725`) remains valid.

**Amendment 2 — 2026-05-06 (same day, before any committed sweep result):**

The HFM-recommended A1 param grid `momentum_threshold ∈ {1.5, 2.0, 2.5, 3.0}` was found to use units mismatched to the strategy implementation. `VolMomentumGoldStrategy._compute_momentum()` returns `log(close_now / close_past)` over `momentum_window` bars (default 240 hours). For XAUUSD this typically falls in [-0.05, 0.05] (±5% over 10 days). The HFM's values 1.5-3.0 would require log returns of 150%-300% over the window — physically impossible for normal market conditions. A trial run with the original grid produced 0 trades per cell on the holdout window and an empty `best_params` extraction.

**Corrected grid (binding):**
- `momentum_threshold ∈ {0.0, 0.005, 0.01, 0.02}` = 4 values (no-gate to 2% threshold over the 10-day momentum window)
- `vol_lookback ∈ {168, 240, 336}` = 3 values (7-14 day lookback; HFM's {10, 20, 30} would be ~hours not days, far too short for 1h bars)
- `cooldown_bars ∈ {0, 5, 24}` = 3 values
- = **36 cells** (down from 48)

This correction was applied BEFORE any committed sweep result file. The trial run with the original grid produced no actionable verdict (infrastructure null, not strategy verdict) and its draft outputs in `reports/a1_vol_momentum_gold_2026-05-07/` (gitignored) are discarded. The HFM anti-temptation rule (ONE sweep, no iteration) applies to the CORRECTED grid: one run, one decision.

This amendment is itself committed BEFORE the corrected sweep is launched. If the corrected sweep is null, write the obituary; do NOT iterate further.
Source plan: `~/.claude/plans/lets-first-make-a-quirky-hartmanis.md` (super plan, approved 2026-05-06).
HFM memo (consulted): `~/.claude/plans/lets-first-make-a-quirky-hartmanis-agent-ad88756c0e4ce744f.md`.
