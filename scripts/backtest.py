"""Unified backtest CLI — single command produces triple output (SQLite + JSON + HTML).

Usage:
    python -m scripts.backtest run bb_rsi_mr --symbol BTCUSDT --tf 1h
    python -m scripts.backtest run bb_rsi_mr --symbol BTCUSDT --tf 1h --days 365
    python -m scripts.backtest run sma_crossover --symbol BTCUSDT --tf 1h
    python -m scripts.backtest list                    # list past runs
    python -m scripts.backtest list bb_rsi_mr          # list runs for a strategy
    python -m scripts.backtest validate bb_rsi_mr --tier lite
    python -m scripts.backtest validate bb_rsi_mr --tier standard
    python -m scripts.backtest validate bb_rsi_mr --mode smoke
    python -m scripts.backtest validate bb_rsi_mr --mode crash_stress
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import pandas as pd

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.backtest.result_store import ResultStore
from src.backtest.validator import StrategyValidator
from src.data.downloader import BinanceDownloader
from src.strategies.base import BaseStrategy
from src.strategies.day_trading.bb_rsi_mr import BBRSIMeanRevStrategy
from src.strategies.filters.regime_filter import VPINRegimeFilter
from src.strategies.scalping.ema_crossover_wf import WalkForwardEMA
from src.strategies.trend_following.donchian_ensemble import DonchianEnsembleStrategy
from src.strategies.stat_arb.btc_neutral_mr import BTCNeutralMRStrategy
from src.strategies.momentum.vol_momentum import VolMomentumStrategy
from src.strategies.momentum.adaptive_momentum import AdaptiveMomentumStrategy
from src.strategies.event_driven.liquidation_cascade import LiquidationCascadeStrategy
from src.utils.types import Signal, SignalAction


# ---------------------------------------------------------------------------
# CLI Backtest Presets — maps preset IDs to (factory, indicators)
# ---------------------------------------------------------------------------
#
# DESIGN NOTE (task #125 G.8 consolidation, 2026-04-15):
# This module has TWO related-but-distinct registries:
#
#   src/strategies/router.py::STRATEGY_REGISTRY (canonical class registry)
#     dict[str, type[BaseStrategy]]
#     Maps strategy KEYS to BaseStrategy CLASSES. Used by the engine, by
#     ConfigRouter, and by deep_backtest for class-name lookup. Strategies
#     register themselves at module import time via `register_strategy()`.
#
#   scripts/backtest.py::BACKTEST_PRESETS (this module — CLI preset library)
#     dict[str, dict]
#     Maps PRESET IDs (e.g., "bb_rsi_mr_opt", "bb_rsi_mr_vpin") to
#     {"factory": lambda tf: <fully-configured strategy instance>,
#      "indicators": [list of indicator keys to precompute]}
#     A "preset" is a single strategy CLASS pre-configured with a specific
#     parameter combination. Multiple presets can wrap the same class
#     (bb_rsi_mr, bb_rsi_mr_opt, bb_rsi_mr_vpin all use BBRSIMeanRevStrategy).
#
# Why the two are NOT merged: they serve different abstractions. The router
# registry answers "what STRATEGY CLASSES exist?". The CLI presets answer
# "what FULLY-PARAMETERIZED variants does the unified backtest CLI support?".
# Merging them would either lose the parameter sets (if collapsed to classes)
# or push fully-configured factory lambdas into router.py (which the engine
# doesn't need and shouldn't import).
#
# `STRATEGY_REGISTRY` is kept as a backward-compat alias of `BACKTEST_PRESETS`
# below for legacy importers (scripts/gold_backtest.py).
# ---------------------------------------------------------------------------

class SMACrossoverStrategy(BaseStrategy):
    """SMA(10)/SMA(20) crossover — long only."""

    def __init__(self, timeframe: str = "1h"):
        super().__init__(name="sma_crossover", markets=["BTCUSDT"], timeframe=timeframe)

    def on_features(self, symbol, timeframe, features) -> Signal | None:
        prev = self._prev_features
        if prev is None:
            return None
        sma10 = features.get("SMA_10")
        sma20 = features.get("SMA_20")
        prev_sma10 = prev.get("SMA_10")
        prev_sma20 = prev.get("SMA_20")
        if any(v is None or pd.isna(v) for v in [sma10, sma20, prev_sma10, prev_sma20]):
            return None
        if prev_sma10 <= prev_sma20 and sma10 > sma20 and self._position == "FLAT":
            return Signal(symbol=symbol, action=SignalAction.LONG, confidence=1.0,
                          strategy_name=self.name, timeframe=timeframe)
        if prev_sma10 >= prev_sma20 and sma10 < sma20 and self._position == "LONG":
            return Signal(symbol=symbol, action=SignalAction.CLOSE, confidence=1.0,
                          strategy_name=self.name, timeframe=timeframe)
        return None


BACKTEST_PRESETS: dict[str, dict] = {
    "sma_crossover": {
        "factory": lambda tf: SMACrossoverStrategy(timeframe=tf),
        "indicators": ["sma_10", "sma_20"],
    },
    "bb_rsi_mr": {
        "factory": lambda tf: BBRSIMeanRevStrategy(
            name="bb_rsi_mr", markets=["BTCUSDT"], timeframe=tf,
            bb_period=20, rsi_period=14, adx_period=14, atr_period=14,
            rsi_overbought=75.0, rsi_oversold=25.0, adx_threshold=20.0,
            sl_atr_mult=3.0, max_hold_bars=48, cooldown_bars=5,
        ),
        "indicators": ["bbands_20", "rsi_14", "adx_14", "atr_14"],
    },
    "bb_rsi_mr_opt": {
        "factory": lambda tf: BBRSIMeanRevStrategy(
            name="bb_rsi_mr_opt", markets=["BTCUSDT"], timeframe=tf,
            bb_period=20, rsi_period=14, adx_period=14, atr_period=14,
            rsi_overbought=75.0, rsi_oversold=22.0, adx_threshold=20.0,
            sl_atr_mult=3.0, max_hold_bars=48, cooldown_bars=5,
        ),
        "indicators": ["bbands_20", "rsi_14", "adx_14", "atr_14"],
    },
    "bb_rsi_mr_vpin": {
        "factory": lambda tf: BBRSIMeanRevStrategy(
            name="bb_rsi_mr_vpin", markets=["BTCUSDT"], timeframe=tf,
            bb_period=20, rsi_period=14, adx_period=14, atr_period=14,
            rsi_overbought=75.0, rsi_oversold=25.0, adx_threshold=20.0,
            sl_atr_mult=3.0, max_hold_bars=48, cooldown_bars=5,
            vpin_filter=VPINRegimeFilter(threshold=0.7, n_buckets=50, cooldown_candles=3),
        ),
        "indicators": ["bbands_20", "rsi_14", "adx_14", "atr_14"],
    },
    "btc_neutral_mr": {
        "factory": lambda tf: BTCNeutralMRStrategy(
            name="btc_neutral_mr", markets=["ETHUSDT"], timeframe=tf,
            regression_window=168, z_entry=2.5, z_exit=0.5, z_stop=4.0,
            atr_period=14, sl_atr_mult=8.0, max_hold_bars=72, cooldown_bars=10,
        ),
        "indicators": ["atr_14"],
        "ref_symbol": "BTCUSDT",  # BTC close prices merged as ref_close column
    },
    "vol_momentum": {
        "factory": lambda tf: VolMomentumStrategy(
            name="vol_momentum", markets=["BTCUSDT"], timeframe=tf,
            momentum_window=168, vol_lookback=168, vol_target=0.15,
            atr_period=14, sl_atr_mult=3.0, max_hold_bars=168,
            cooldown_bars=5, long_only=False, rebalance_interval=24,
        ),
        "indicators": ["atr_14"],
    },
    "vol_momentum_long": {
        "factory": lambda tf: VolMomentumStrategy(
            name="vol_momentum_long", markets=["BTCUSDT"], timeframe=tf,
            momentum_window=168, vol_lookback=168, vol_target=0.15,
            atr_period=14, sl_atr_mult=3.0, max_hold_bars=168,
            cooldown_bars=5, long_only=True, rebalance_interval=24,
        ),
        "indicators": ["atr_14"],
    },
    "adaptive_momentum": {
        "factory": lambda tf: AdaptiveMomentumStrategy(
            name="adaptive_momentum", markets=["BTCUSDT"], timeframe=tf,
            lookbacks=(24, 72, 168), skip_bars=1,
            er_window=72, er_threshold=0.30,
            vol_lookback=168, vol_target=0.15,
            signal_gain=2.0, entry_threshold=0.15, exit_threshold=0.10,
            atr_period=14, sl_atr_mult=3.0, trail_atr_mult=4.0,
            max_hold_bars=336, cooldown_bars=5, rebalance_interval=6,
            long_only=False, max_risk_per_trade=0.012,
        ),
        "indicators": ["atr_14"],
    },
    "donchian_ensemble": {
        "factory": lambda tf: DonchianEnsembleStrategy(
            name="donchian_ensemble", markets=["BTCUSDT"], timeframe=tf,
            dc_short=20, dc_medium=55, dc_long=120,
            atr_period=14, sl_atr_mult=2.5,
            max_hold_bars=120, cooldown_bars=5, min_channels=2,
        ),
        "indicators": ["donchian_20", "donchian_55", "donchian_120", "atr_14"],
    },
    "donchian_ensemble_vpin": {
        "factory": lambda tf: DonchianEnsembleStrategy(
            name="donchian_ensemble_vpin", markets=["BTCUSDT"], timeframe=tf,
            dc_short=20, dc_medium=55, dc_long=120,
            atr_period=14, sl_atr_mult=2.5,
            max_hold_bars=120, cooldown_bars=5, min_channels=2,
            vpin_filter=VPINRegimeFilter(threshold=0.7, n_buckets=50, cooldown_candles=3),
        ),
        "indicators": ["donchian_20", "donchian_55", "donchian_120", "atr_14"],
    },
    "donchian_ensemble_adx": {
        "factory": lambda tf: DonchianEnsembleStrategy(
            name="donchian_ensemble_adx", markets=["BTCUSDT"], timeframe=tf,
            dc_short=20, dc_medium=55, dc_long=120,
            atr_period=14, sl_atr_mult=2.5,
            max_hold_bars=120, cooldown_bars=5, min_channels=2,
            adx_trend_threshold=25.0,
        ),
        "indicators": ["donchian_20", "donchian_55", "donchian_120", "atr_14", "adx_14"],
    },
    "donchian_ensemble_long": {
        "factory": lambda tf: DonchianEnsembleStrategy(
            name="donchian_ensemble_long", markets=["BTCUSDT"], timeframe=tf,
            dc_short=20, dc_medium=55, dc_long=120,
            atr_period=14, sl_atr_mult=2.5,
            max_hold_bars=120, cooldown_bars=5, min_channels=2,
            long_only=True,
        ),
        "indicators": ["donchian_20", "donchian_55", "donchian_120", "atr_14"],
    },
    "wf_ema": {
        "factory": lambda tf: WalkForwardEMA(
            name="wf_ema", markets=["BTCUSDT"], timeframe=tf,
            train_window=504, reoptim_interval=1344,
            fast_min=5, fast_max=50, fast_step=5,
            slow_min=30, slow_max=200, slow_step=10,
            fee_per_trade=0.001, hard_stop_pct=0.05,
            long_only=False,
        ),
        "indicators": [],  # WF-EMA computes its own EMAs from close prices
    },
    "wf_ema_long": {
        "factory": lambda tf: WalkForwardEMA(
            name="wf_ema_long", markets=["BTCUSDT"], timeframe=tf,
            train_window=504, reoptim_interval=1344,
            fast_min=5, fast_max=50, fast_step=5,
            slow_min=30, slow_max=200, slow_step=10,
            fee_per_trade=0.001, hard_stop_pct=0.05,
            long_only=True,
        ),
        "indicators": [],
    },
    # Session 23 Strategy 3 — liquidation cascade reversion.
    # Stage 1 research showed best config: SL -200 / TP +100 bps / 30m hold.
    # Strategy computes 1-minute returns + rolling std internally; no
    # indicators needed from feature_engine.
    "liquidation_cascade": {
        "factory": lambda tf: LiquidationCascadeStrategy(
            name="liquidation_cascade", markets=["BTCUSDT"], timeframe=tf,
            cascade_sigma=4.0, cascade_min_move_bps=50.0,
            rolling_window_bars=1440, sl_bps=200.0, tp_bps=100.0,
            max_hold_bars=30, cooldown_bars=60, long_only=True,
        ),
        "indicators": [],
    },
}


# Backward-compat alias — see DESIGN NOTE above. Legacy importers
# (scripts/gold_backtest.py) import `STRATEGY_REGISTRY` from this module.
# New code should reference `BACKTEST_PRESETS` directly. This alias is NOT
# the same thing as `src/strategies/router.py::STRATEGY_REGISTRY`, which is
# the canonical class registry — see the comment block above the BACKTEST_PRESETS
# definition for the disambiguation.
STRATEGY_REGISTRY = BACKTEST_PRESETS


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_run(args: argparse.Namespace) -> None:
    strategy_id = args.strategy_id
    if strategy_id not in STRATEGY_REGISTRY:
        print(f"Unknown strategy: {strategy_id}")
        print(f"Available: {', '.join(STRATEGY_REGISTRY)}")
        sys.exit(1)

    reg = STRATEGY_REGISTRY[strategy_id]
    symbol = args.symbol
    tf = args.tf
    days = args.days

    print(f"  Strategy:  {strategy_id}")
    print(f"  Symbol:    {symbol}")
    print(f"  Timeframe: {tf}")
    print(f"  Period:    {days} days")
    print()

    # Download data
    print("  Downloading data...")
    dl = BinanceDownloader()
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    ohlcv = await dl.download(
        symbol=symbol,
        timeframe=tf,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
    )
    print(f"  Downloaded {len(ohlcv)} candles")

    if len(ohlcv) < 50:
        print("  ERROR: Not enough data (need >= 50 candles)")
        sys.exit(1)

    # If strategy needs reference symbol data (e.g., BTC for beta-neutral)
    if "ref_symbol" in reg:
        ref_sym = reg["ref_symbol"]
        print(f"  Downloading reference data ({ref_sym})...")
        ref_data = await dl.download(
            symbol=ref_sym,
            timeframe=tf,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        # Align by truncating to shorter length
        min_len = min(len(ohlcv), len(ref_data))
        ohlcv = ohlcv.iloc[-min_len:].reset_index(drop=True)
        ref_data = ref_data.iloc[-min_len:].reset_index(drop=True)
        ohlcv["ref_close"] = ref_data["close"].values
        print(f"  Reference data aligned: {min_len} candles")

    # Setup risk manager if enabled
    risk_manager = None
    risk_mode_label = "NONE"
    if getattr(args, "enable_risk", False):
        from src.risk.config import RiskConfig
        from src.risk.manager import RiskManager
        from src.risk.state import RiskState
        risk_config = RiskConfig.from_toml()
        risk_state = RiskState(db_path=":memory:")
        risk_state.current_equity = 10_000.0
        risk_state.peak_equity = 10_000.0
        risk_state.active_mode = args.risk_mode
        risk_manager = RiskManager(config=risk_config, state=risk_state)
        risk_mode_label = args.risk_mode
        print(f"  Risk Mode: {args.risk_mode}")

    # Run backtest
    print("  Running backtest...")
    strategy = reg["factory"](tf)
    engine = BacktestEngine(config=BacktestConfig(
        commission_pct=args.commission / 100,
    ), risk_manager=risk_manager)
    result = engine.run(
        strategy=strategy,
        data=ohlcv,
        symbol=symbol,
        timeframe=tf,
        indicators=reg["indicators"],
    )

    print(f"  Trades: {len(result.trades)}")
    print(f"  Return: {result.metrics.get('total_return_pct', 0):.2f}%")
    print(f"  Sharpe: {result.metrics.get('sharpe', 0):.3f}")
    if result.risk_rejections > 0:
        print(f"  Risk Rejections: {result.risk_rejections}")
    print()

    # Save triple output
    print("  Saving results...")
    store = ResultStore()
    import json as _json
    config_data = {"commission_pct": args.commission / 100, "risk_mode": risk_mode_label}
    record = store.save_run(
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=tf,
        equity=result.equity_curve,
        trades=result.trades,
        ohlcv_df=ohlcv,
        config_json=_json.dumps(config_data),
    )
    store.close()

    print()
    print(f"  Run ID:  {record.run_id}")
    print(f"  JSON:    {record.json_path}")
    print(f"  HTML:    {record.html_path}")
    print()
    print("  Metrics (from DB = JSON = HTML):")
    for key in ["total_return_pct", "sharpe", "sortino", "max_drawdown_pct",
                "calmar", "win_rate_pct", "profit_factor", "total_trades", "psr"]:
        val = record.metrics.get(key)
        if val is not None:
            print(f"    {key:<22} {val}")
    print()
    print(f"  Open report: open {record.html_path}")


def cmd_list(args: argparse.Namespace) -> None:
    store = ResultStore()
    strategy_id = args.strategy_id

    if strategy_id:
        runs = store.get_runs(strategy_id)
        if not runs:
            print(f"  No runs found for {strategy_id}")
            return
        print(f"\n  Runs for {strategy_id}:\n")
    else:
        # List all strategies that have runs
        conn = store._ensure_conn()
        rows = conn.execute(
            """SELECT r.*, res.sharpe, res.max_drawdown_pct, res.total_return_pct, res.total_trades
               FROM backtest_runs r
               JOIN backtest_results res ON res.run_id = r.id
               ORDER BY r.created_at DESC LIMIT 50"""
        ).fetchall()
        runs = [dict(row) for row in rows]
        if not runs:
            print("  No backtest runs found.")
            return
        print("\n  All runs:\n")

    header = f"  {'ID':>4}  {'Strategy':<16} {'Symbol':<10} {'TF':<4} {'Return%':>8} {'Sharpe':>7} {'MaxDD%':>7} {'Trades':>6}  {'Date'}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in runs:
        print(
            f"  {r['id']:>4}  {r['strategy_id']:<16} {r['symbol']:<10} {r['timeframe']:<4} "
            f"{r.get('total_return_pct', 0):>7.2f}% {r.get('sharpe', 0):>7.3f} "
            f"{r.get('max_drawdown_pct', 0):>6.2f}% {r.get('total_trades', 0):>6}  "
            f"{r.get('created_at', '')[:16]}"
        )
    print()
    store.close()


async def cmd_validate(args: argparse.Namespace) -> None:
    """Run tiered validation or a single protocol on a strategy."""
    strategy_id = args.strategy_id
    if strategy_id not in STRATEGY_REGISTRY:
        print(f"Unknown strategy: {strategy_id}")
        print(f"Available: {', '.join(STRATEGY_REGISTRY)}")
        sys.exit(1)

    reg = STRATEGY_REGISTRY[strategy_id]
    symbol = args.symbol
    tf = args.tf
    tier = args.tier
    mode = args.mode

    print(f"\n  Strategy:  {strategy_id}")
    print(f"  Symbol:    {symbol}")
    print(f"  Timeframe: {tf}")
    if mode:
        print(f"  Protocol:  {mode}")
    else:
        print(f"  Tier:      {tier}")
    print()

    validator = StrategyValidator(
        strategy_factory=reg["factory"],
        symbol=symbol,
        indicators=reg["indicators"],
    )

    if mode == "param_sensitivity_2d":
        # Special handling — needs custom params and override factory
        from src.backtest.protocols import run_param_sensitivity_2d

        if not all([args.p1, args.p1base is not None, args.p2, args.p2base is not None]):
            print("  ERROR: param_sensitivity_2d requires --p1, --p1base, --p2, --p2base")
            sys.exit(1)

        def factory_with_params(tf_arg, **overrides):
            strat = reg["factory"](tf_arg)
            for k, v in overrides.items():
                cur = getattr(strat, k, None)
                if cur is not None and isinstance(cur, int):
                    v = max(1, int(round(v)))
                setattr(strat, k, v)
            return strat

        print(f"  Grid search: {args.p1} (base={args.p1base}) x {args.p2} (base={args.p2base})")
        print(f"  Running 5x5 grid (25 backtests)...\n")

        result = await run_param_sensitivity_2d(
            factory_with_params=factory_with_params,
            symbol=symbol,
            indicators=reg["indicators"],
            param1_name=args.p1,
            param1_base=args.p1base,
            param2_name=args.p2,
            param2_base=args.p2base,
            tf=tf,
            days=730,
        )

        # Print grid results
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.summary}")
        print(f"  Runtime: {result.runtime_seconds:.1f}s\n")

        if hasattr(result, "grid") and result.grid:
            # Print grid as table
            p1_vals = sorted(set(c.param1_value for c in result.grid))
            p2_vals = sorted(set(c.param2_value for c in result.grid))
            grid_map = {(c.param1_value, c.param2_value): c for c in result.grid}

            # Header
            hdr = f"  {'':>12}"
            for p2v in p2_vals:
                hdr += f"  {args.p2}={p2v:<8}"
            print(hdr)
            print("  " + "-" * len(hdr))

            best_sharpe = -999
            best_params = {}
            for p1v in p1_vals:
                row = f"  {args.p1}={p1v:<8}"
                for p2v in p2_vals:
                    cell = grid_map.get((p1v, p2v))
                    if cell:
                        s = cell.sharpe
                        marker = " *" if abs(s - (result.base_sharpe or 0)) < 0.001 else "  "
                        row += f"  {s:>7.3f}{marker}"
                        if s > best_sharpe:
                            best_sharpe = s
                            best_params = {args.p1: p1v, args.p2: p2v}
                    else:
                        row += f"  {'ERR':>9}"
                print(row)

            print(f"\n  Base Sharpe: {result.base_sharpe:.3f}")
            print(f"  Best Sharpe: {best_sharpe:.3f} at {best_params}")
            if result.base_sharpe and result.base_sharpe > 0:
                improvement = (best_sharpe - result.base_sharpe) / result.base_sharpe * 100
                print(f"  Improvement: {improvement:+.1f}%")
            print(f"  Sharpe StdDev: {result.sharpe_std:.3f}")
            print(f"  Robust: {result.robust}")

    elif mode:
        # Single protocol mode
        result = await validator.run_single_protocol(mode, tf=tf)
        if hasattr(result, "print_summary"):
            result.print_summary()
        else:
            # ProtocolResult
            status = "PASS" if result.passed else "FAIL"
            print(f"\n  [{status}] {result.summary}")
            print(f"  Runtime: {result.runtime_seconds:.1f}s")
    else:
        # Tier mode
        report, protocol_results = await validator.run_tier(tier, tf=tf)

        # Print protocol results
        if protocol_results:
            print(f"\n{'=' * 80}")
            print(f"  TIER '{tier.upper()}' PROTOCOL RESULTS")
            print(f"{'=' * 80}")
            passed = sum(1 for r in protocol_results if r.passed)
            total = len(protocol_results)
            for r in protocol_results:
                status = "PASS" if r.passed else "FAIL"
                print(f"  [{status}] {r.protocol:<20} {r.summary}")
            print(f"\n  Score: {passed}/{total} protocols passed")
            print(f"{'=' * 80}")

            # Persist validation results to DB
            store = ResultStore()
            session_id = store.save_validation(
                strategy_id=strategy_id,
                symbol=symbol,
                tier=tier,
                protocol_results=protocol_results,
                verdict=f"{passed}/{total} passed",
            )
            store.close()
            print(f"\n  Saved to DB: validation_session #{session_id}")

        # Print legacy validation report if available
        if report is not None:
            report.print_summary()

    print()


def cmd_compare(args: argparse.Namespace) -> None:
    """Compare all strategies from DB — cross-strategy ranking + portfolio metrics."""
    import json
    import numpy as np

    store = ResultStore()
    conn = store._ensure_conn()

    # Get specific runs or all
    if args.run_ids:
        placeholders = ",".join("?" for _ in args.run_ids)
        rows = conn.execute(
            f"""SELECT r.id, r.strategy_id, r.symbol, r.timeframe,
                       res.sharpe, res.sortino, res.total_return_pct, res.max_drawdown_pct,
                       res.win_rate_pct, res.profit_factor, res.total_trades, res.calmar,
                       res.psr, res.equity_curve_json, r.created_at
                FROM backtest_runs r
                JOIN backtest_results res ON res.run_id = r.id
                WHERE r.id IN ({placeholders})
                ORDER BY r.strategy_id, res.sharpe DESC""",
            args.run_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT r.id, r.strategy_id, r.symbol, r.timeframe,
                      res.sharpe, res.sortino, res.total_return_pct, res.max_drawdown_pct,
                      res.win_rate_pct, res.profit_factor, res.total_trades, res.calmar,
                      res.psr, res.equity_curve_json, r.created_at
               FROM backtest_runs r
               JOIN backtest_results res ON res.run_id = r.id
               ORDER BY r.strategy_id, res.sharpe DESC"""
        ).fetchall()

    if not rows:
        print("  No backtest runs found.")
        store.close()
        return

    # Group by strategy
    strategies: dict[str, list[dict]] = {}
    for row in rows:
        d = dict(row)
        sid = d["strategy_id"]
        if sid not in strategies:
            strategies[sid] = []
        strategies[sid].append(d)

    # ── CROSS-STRATEGY SUMMARY ──
    print(f"\n{'=' * 100}")
    print("  CROSS-STRATEGY COMPARISON")
    print(f"{'=' * 100}")
    header = (f"  {'Strategy':<18} {'Runs':>4} {'Avg Sharpe':>10} {'Best Sharpe':>11} "
              f"{'Avg Ret%':>8} {'Worst DD%':>9} {'Avg WR%':>7} {'Avg PF':>7} {'Trades':>6}")
    print(header)
    print("  " + "-" * 96)

    strategy_summaries = []
    for sid, runs in sorted(strategies.items()):
        sharpes = [r["sharpe"] for r in runs if r["sharpe"] is not None]
        returns = [r["total_return_pct"] for r in runs if r["total_return_pct"] is not None]
        dds = [r["max_drawdown_pct"] for r in runs if r["max_drawdown_pct"] is not None]
        wrs = [r["win_rate_pct"] for r in runs if r["win_rate_pct"] is not None]
        pfs = [r["profit_factor"] for r in runs if r["profit_factor"] is not None]
        trades = sum(r["total_trades"] or 0 for r in runs)

        avg_sharpe = np.mean(sharpes) if sharpes else 0
        best_sharpe = max(sharpes) if sharpes else 0
        avg_ret = np.mean(returns) if returns else 0
        worst_dd = max(dds) if dds else 0
        avg_wr = np.mean(wrs) if wrs else 0
        avg_pf = np.mean(pfs) if pfs else 0

        print(f"  {sid:<18} {len(runs):>4} {avg_sharpe:>10.3f} {best_sharpe:>11.3f} "
              f"{avg_ret:>7.2f}% {worst_dd:>8.2f}% {avg_wr:>6.1f}% {avg_pf:>7.3f} {trades:>6}")

        strategy_summaries.append({
            "strategy": sid, "runs": len(runs), "avg_sharpe": round(avg_sharpe, 3),
            "best_sharpe": round(best_sharpe, 3), "avg_return_pct": round(avg_ret, 2),
            "worst_dd_pct": round(worst_dd, 2), "avg_win_rate": round(avg_wr, 1),
            "avg_pf": round(avg_pf, 3), "total_trades": trades,
        })

    # ── PER-SYMBOL DETAIL for bb_rsi_mr (latest runs only) ──
    if "bb_rsi_mr" in strategies:
        print(f"\n{'=' * 100}")
        print("  BB+RSI MEAN REVERSION — PER-SYMBOL DETAIL (latest 730-day runs)")
        print(f"{'=' * 100}")

        # Deduplicate: keep latest run per symbol
        seen_symbols: dict[str, dict] = {}
        for r in strategies["bb_rsi_mr"]:
            sym = r["symbol"]
            if sym not in seen_symbols or r["id"] > seen_symbols[sym]["id"]:
                seen_symbols[sym] = r

        print(f"  {'Symbol':<12} {'Sharpe':>7} {'Return%':>8} {'MaxDD%':>7} {'WR%':>5} "
              f"{'PF':>6} {'Trades':>6} {'PSR':>6} {'Calmar':>7} {'Verdict'}")
        print("  " + "-" * 85)

        winners = []
        for sym in sorted(seen_symbols.keys()):
            r = seen_symbols[sym]
            sharpe = r["sharpe"] or 0
            verdict = "WINNER" if sharpe > 0.2 else ("MARGINAL" if sharpe > 0 else "LOSER")
            psr = r["psr"] or 0
            print(f"  {sym:<12} {sharpe:>7.3f} {r['total_return_pct']:>7.2f}% "
                  f"{r['max_drawdown_pct']:>6.2f}% {r['win_rate_pct']:>4.1f}% "
                  f"{r['profit_factor']:>6.3f} {r['total_trades']:>6} {psr:>6.4f} "
                  f"{r['calmar'] or 0:>7.3f} {verdict}")
            if sharpe > 0.2:
                winners.append(r)

        # ── PORTFOLIO ANALYSIS (5 winners) ──
        if len(winners) >= 2:
            print(f"\n{'=' * 100}")
            print(f"  PORTFOLIO ANALYSIS — {len(winners)} winning symbols (equal-weight)")
            print(f"{'=' * 100}")

            from src.backtest.metrics import compute_metrics

            # Load equity curves and combine
            equity_curves = []
            for r in winners:
                try:
                    eq_data = json.loads(r["equity_curve_json"])
                    eq_series = pd.Series(
                        {int(k): v for k, v in eq_data.items()},
                        dtype=float,
                    ).sort_index()
                    # Normalize to returns
                    eq_returns = eq_series.pct_change().dropna()
                    equity_curves.append(eq_returns)
                except Exception:
                    continue

            if equity_curves:
                # Align and combine (equal-weight average of returns)
                combined = pd.concat(equity_curves, axis=1).dropna()
                if len(combined) > 10:
                    avg_returns = combined.mean(axis=1)
                    # Build portfolio equity curve
                    portfolio_equity = (1 + avg_returns).cumprod() * 10000
                    portfolio_equity.index = range(len(portfolio_equity))

                    # Compute metrics
                    p_ret = (portfolio_equity.iloc[-1] / 10000 - 1) * 100
                    p_dd = ((portfolio_equity / portfolio_equity.cummax()) - 1).min() * 100
                    p_vol = avg_returns.std() * np.sqrt(365 * 24)  # hourly to annual
                    p_mean = avg_returns.mean() * 365 * 24
                    p_sharpe = p_mean / p_vol if p_vol > 0 else 0

                    # Correlation matrix
                    corr = combined.corr()
                    avg_corr = (corr.values.sum() - len(winners)) / (len(winners) * (len(winners) - 1)) if len(winners) > 1 else 0

                    win_syms = [r["symbol"] for r in winners]
                    print(f"  Symbols: {', '.join(win_syms)}")
                    print(f"  Portfolio Sharpe:       {p_sharpe:.3f}")
                    print(f"  Portfolio Return:       {p_ret:.2f}%")
                    print(f"  Portfolio Max DD:       {abs(p_dd):.2f}%")
                    print(f"  Avg Pairwise Corr:      {avg_corr:.4f}")
                    print(f"  Diversification Benefit: {'YES' if p_sharpe > max(r['sharpe'] for r in winners) else 'NO'}")

    # ── VALIDATION SUMMARY ──
    val_rows = conn.execute(
        """SELECT strategy_id, symbol, tier, verdict, created_at
           FROM validation_sessions ORDER BY id"""
    ).fetchall()

    if val_rows:
        print(f"\n{'=' * 100}")
        print("  VALIDATION SUMMARY")
        print(f"{'=' * 100}")
        print(f"  {'Strategy':<18} {'Symbol':<12} {'Tier':<10} {'Verdict':<20} {'Date'}")
        print("  " + "-" * 75)
        for v in val_rows:
            print(f"  {v['strategy_id']:<18} {v['symbol']:<12} {v['tier']:<10} "
                  f"{v['verdict']:<20} {v['created_at'][:16]}")

    print()
    store.close()


