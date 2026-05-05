# Portfolio Postmortem — Session 24 (2026-05-05)

**Window:** 2026-04-26 (paper-trading start) → 2026-05-05 (Session 24).
**Source:** `scripts/generate_portfolio_report.py` + direct SQLite queries against `data/trades.db` and `data/trades_gold.db`.

## Headline

Combined portfolio: **$19,695.19** from $20,000 initial → **−$304.81 / −1.52%** over 8.5 days live.
- Crypto engine: $9,883.16 (−1.17%)
- Gold engine: $9,812.03 (−1.88%)
- 61 closed round-trips before this session's triage close. 3 orphaned positions closed during this session for net realized **+$11.26** (XAUUSD orphan +$13.41, ETH +$5.17, BNB −$7.32).

## Per-strategy performance

| Strategy | Engine | Trades | WR | P&L | PF | Avg dur | Verdict |
|---|---|---|---|---|---|---|---|
| **vol_momentum** | crypto | 7 | 28.6% | **+$111.13** | **3.60** | 35.3h | ✅ ONLY WINNER |
| funding_carry | crypto | 5 | 0.0% | −$25.24 | 0.00 | 62.2h | 💀 No edge captured |
| donchian_gold | gold | 8 | 12.5% | −$34.08 | 0.03 | 0.3h | ⚠️ Stops too tight |
| vol_momentum_gold | gold | 39 | 10.3% | **−$166.75** | 0.11 | 2.7h | 💀 Over-trading on gold |
| donchian_ensemble_adx | crypto | 2 | 0.0% | **−$200.00** | 0.00 | 38h | 💀 −$100/trade |

**Bleeders outweigh the winner 4×:** the only profitable strategy (`vol_momentum` crypto) generated +$111. The four losing strategies generated −$426. Combined gross −$315 plus close-realized +$11 ≈ −$304 final.

## Why poor results — root cause analysis

### 1. Operational silence — gold engine STALE 87 hours undetected

`data/heartbeat_gold.json` showed `last_candle_age_s = 314,145` — last candle 2026-05-01 20:00 UTC, 87 hours before this session began. Investigation (this session, Phase 1) found:

- `src/data/feeds/icmarkets_feed.py:218-227` sends `ProtoOASubscribeLiveTrendbarReq` for each (symbol × timeframe).
- The corresponding ack message `ProtoOASubscribeLiveTrendbarRes` had **no handler** in `_on_message_received` (lines 136-242). Spot subscribes acked at line 230-232; trendbar acks were silently discarded by the protobuf dispatcher.
- `_send_quiet()` at line 244-261 attaches an errback at `log.debug` level only. If Spotware silently dropped the trendbar subscribe (likely on the post-weekend reconnect Sun 22:27 UTC), the engine never knew its candle stream had failed.
- The watchdog (`scripts/watchdog.py`) only checked `data/heartbeat.json` (crypto), not `data/heartbeat_gold.json`. Even if the watchdog HAD checked gold, the previous PID-file kill mechanism was broken (no PID file ever written by the engine — `data/pids/` empty since project start), so no restart would have fired.

**Impact:** vol_momentum_gold opened a SHORT XAUUSD 0.1173 @ $4678.06 on Apr 27 19:00 UTC. The strategy never received a candle to evaluate exit conditions for 8 days. The position was effectively orphaned through the entire weekend gap into this session. (Lucky outcome: gold dropped to ~$4557, so the orphan was +$13.41 unrealized when closed.)

### 2. Strategy mix concentration — 4 of 5 live strategies losing

Going into the live phase, six strategies were enabled (post Session 23 triage). Live results show one structural winner and four structural losers:

- **vol_momentum (crypto)**: PF 3.60 over 7 trades. Profile is textbook for a momentum strategy: low frequency, low WR (28.6%), but big wins ($76.92 avg) covering small losses ($8.54 avg). Working as designed.
- **vol_momentum_gold**: SAME algorithm code as the crypto twin, applied to XAUUSD. Generated 5.6× the trade frequency (39 vs 7) on hostile-to-momentum gold tick noise. Net loss $167. The Apr 22 cTrader partial-bar fix (commit `79a84c9`) cut the original 86-flip-flops-per-42-hours pathology, but residual over-trading suggests gold's intraday volatility profile is fundamentally hostile to this algorithm without a regime filter or different parameter set.
- **funding_carry**: 5 trades, ALL losing. WF backtest passed Sharpe 0.533 with 62 trades over 4.3 years. Live shows zero edge. Either the regime has shifted (BTC funding rates structurally different from training data), the synthetic feed (`FundingSyntheticFeed`) is misaligned with what the backtest measured, or the entry filter is too eager. No live evidence of the backtested edge.
- **donchian_ensemble_adx**: only 2 closed trades, both ~−$100. Profile concerning — single-trade losses are 8× the avg vol_momentum loss. With 2 currently-open positions on May 4, tail risk was concentrated.
- **donchian_gold**: 8 trades, 12.5% WR, avg duration 0.3h (18 min). The strategy's stop is being hit before momentum plays — gold's intraday tick noise dwarfs the chosen `sl_atr_mult`. Solvable with parameter tuning, not a structural failure.

### 3. Equal sizing across strategies regardless of conviction or PF

`max_risk_per_trade` ranges 1.0%-1.5% across strategies. The winner (vol_momentum) is sized identically to the bleeders. M3S allocator (which would route capital based on rolling Sharpe) is not yet wired into live position sizing. Net: the winner cannot compound; the bleeders take equal-sized losses.

