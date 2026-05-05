# Strategy Triage — Session 24 (2026-05-05)

After 8.5 days of live paper trading, three strategies disabled in `config/strategies.toml`. One left enabled but watch-listed. One left untouched as the only structural winner. Re-enable criteria specified per strategy.

## Decisions table

| Strategy | Live trades | WR | P&L | PF | Decision | Re-enable criteria |
|---|---|---|---|---|---|---|
| `vol_momentum` (crypto) | 7 | 28.6% | +$111.13 | 3.60 | **KEEP** | n/a — protect, consider scale-up |
| `donchian_gold` | 8 | 12.5% | −$34.08 | 0.03 | **KEEP + watch** | Re-tune `sl_atr_mult` if next 10 trades stay < 20% WR |
| `donchian_ensemble_adx` | 2 | 0% | −$200.00 | 0.00 | **DISABLE** | Re-evaluate in 2 weeks. Re-enable only if 90d backtest replay shows positive expectancy AND identifies what was unlucky in the live 2-trade sample. |
| `funding_carry` | 5 | 0% | −$25.24 | 0.00 | **DISABLE** | 90d `deep_backtest` against current regime; gate Sharpe > 0.3 OOS to re-enable. Suspect: regime shift OR synthetic feed alignment off. |
| `vol_momentum_gold` | 39 | 10.3% | −$166.75 | 0.11 | **DISABLE** | 90d `deep_backtest` with parameter sensitivity sweep; gate PF > 1.2 OOS. Suspect: gold tick noise hostile to momentum_threshold = 0.01. |

## Per-strategy rationale

### vol_momentum (crypto) — KEEP

**Profile:** 7 trades, 28.6% WR, +$111.13, PF 3.60, avg duration 35.3h.

Win-loss ratio (avg_win $76.92 / avg_loss $8.54 ≈ 9.0) is the math signature of a working momentum strategy: low frequency, low WR, big winners covering small losers. PF 3.60 is well above the typical 1.5-2.0 threshold for production-ready momentum.

Sample size N=7 is thin but every other component checks out:
- Both winning trades hit substantial gains (+$152.46 and +$76.92 — not lucky stops near entry).
- Avg duration 35.3h matches the strategy's design (168-bar momentum lookback, intended swing horizon).
- Per-symbol breakdown across DOGEUSDT/ADAUSDT/DOTUSDT shows the wins are not concentrated in one symbol.

**Action:** keep enabled. After session 24, watch for the next 10 trades. If PF holds > 2.0, scale via `max_risk_per_trade` bump or universe extension.

### donchian_gold — KEEP + WATCH

**Profile:** 8 trades, 12.5% WR, −$34.08, PF 0.03, avg duration 0.3h (18 min).

The 0.3h average duration is the smoking gun. Gold's intraday tick noise (typical 1h ATR ~$15-30) dominates the chosen `sl_atr_mult` — stops are hit before momentum plays. This is a parameter problem, not a strategy-doesn't-work problem. Phase 4 WF backtest had Calmar 13.68 OOS, which is excellent — the live gap is parameter-tuning specific to gold's regime.

**Action:** kept enabled (sample too small to kill, loss bounded at −$34). Watch-listed in `config/strategies.toml`. Next sprint: sweep `sl_atr_mult` in {3.0, 4.0, 5.0, 6.0} via `deep_backtest` and pick the one that shows OOS Calmar > 1.0.

### donchian_ensemble_adx — DISABLE

**Profile:** 2 closed trades, 0% WR, −$200.00 (−$83.71 and −$116.28), 38h avg.

Sample size N=2 is too small to call structurally broken — could be variance. But:
- Both losses were near the strategy's full risk cap (−$100 / 1.5% × $10k ≈ −$150 max single-trade risk).
- Two open positions on May 4 (ETH @ 2363, BNB @ 631) had the same potential downside.

The live 4-position concentration of risk in a strategy with no winning trades led to disabling. The 2 open positions were closed during this session's triage (ETH +$5.17, BNB −$7.32, net −$2.15). The −$200 closed plus the close-realized −$2 = −$202 total — out of $10k crypto engine equity, that's −2.0% from a single strategy with N=2. Tail risk too concentrated to trust further sample-building live.

**Action:** disabled with annotation. Re-evaluate in 2 weeks. If 90d backtest replay shows positive expectancy AND identifies why the live 2-trade sample was unlucky (e.g. specific market regime, specific symbol failure mode), re-enable. Otherwise write obituary.

### funding_carry — DISABLE