# ---------------------------------------------------------------------------
# Param priority for auto-optimization
# ---------------------------------------------------------------------------

PARAM_PRIORITY = {
    "donchian_ensemble": [("sl_atr_mult", 2.5), ("max_hold_bars", 120), ("min_channels", 2), ("cooldown_bars", 5)],
    "donchian_ensemble_adx": [("sl_atr_mult", 2.5), ("max_hold_bars", 120), ("adx_trend_threshold", 25.0), ("cooldown_bars", 5)],
    "vol_momentum": [("momentum_window", 168), ("vol_target", 0.15), ("sl_atr_mult", 3.0), ("rebalance_interval", 24)],
    "adaptive_momentum": [("sl_atr_mult", 3.0), ("trail_atr_mult", 4.0), ("er_threshold", 0.30), ("entry_threshold", 0.15)],
    "bb_rsi_mr": [("rsi_oversold", 25.0), ("sl_atr_mult", 3.0), ("adx_threshold", 20.0), ("bb_period", 20)],
    "bb_rsi_mr_opt": [("rsi_oversold", 22.0), ("sl_atr_mult", 3.0), ("adx_threshold", 20.0), ("bb_period", 20)],
}


async def cmd_optimize(args: argparse.Namespace) -> None:
    """Systematic multi-round parameter optimization."""
    from src.backtest.protocols import run_param_sensitivity_2d

    strategy_id = args.strategy_id
    if strategy_id not in STRATEGY_REGISTRY:
        print(f"Unknown strategy: {strategy_id}")
        print(f"Available: {', '.join(STRATEGY_REGISTRY)}")
        sys.exit(1)

    reg = STRATEGY_REGISTRY[strategy_id]
    symbol = args.symbol
    tf = args.tf
    rounds = args.rounds

    # Get param priority for this strategy
    if strategy_id not in PARAM_PRIORITY:
        print(f"  No param priority defined for {strategy_id}")
        print(f"  Available: {', '.join(PARAM_PRIORITY)}")
        sys.exit(1)

    params = PARAM_PRIORITY[strategy_id]
    print(f"\n  Optimizing: {strategy_id} on {symbol}")
    print(f"  Rounds: {rounds}")
    print(f"  Params (priority order): {', '.join(p[0] for p in params)}")
    print()

    def factory_with_params(tf_arg, **overrides):
        strat = reg["factory"](tf_arg)
        for k, v in overrides.items():
            cur = getattr(strat, k, None)
            if cur is not None and isinstance(cur, int):
                v = max(1, int(round(v)))
            setattr(strat, k, v)
        return strat

    best_sharpe = None
    best_params = {}
    current_bases = {name: base for name, base in params}
    no_improvement_count = 0

    for round_num in range(1, rounds + 1):
        # Select 2 params for this round (rotate through priority list)
        idx1 = ((round_num - 1) * 2) % len(params)
        idx2 = ((round_num - 1) * 2 + 1) % len(params)
        p1_name, p1_base = params[idx1][0], current_bases[params[idx1][0]]
        p2_name, p2_base = params[idx2][0], current_bases[params[idx2][0]]

        print(f"  === Round {round_num} ===")
        print(f"  Grid: {p1_name} (base={p1_base}) x {p2_name} (base={p2_base})")

        result = await run_param_sensitivity_2d(
            factory_with_params=factory_with_params,
            symbol=symbol,
            indicators=reg["indicators"],
            param1_name=p1_name,
            param1_base=p1_base,
            param2_name=p2_name,
            param2_base=p2_base,
            tf=tf,
            days=730,
        )

        # Find best cell
        round_best_sharpe = -999
        round_best_params = {}
        if hasattr(result, "grid") and result.grid:
            for cell in result.grid:
                if cell.sharpe > round_best_sharpe:
                    round_best_sharpe = cell.sharpe
                    round_best_params = {p1_name: cell.param1_value, p2_name: cell.param2_value}

        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.summary}")
        print(f"  Best: Sharpe {round_best_sharpe:.3f} at {round_best_params}")

        # Check improvement
        if best_sharpe is not None:
            improvement = (round_best_sharpe - best_sharpe) / abs(best_sharpe) * 100 if best_sharpe != 0 else 100
            print(f"  Improvement: {improvement:+.1f}%")
            if improvement < 5.0:
                no_improvement_count += 1
                print(f"  (No significant improvement — {no_improvement_count}/3)")
            else:
                no_improvement_count = 0
        else:
            improvement = 100

        if round_best_sharpe > (best_sharpe or -999):
            best_sharpe = round_best_sharpe
            best_params.update(round_best_params)
            # Update bases for next round
            for k, v in round_best_params.items():
                if k in current_bases:
                    current_bases[k] = v

        print()

        # Stop criteria
        if no_improvement_count >= 3:
            print("  STOP: 3 consecutive rounds without >5% improvement")
            break

    print(f"  === OPTIMIZATION COMPLETE ===")
    print(f"  Best Sharpe: {best_sharpe:.3f}")
    print(f"  Best Params: {best_params}")
    print(f"  Rounds completed: {min(round_num, rounds)}/{rounds}")
    print()