### 4. STATE.md / ROADMAP.md 17 days stale

Last STATE update was 2026-04-18 (G.3 Day 1 launch). Today is 2026-05-05. 17 days of live operation, strategy outcomes, partial fixes (Apr 22 partial-bar), and accumulating drift were undocumented. Doc cadence rule (CLAUDE.md Rule 1, "update before saying done") was not enforced during the live observation phase.

## Concrete improvements (this session shipped)

| Change | File | Effect |
|---|---|---|
| Add `ProtoOASubscribeLiveTrendbarRes` handler | `src/data/feeds/icmarkets_feed.py` (around line 232) | Spotware's trendbar ack now logs `icmarkets_trendbar_subscribed`. Operator can see ack count match send count. |
| Add `_send_loud()` helper for trendbar subscribes | same file (after `_send_quiet`) | Trendbar errback now logs at WARNING with a label. Future silent fails will alert. |
| Add 30s pending-subscribe audit | same file (after spot ack handler) | If acks don't arrive within 30s, logs `icmarkets_trendbar_subscribe_unacked` at WARNING. Ops sees explicit "engine running tick-only" alert. |
| Watchdog v2 (multi-engine, kickstart-based) | `scripts/watchdog.py` | Checks BOTH `heartbeat.json` and `heartbeat_gold.json`. Bumped candle-stale threshold from 1800s → 7200s (1h candles need 2× grace). Replaced PID-file kill (broken — never wrote PID) with `launchctl kickstart -k`. macOS notification on STALE. |
| Disable `vol_momentum_gold` | `config/strategies.toml` | No new gold-side momentum signals until backtest replay finds parameter regime that explains the crypto/gold divergence. |
| Disable `funding_carry` | `config/strategies.toml` | No new carry signals until 90d backtest replay confirms or refutes regime drift. |
| Disable `donchian_ensemble_adx` | `config/strategies.toml` | No new signals; sample (2 trades) too small to draw conclusions but per-trade loss profile concerning. |
| Mark `donchian_gold` for stop re-tune | `config/strategies.toml` (comment only) | Watch-list, not killed — sample size 8, fix is a single param. |
| Close 3 orphan positions | `scripts/operational/close_orphan_positions.py` | Synthetic close with backups. Net realized +$11.26. State reset clean. |

## Next-sprint improvements (not in this session)

| Lever | Action | Gate |
|---|---|---|
| **Heal `vol_momentum_gold`** | `python -m src.research.deep_backtest --strategy vol_momentum_gold --window 90d` interactive TUI. Compare param sensitivity vs crypto twin. Either find a gold-specific param set with PF > 1.2 or write obituary. | PF > 1.2 OOS to re-enable |
| **Heal `funding_carry`** | Same: 90d backtest replay against current regime. If WF still passes, check synthetic feed alignment. If WF fails, write obituary. | Sharpe > 0.3 OOS to re-enable |
| **Re-tune `donchian_gold` stops** | Sweep `sl_atr_mult` in {3.0, 4.0, 5.0, 6.0} via `deep_backtest`. Hold all other params fixed. | Calmar > 1.0 OOS at chosen mult |
| **`donchian_ensemble_adx` audit** | After 2 weeks, evaluate if the kill was justified or if the 2-trade sample was unlucky variance. Re-enable only with clear backtest support. | Active research after data accumulates |
| **Scale `vol_momentum`** | Two paths: (a) bump `max_risk_per_trade` from 0.012 → 0.018-0.020. (b) Extend `markets` from `[DOGE, ADA, DOT]` to add `[ETH, BNB, APT]` (test with backtest first). | (a) PF > 2.0 holding through scale-up. (b) PF > 1.5 on extended universe in backtest. |
| **Wire M3S allocator into live sizing** | Currently runs in shadow. Activate the per-strategy capital allocator so `vol_momentum` gets > 1× risk and bleeders get capped. | M3S phase ramp gate |
| **Enforce doc cadence** | STATE.md / ROADMAP.md updated after every session. CLAUDE.md Rule 1 already codified. Just enforce. | Manual discipline |

## What NOT to do

- **Don't tune live.** Any parameter change goes through `deep_backtest` first. Live numbers are too noisy with N=2-39 trades to inform tuning.
- **Don't widen `vol_momentum_gold` stops and re-enable** without understanding the crypto/gold divergence root cause. Bigger stops on a strategy that fundamentally doesn't fit a market = bigger losses.
- **Don't kill `vol_momentum` (crypto) because total portfolio is red.** PF 3.60 over 7 trades is sample-size-thin but the per-trade profile (avg win $76.92, avg loss $8.54, win-loss ratio 9×) is the math signature of a working momentum strategy.
- **Don't add new strategies until allocator is wired.** Adding more strategies into equal-sized buckets dilutes the winner further.

## References

- `scripts/generate_portfolio_report.py` — produced the analysis driving this postmortem
- `scripts/operational/close_orphan_positions.py` — synthetic close mechanism
- `docs/investigations/2026-04-17_vol_momentum_stale_buffer.md` — prior investigation pattern
- Plan file: `~/.claude/plans/lets-first-make-a-quirky-hartmanis.md` (Session 24 plan)
- Backups: `data/trades.db.bak.20260505T114004Z`, `data/trades_gold.db.bak.20260505T114005Z`
