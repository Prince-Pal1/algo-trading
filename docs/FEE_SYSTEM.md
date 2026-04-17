# Fee System

Unified broker-aware, scenario-aware, style-aware cost model. Every part of the system (strategies, backtest engine, RiskManager, dashboard, cost-attribution writer) queries a single source of truth.

Built in Session 22 (2026-04-18). Supersedes the ad-hoc `make_fee_model("name")` pattern in `src.backtest.fee_profiles` — that registry is still authoritative and still works, but new callers use `FeeManager`.

## Two-axis cost model

Costs are keyed by **(broker × instrument_class × scenario × strategy-style)**.

### Strategy declares style (one of 5)

| Style | Typical hold | Commission-dominant? | Swap-aware? | Examples |
|---|---|---|---|---|
| `scalping` | ≤15 min | yes | no | ema_crossover, candle_burst_hunter, hedged_structure_play |
| `intraday` | same-day | balanced | no | bb_rsi_mr, rsi2_mr, funding_mean_reversion |
| `swing` | 1–7 days | balanced | yes | donchian_ensemble, vol_momentum, swift_alma, donchian_gold, vol_momentum_gold |
| `position` | weeks–months | low | yes (dominant) | clenow_momentum, funding_carry |
| `arbitrage` | paired, same-session | balanced | no | btc_neutral_mr |

Declared at class level:
```python
class DonchianEnsembleStrategy(BaseStrategy):
    fee_style = "swing"
```

### System detects scenario (one of 4, auto)

Precedence: `news_active > volatile > illiquid > normal`

| Scenario | Trigger | Effect on spread |
|---|---|---|
| `news_active` | within ±15 min of an event in `config/news_calendar.csv` | × `news_spread_mult` (typically 5×) |
| `volatile` | current_ATR > 1.5 × rolling 30-bar ATR | × 2.0 (migration default) |
| `illiquid` | off-session (e.g. XAUUSD 00:00-07:00 UTC, 21:00-24:00 UTC) | × 3.5 (migration default) |
| `normal` | everything else | base (from research calibration) |

Scenarios are derived from TOML during migration but expected to be refined empirically using the weekly spread sampler (see `scripts/ctrader_spread_weekly.sh`).

## API

```python
from src.fees import FeeManager, cost_for_signal, cost_for_strategy_signal

# Resolve a FeeModel for the active broker + auto-scenario
fees = FeeManager.resolve(symbol="XAUUSD", style="swing", timestamp_ms=now_ms)

# Pre-trade cost projection
cost = FeeManager.project_cost(
    symbol="XAUUSD", qty_lots=1.0, style="swing",
    hold_hours=48.0, mid_price=4865.0,
)
# → CostProjection(spread=X, commission=Y, swap=Z, total=X+Y+Z,
#                  scenario="normal", profile_name="...", broker_id="...")

# Signal-level one-liner
cp = cost_for_strategy_signal(strategy, signal)

# Introspection (for dashboard / logs)
info = FeeManager.explain(symbol="XAUUSD", style="swing")
# → {'broker_id': ..., 'profile_name': ..., 'scenario': 'normal', ...}
```

## File layout

```
config/
    active_broker.toml              # single source of truth for "current broker"
    brokers/<id>.toml               # per-broker spec (profiles by instrument × scenario)
    broker_fees.toml                # legacy flat registry (still authoritative for src.backtest.fee_profiles)
    news_calendar.csv               # event windows used by scenario detector
src/fees/
    __init__.py                     # public API surface
    broker.py                       # Broker + BrokerInstrumentProfile + loader
    active.py                       # active-broker pointer (read/write)
    scenario.py                     # auto-detector (news / volatile / illiquid)
    manager.py                      # FeeManager.resolve() + project_cost() + explain()
    helpers.py                      # signal-level shortcuts
    attribution.py                  # per-trade cost attribution side-table writer
docs/FEE_SYSTEM.md                  # this file
scripts/
    migrate_broker_fees.py          # regenerate config/brokers/*.toml from broker_fees.toml
    set_active_broker.py            # CLI: switch default broker + per-class overrides
    ctrader_spread_sampler.py       # live tick capture
    ctrader_spread_report.py        # bucketed spread analysis → calibration report
    ctrader_spread_weekly.sh        # launchd wrapper: sampler → analyzer
tests/test_fees/                    # 103 tests across Phases A–E
```

## Adding a new broker

1. Create `config/brokers/<id>.toml` (copy `ic_markets_ctrader.toml` as template)
2. Fill the `[broker]` metadata block + `[leverage_max]` + `[instruments.<class>]` + `[instruments.<class>.scenarios.<scen>]` tables for `normal` (minimum) and optionally `news_active`, `illiquid`, `volatile`
3. Add operational metadata (`BROKER_METADATA` entry) in `scripts/migrate_broker_fees.py` if you want migration-script support
4. `python3 scripts/set_active_broker.py --list` should now show it
5. Run `python3 -m pytest tests/test_fees/` to confirm the new profile loads cleanly

## Live-calibrating scenarios

The weekly Monday sampler (`launchd` job `com.algo-trading.spread-sampler`) captures 4h of XAUUSD ticks through the London/NY peak overlap, then the analyzer buckets them by trading session and recommends a `base_spread_pips` update for the active profile. Apply the recommendation manually by editing the `[scenarios.normal.spread]` table, then re-run the strategy walk-forward to confirm the new cost assumption doesn't change the deployable-bar verdict.

Live calibration drift is expected. The `trade_cost_attribution` side-table records which profile each closed trade used, so post-hoc analysis can detect when a strategy's realized cost drifts >10% from its backtest-assumed cost.

## Style × scenario matrix

Five styles × four scenarios = 20 cost cells per (broker, symbol). The migration seeds 4 of those cells per broker (`normal`, `news_active` where specified, `illiquid`, `volatile`). Style only modulates which components apply (e.g. scalping → no swap), not the profile selection.

## Authority note

This file is the architecture reference. Phase status for the fee-system build lives in `ROADMAP.md` (single source). If anything here contradicts `ROADMAP.md`'s phase table, `ROADMAP.md` wins.