# ---------------------------------------------------------------------------
# risk-compare
# ---------------------------------------------------------------------------

async def cmd_risk_compare(args: argparse.Namespace) -> None:
    """Run same strategy across risk modes and compare results."""
    from src.risk.config import RiskConfig
    from src.risk.manager import RiskManager
    from src.risk.state import RiskState

    strategy_id = args.strategy_id
    if strategy_id not in STRATEGY_REGISTRY:
        print(f"Unknown strategy: {strategy_id}")
        print(f"Available: {', '.join(STRATEGY_REGISTRY)}")
        sys.exit(1)

    reg = STRATEGY_REGISTRY[strategy_id]
    symbol = args.symbol
    tf = args.tf
    days = args.days
    commission_pct = args.commission / 100
    modes = ["NONE", "AGGRESSIVE", "BALANCED", "DEFENSIVE"]

    print(f"\n  RISK MODE COMPARISON: {strategy_id} on {symbol} {tf} ({days} days)")
    print("=" * 72)

    # Download data ONCE
    print("  Downloading data...")
    dl = BinanceDownloader()
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    ohlcv = await dl.download(
        symbol=symbol, timeframe=tf,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
    )
    print(f"  Downloaded {len(ohlcv)} candles\n")

    if len(ohlcv) < 50:
        print("  ERROR: Not enough data (need >= 50 candles)")
        sys.exit(1)

    # If strategy needs reference symbol data
    if "ref_symbol" in reg:
        ref_sym = reg["ref_symbol"]
        ref_data = await dl.download(
            symbol=ref_sym, timeframe=tf,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        min_len = min(len(ohlcv), len(ref_data))
        ohlcv = ohlcv.iloc[-min_len:].reset_index(drop=True)
        ref_data = ref_data.iloc[-min_len:].reset_index(drop=True)
        ohlcv["ref_close"] = ref_data["close"].values

    # Run backtest for each mode
    risk_config = RiskConfig.from_toml()
    results: dict[str, dict] = {}

    for mode in modes:
        print(f"  Running {mode}...")
        strategy = reg["factory"](tf)

        if mode == "NONE":
            risk_mgr = None
        else:
            risk_state = RiskState(db_path=":memory:")
            risk_state.current_equity = 10_000.0
            risk_state.peak_equity = 10_000.0
            risk_state.active_mode = mode
            risk_mgr = RiskManager(config=risk_config, state=risk_state)

        engine = BacktestEngine(
            config=BacktestConfig(commission_pct=commission_pct),
            risk_manager=risk_mgr,
        )
        result = engine.run(
            strategy=strategy, data=ohlcv.copy(),
            symbol=symbol, timeframe=tf,
            indicators=reg["indicators"],
        )
        results[mode] = {
            "trades": len(result.trades),
            "return_pct": result.metrics.get("total_return_pct", 0),
            "sharpe": result.metrics.get("sharpe", 0),
            "max_dd": result.metrics.get("max_drawdown_pct", 0),
            "win_rate": result.metrics.get("win_rate_pct", 0),
            "profit_factor": result.metrics.get("profit_factor", 0),
            "rejections": result.risk_rejections,
            "rejection_reasons": result.risk_rejection_reasons,
        }

    # Print comparison table
    print()
    header = f"  {'Metric':<22}"
    for m in modes:
        header += f"{m:>14}"
    print(header)
    print("  " + "-" * (22 + 14 * len(modes)))

    rows = [
        ("Total Return %", "return_pct", ".2f"),
        ("Sharpe", "sharpe", ".3f"),
        ("Max Drawdown %", "max_dd", ".2f"),
        ("Win Rate %", "win_rate", ".1f"),
        ("Total Trades", "trades", "d"),
        ("Profit Factor", "profit_factor", ".2f"),
        ("Signals Rejected", "rejections", "d"),
    ]

    for label, key, fmt in rows:
        line = f"  {label:<22}"
        for m in modes:
            val = results[m][key]
            if key == "rejections" and m == "NONE":
                line += f"{'--':>14}"
            else:
                line += f"{val:>14{fmt}}"
        print(line)

    print("  " + "-" * (22 + 14 * len(modes)))

    # Print rejection reasons
    has_rejections = any(results[m]["rejections"] > 0 for m in modes if m != "NONE")
    if has_rejections:
        print("\n  Top Rejection Reasons:")
        for m in modes:
            if m == "NONE":
                continue
            reasons = results[m]["rejection_reasons"]
            if reasons:
                sorted_reasons = sorted(reasons.items(), key=lambda x: -x[1])
                reason_strs = [f"{r} ({c})" for r, c in sorted_reasons[:5]]
                print(f"    {m}: {', '.join(reason_strs)}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backtest",
        description="Backtest CLI — single command, triple output (SQLite + JSON + HTML)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="Run a backtest → produces SQLite row + JSON + HTML")
    p_run.add_argument("strategy_id", type=str, help="Strategy ID from registry")
    p_run.add_argument("--symbol", type=str, default="BTCUSDT", help="Symbol (default: BTCUSDT)")
    p_run.add_argument("--tf", type=str, default="1h", help="Timeframe (default: 1h)")
    p_run.add_argument("--days", type=int, default=180, help="Lookback days (default: 180)")
    p_run.add_argument("--commission", type=float, default=0.04, help="Commission %% (default: 0.04)")
    p_run.add_argument("--enable-risk", action="store_true", help="Enable risk gating")
    p_run.add_argument("--risk-mode", default="AGGRESSIVE",
                        choices=["AGGRESSIVE", "BALANCED", "DEFENSIVE"],
                        help="Risk mode (default: AGGRESSIVE)")

    # list
    p_list = sub.add_parser("list", help="List past backtest runs")
    p_list.add_argument("strategy_id", type=str, nargs="?", default=None, help="Filter by strategy")

    # validate
    p_val = sub.add_parser("validate", help="Run tiered validation protocols")
    p_val.add_argument("strategy_id", type=str, help="Strategy ID from registry")
    p_val.add_argument("--symbol", type=str, default="BTCUSDT", help="Symbol (default: BTCUSDT)")
    p_val.add_argument("--tf", type=str, default="1h", help="Timeframe (default: 1h)")
    p_val.add_argument("--tier", type=str, default="lite",
                        choices=["lite", "standard", "intense", "research"],
                        help="Validation tier (default: lite)")
    p_val.add_argument("--mode", type=str, default=None,
                        help="Run a single protocol (smoke, spot_check, crash_stress, "
                             "multi_interval, multi_timeframe, regime_test, commission_sweep, "
                             "param_sensitivity_2d)")
    p_val.add_argument("--p1", type=str, default=None, help="Param1 name for param_sensitivity_2d")
    p_val.add_argument("--p1base", type=float, default=None, help="Param1 base value")
    p_val.add_argument("--p2", type=str, default=None, help="Param2 name for param_sensitivity_2d")
    p_val.add_argument("--p2base", type=float, default=None, help="Param2 base value")

    # compare
    p_cmp = sub.add_parser("compare", help="Compare all strategies from DB")
    p_cmp.add_argument("run_ids", type=int, nargs="*", help="Specific run IDs (default: all)")

    # optimize
    p_opt = sub.add_parser("optimize", help="Systematic multi-round parameter optimization")
    p_opt.add_argument("strategy_id", type=str, help="Strategy ID from registry")
    p_opt.add_argument("--symbol", type=str, required=True, help="Symbol to optimize on")
    p_opt.add_argument("--tf", type=str, default="1h", help="Timeframe (default: 1h)")
    p_opt.add_argument("--rounds", type=int, default=3, help="Max optimization rounds (default: 3)")

    # risk-compare
    p_rc = sub.add_parser("risk-compare", help="Compare same strategy across risk modes")
    p_rc.add_argument("strategy_id", type=str, help="Strategy ID from registry")
    p_rc.add_argument("--symbol", type=str, default="BTCUSDT", help="Symbol (default: BTCUSDT)")
    p_rc.add_argument("--tf", type=str, default="1h", help="Timeframe (default: 1h)")
    p_rc.add_argument("--days", type=int, default=180, help="Lookback days (default: 180)")
    p_rc.add_argument("--commission", type=float, default=0.04, help="Commission %% (default: 0.04)")

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(cmd_run(args))
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "validate":
        asyncio.run(cmd_validate(args))
    elif args.command == "compare":
        cmd_compare(args)
    elif args.command == "optimize":
        asyncio.run(cmd_optimize(args))
    elif args.command == "risk-compare":
        asyncio.run(cmd_risk_compare(args))


if __name__ == "__main__":
    main()
