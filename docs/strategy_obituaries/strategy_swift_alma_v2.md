# 🪦 swift_alma_v2 — RESEARCH_ONLY across all 5 leverage modes

**Killed:** 2026-04-18 (Session 23 Day 1, task #116 verdict)
**Category:** PREMISE (signal generator's edge does not survive OOS walk-forward)
**Replaces / depends on:** parent `swift_alma` (task #103, Pine-ported), upgraded in tasks #131 + #132

## The claim

`swift_alma_v2` was an upgrade of the parent `swift_alma` designed to rescue the cost-killed Pine port (-12.47% on IC Markets MT4 fees). The v2 added 8 research-justified layers on top of the plain ALMA crossover:

1. Decoupled `risk_pct` / `sl_pct` so sizing is independent of stop width
2. ADX ≥ 22 regime filter (avoid chop)
3. 4h EMA 50 HTF trend confirmation (only trade WITH higher TF)
4. London + NY session filter (07:00–11:00 and 13:30–16:30 UTC)
5. 3-bar cooldown after an exit
6. Strategy-internal mode-aware `_effective_risk_pct()` that adapts to the engine-declared leverage mode (5 modes)
7. ATR-scaled SL / TP
8. Vol-target sizing layer

In-sample matrix on 1y × 1h × L=15 × MT4 fees showed all 5 modes net-positive, with MARGIN_CAPPED topping at +7.81%. Task #131 landed the strategy into production backlog as Stage-5 optimization candidate.

## What killed it

Task #116 walk-forward verdict sweep (`scripts/run_swift_alma_v2_task116.sh` 2026-04-18):
- **Config**: 1h × [90, 365] days × L=10 × MT4 fees × 7-fold WF (gate Calmar ≥ 0.5, 3/6 profitable fold requirement) across all 5 leverage modes.
- **Result**:

| Mode | Verdict | Continuous Calmar | Per-fold mean | Ann return | Notes |
|---|---|---:|---:|---:|---|
| invariant | RESEARCH_ONLY | −0.221 | −0.459 | −2.2%/yr | Loses outright |
| margin_capped | RESEARCH_ONLY | −0.217 | +0.072 | −0.2%/yr | Per-fold mean flips sign of continuous Calmar |
| vol_targeted | RESEARCH_ONLY | −0.229 | +0.108 | — | Vol-targeted gate Calmar ≥ 1.0 (higher signal-edge requirement) |
| risk_scaled | FAILED | — | — | — | Phase 2.5 P&L-scaling assertion fail |
| kelly_fractional | RESEARCH_ONLY | −0.000 | +0.000 | 0.0%/yr | Half-Kelly (WR=0.40, PR=1.5): trades but makes nothing |

**Zero modes clear the deployable gate.** The in-sample +7.81% on MARGIN_CAPPED that made v2 look promising did not survive 7-fold walk-forward; the per-fold mean is +0.072 (barely positive) but continuous Calmar is −0.217, meaning compounding and non-overlapping fold boundaries erase the edge.

## Root cause (structural, not fixable by parameter tuning)

- The parent `swift_alma` Pine port was commission-killed on real MT4 fees (-12.47% on 1y).
- v2's 8-layer filter stack rescued the in-sample window enough to show +7.81% on MARGIN_CAPPED — but all 8 filters OVERFIT the validation period.
- Across rolling 90-day OOS folds, the filter combinations fire too rarely (ADX ≥ 22 + session + HTF + cooldown + regime) to generate consistent trade counts, and the trades that DO fire net out to zero once spread + commission are taken.
- ALMA crossover itself is a hypergeneric signal with no persistent edge in any published literature; v2's 8 filters try to compensate but each additional filter is an extra degree of freedom for overfitting.

## Revival conditions

1. A completely different signal core (NOT ALMA crossover). The filter/regime/session layers from v2 might still be reusable for a strategy with real edge at the core.
2. Tick-level data + M1 intrabar resolution (G.5b `M1PathModel`) — current WF is M5/H1 bar-level, missing genuine scalping edge.
3. Regime-specific entry logic (different signal in trending vs mean-reverting regimes, detected by ADX or similar) rather than one-size ALMA crossover.

## Do NOT re-attempt without

- New signal architecture with prior external validation (published paper, not just in-sample tuning)
- Task #116b research cycle demonstrating out-of-sample positive Calmar BEFORE any production wiring

## Files

- Strategy code: `src/strategies/trend_following/swift_alma_v2.py` (keep — the regime/session/HTF filter utilities are reusable)
- Task #116 reports: `reports/deep_backtest_swift_alma_v2_task116/{invariant,margin_capped,vol_targeted,risk_scaled,kelly_fractional}/summary.json`
- Parent obituary context: `docs/BROKER_FEES.md` (MT4 fee calibration)
- Graveyard row: pending ingestion via `scripts/ingest_obituaries.py`