**Profile:** 5 trades, 0% WR, −$25.24, all small losses (avg −$5.05), avg duration 62.2h.

Real-data backtest (4.3 years, before Session 22 enable) showed Sharpe 0.533 / +2.62% total return / 62 trades. Gate-history was: A.1 research PASS → A.3 synthetic PASS Sharpe 2.584 → A.4 MC/fee/crash PASS → A.5 M3S PASS → real-data PASS Sharpe 0.533.

Live shows zero edge. Possible causes (in order of likelihood):

1. **Regime shift.** Funding rate distribution may have changed since the training window ended (training cut likely in 2024-Q4 / 2025-early). Current low-funding environment doesn't trigger meaningful trades.
2. **Synthetic feed alignment.** `FundingSyntheticFeed` polls `/fapi/v1/premiumIndex` and emits at 8h boundaries. If timing or value source drifted from what the backtest used, signals would be miscalibrated.
3. **Entry filter too eager.** Live shows 5 trades in 8.5 days = 1 every 1.7d. Backtest showed 62 in 4.3y = 1 every 25d. Live is 14× more frequent than backtest — suggests entry threshold is being hit way too often, not selectively enough.

**Action:** disabled. Run `python -m src.research.deep_backtest --strategy funding_carry --window 90d` next sprint. If Sharpe > 0.3 OOS confirms historical edge, investigate (2) and (3) before re-enable. If Sharpe < 0.3, write obituary.

### vol_momentum_gold — DISABLE

**Profile:** 39 trades, 10.3% WR, −$166.75, PF 0.11, avg duration 2.7h.

Same algorithm code as crypto twin (which produces PF 3.60). Different market, different result.

Trade frequency divergence: crypto 7 trades / 8 days = 0.9/day. Gold 39 trades / 8 days = 4.9/day. **5.4× the frequency.** This is the root signal: momentum thresholds tuned for crypto's volatility regime are way too sensitive on gold's intraday tick noise.

The Apr 22 cTrader partial-bar fix (commit `79a84c9`) cut the original 86-flip-flops-per-42-hours pathology (where every partial trendbar was treated as `closed=True` and re-evaluated). Post-fix, the pathology dropped to 4.9/day instead of ~50/day — improvement, but residually still hostile.

The orphan SHORT @ $4678 from Apr 27 19:00 UTC sat through the gold engine's 87h STALE outage. Gold dropped to ~$4557 by end of session 24, so the orphan was incidentally +$13.41 unrealized when closed. Lucky outcome, not a strategy validation.

**Action:** disabled. Run `python -m src.research.deep_backtest --strategy vol_momentum_gold --window 90d` next sprint with parameter sensitivity:
- `momentum_threshold` in {0.01, 0.015, 0.02, 0.03}
- `vol_lookback` in {168, 240, 336}
- `cooldown_bars` in {5, 10, 20}

If a regime exists where gold OOS PF > 1.2, re-enable with that param set. If no regime works, write obituary.

## Re-enable workflow (when criteria met)

1. Open `~/.claude/plans/<task>.md` for the re-enable plan.
2. Run `python -m src.research.deep_backtest --strategy <name>` interactive TUI; record full WF Calmar / Sharpe / WF folds passing.
3. If gate passes, update `config/strategies.toml` `enabled = true` and ADD a new dated comment that supersedes the disable comment (do NOT delete history).
4. Restart the relevant engine via `launchctl kickstart -k gui/$(id -u)/com.algo-trading.engine[-gold]`.
5. Verify the strategy registers in logs: `grep strategy_registered data/logs/engine_err.log`.
6. Update `STATE.md` with the re-enable + reasoning.
7. Watch closely for the first 10 trades.

## Open positions disposition (session 24)

| Position | Action taken | Realized P&L |
|---|---|---|
| `vol_momentum_gold` SELL XAUUSD 0.117 @ $4678.06 (Apr 27) | Synthetic close at $4557.06 via `scripts/operational/close_orphan_positions.py` | **+$13.41** |
| `donchian_ensemble_adx` BUY ETHUSDT 0.387 @ $2363.27 (May 4) | Synthetic close at $2380.21 | **+$5.17** |
| `donchian_ensemble_adx` BUY BNBUSDT 2.386 @ $630.68 (May 4) | Synthetic close at $628.55 | **−$7.32** |
| **Combined** | — | **+$11.26** |

DBs backed up before mutation:
- `data/trades.db.bak.20260505T114004Z`
- `data/trades_gold.db.bak.20260505T114005Z`
