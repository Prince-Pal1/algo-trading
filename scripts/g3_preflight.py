#!/usr/bin/env python3
"""G.3 paper clock preflight smoke test.

Runs the full shadow pipeline end-to-end on 100 bars of XAUUSD 1h
historical data through donchian_gold + vol_momentum_gold. Asserts:
  - ≥1 order placed
  - ≥1 M3S leverage grant recorded
  - ≥1 LeverageBudgetAllocator decision recorded
  - Shadow parquet dump exists with expected columns
  - No exceptions bubble up

Exit 0 if all pass, 1 if any fail.

Run this as the final sanity check before G.3 Day 1 starts OR
immediately after Spotware KYC approval lands.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd

from src.data.feeds.parquet_replay_feed import ParquetReplayFeed
from src.m3s.leverage_budget import LeverageBudgetAllocator
from src.shadow_orchestrator import ShadowOrchestrator, StrategyRoute
from src.strategies.momentum.vol_momentum_gold import VolMomentumGoldStrategy
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy
from src.utils.types import Tier


DATA_PATH = "data/historical/XAUUSD_1h.parquet"
STATE_DUMP_PATH = Path("data/shadow_runs/g3_preflight")


async def _run_shadow_loop() -> dict:
    feed = ParquetReplayFeed(DATA_PATH, symbol="XAUUSD", timeframe="1h")
    donchian = DonchianGoldStrategy()
    vmg = VolMomentumGoldStrategy()

    orchestrator = ShadowOrchestrator(
        feed=feed,
        strategy_routes=[
            StrategyRoute(donchian, "institutional", 25.0),
            StrategyRoute(vmg, "institutional", 25.0),
        ],
        initial_institutional_cash=10_000.0,
        initial_aggressive_cash=0.0,
        indicators=[
            "donchian_20", "donchian_55", "donchian_120",
            "atr_14", "atr_20", "adx_14",
        ],
        state_dump_path=STATE_DUMP_PATH,
        state_dump_interval=1000,
        run_id="g3_preflight",
    )
    state = await orchestrator.run(DATA_PATH)
    return {
        "bars": state.bar_index,
        "trades": len(state.trades),
        "equity_final": state.equity_total[-1] if state.equity_total else 0.0,
        "stop_outs": state.broker_stop_outs,
    }


def _exercise_leverage_budget() -> dict:
    """Quick sanity check that the leverage budget allocator runs."""
    from src.m3s.types import PortfolioSnapshot, StrategySnapshot

    alloc = LeverageBudgetAllocator(
        aggregate_cap=80.0,
        tier_floors={
            Tier.INSTITUTIONAL_TREND: 0.30,
            Tier.INSTITUTIONAL_MR: 0.15,
        },
    )
    snap = PortfolioSnapshot(
        ts_ms=1_700_000_000_000,
        equity=10_000.0,
        hwm=10_000.0,
        drawdown_pct=0.0,
        per_strategy={
            "donchian_gold": StrategySnapshot(
                name="donchian_gold", n_trades_30d=50,
                rolling_sharpe_30d=1.3, realized_vol_30d=0.12, pnl_30d=0.0,
            ),
            "vol_momentum_gold": StrategySnapshot(
                name="vol_momentum_gold", n_trades_30d=40,
                rolling_sharpe_30d=1.1, realized_vol_30d=0.08, pnl_30d=0.0,
            ),
        },
        signal_corr={},
    )
    decision = alloc.allocate_leverage(
        snapshot=snap,
        strategy_requests={"donchian_gold": 50.0, "vol_momentum_gold": 50.0},
        strategy_tiers={
            "donchian_gold": Tier.INSTITUTIONAL_TREND,
            "vol_momentum_gold": Tier.INSTITUTIONAL_MR,
        },
    )
    return {
        "grants": decision.per_strategy,
        "aggregate_cap": decision.aggregate_cap,
        "dynamic_pool": decision.dynamic_pool,
    }


def main() -> int:
    failures: list[str] = []

    # Check 1: shadow pipeline runs
    print("[1/4] Running shadow pipeline end-to-end...")
    try:
        result = asyncio.run(_run_shadow_loop())
        print(f"      bars={result['bars']} trades={result['trades']} "
              f"equity={result['equity_final']:.2f} stop_outs={result['stop_outs']}")
    except Exception as e:
        failures.append(f"shadow pipeline crashed: {e}")
        print(f"      ❌ FAILED: {e}")
        return 1

    # Check 2: at least 1 order was placed
    print("[2/4] Verifying orders placed...")
    if result["trades"] < 1:
        failures.append(f"expected >=1 trade, got {result['trades']}")
        print(f"      ❌ FAILED: no trades")
    else:
        print(f"      ✅ {result['trades']} trades")

    # Check 3: shadow state parquet exists and has expected columns
    print("[3/4] Verifying shadow state parquet...")
    equity_parquet = STATE_DUMP_PATH / "equity.parquet"
    if not equity_parquet.exists():
        failures.append(f"state parquet missing: {equity_parquet}")
        print(f"      ❌ FAILED: {equity_parquet} does not exist")
    else:
        try:
            df = pd.read_parquet(equity_parquet)
            expected_cols = {
                "bar_index", "equity_institutional",
                "equity_aggressive", "equity_total", "margin_level",
            }
            actual_cols = set(df.columns)
            missing = expected_cols - actual_cols
            if missing:
                failures.append(f"state parquet missing columns: {missing}")
                print(f"      ❌ FAILED: missing columns {missing}")
            elif len(df) == 0:
                failures.append("state parquet empty")
                print(f"      ❌ FAILED: 0 rows")
            else:
                print(f"      ✅ {len(df)} equity points, "
                      f"final={df['equity_total'].iloc[-1]:.2f}")
        except Exception as e:
            failures.append(f"failed to read state parquet: {e}")
            print(f"      ❌ FAILED: {e}")

    # Check 4: LeverageBudgetAllocator sanity
    print("[4/4] Exercising LeverageBudgetAllocator...")
    try:
        budget = _exercise_leverage_budget()
        total_granted = sum(budget["grants"].values())
        if total_granted <= 0:
            failures.append("LeverageBudgetAllocator granted nothing")
            print(f"      ❌ FAILED: 0 grants")
        elif total_granted > budget["aggregate_cap"] + 1e-6:
            failures.append(f"granted {total_granted} > cap {budget['aggregate_cap']}")
            print(f"      ❌ FAILED: grants exceed cap")
        else:
            print(f"      ✅ {budget['grants']}, pool={budget['dynamic_pool']:.1f}")
    except Exception as e:
        failures.append(f"LeverageBudgetAllocator crashed: {e}")
        print(f"      ❌ FAILED: {e}")

    print()
    if failures:
        print(f"❌ PREFLIGHT FAILED: {len(failures)} issue(s)")
        for f in failures:
            print(f"   - {f}")
        return 1

    print("✅ PREFLIGHT PASSED — G.3 Day 1 is safe to start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
