"""Sub-phase 0.7 — tests for src/m3s/meta_backtest.py + BT #3 gate."""

from __future__ import annotations

import numpy as np
import pytest

from src.m3s.meta_backtest import (
    MetaBacktestResult,
    MetaTrade,
    compare_baselines,
    run_fixed_weight_baseline,
    run_m3s_simulation,
)
from src.m3s.modes import M3SMode


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_700_000_000_000


def _ts(day: int) -> int:
    return _BASE_TS + day * _MS_PER_DAY


def _synthetic_trades(days: int = 180, seed: int = 42) -> list[MetaTrade]:
    """Three strategies, alternating winners and losers, +EV overall."""
    rng = np.random.default_rng(seed)
    strategies = [
        ("bb_rsi_mr_opt", "BTCUSDT", 25, 40),
        ("donchian_adx", "ETHUSDT", 20, 30),
        ("vol_momentum", "SOLUSDT", 15, 25),
    ]
    trades: list[MetaTrade] = []
    for day in range(days):
        for strat, sym, mean_pnl, stdev in strategies:
            trades.append(MetaTrade(
                strategy=strat,
                symbol=sym,
                pnl=float(rng.normal(mean_pnl, stdev)),
                ts_ms=_ts(day),
            ))
    return trades


# ══════════════════════════════════════════════════════════════════════
# Metrics & baselines
# ══════════════════════════════════════════════════════════════════════


class TestFixedWeightBaseline:
    def test_runs_without_error(self):
        trades = _synthetic_trades(days=30)
        weights = {"bb_rsi_mr_opt": 0.4, "donchian_adx": 0.3, "vol_momentum": 0.3}
        result = run_fixed_weight_baseline(trades, weights)
        assert isinstance(result, MetaBacktestResult)
        assert result.n_trades == 30 * 3

    def test_zero_weight_strategy_ignored(self):
        trades = [
            MetaTrade("a", "BTC", 100.0, _ts(0)),
            MetaTrade("b", "BTC", 100.0, _ts(0)),
        ]
        result = run_fixed_weight_baseline(trades, {"a": 0.0, "b": 1.0})
        # Only b contributed
        assert result.final_equity == pytest.approx(10_100.0)

    def test_final_equity_matches_total(self):
        trades = [
            MetaTrade("a", "BTC", 100.0, _ts(0)),
            MetaTrade("a", "BTC", -50.0, _ts(1)),
            MetaTrade("a", "BTC", 200.0, _ts(2)),
        ]
        result = run_fixed_weight_baseline(trades, {"a": 1.0})
        assert result.final_equity == pytest.approx(10_250.0)


class TestM3SSimulation:
    def test_runs_without_error(self):
        trades = _synthetic_trades(days=30)
        result = run_m3s_simulation(trades, mode=M3SMode.STANDARD)
        assert isinstance(result, MetaBacktestResult)
        assert result.name == "m3s_standard"

    def test_all_four_modes_finish(self):
        trades = _synthetic_trades(days=30)
        for mode in [M3SMode.CONSERVATIVE, M3SMode.STANDARD, M3SMode.GROWTH]:
            result = run_m3s_simulation(trades, mode=mode)
            assert result.final_equity > 0
            assert isinstance(result.sharpe_annualized, float)

    def test_equity_curve_is_chronological(self):
        trades = _synthetic_trades(days=30)
        result = run_m3s_simulation(trades, mode=M3SMode.STANDARD)
        timestamps = [ts for ts, _ in result.equity_curve]
        assert timestamps == sorted(timestamps)

    def test_empty_trades_yields_initial_equity(self):
        result = run_m3s_simulation([], mode=M3SMode.STANDARD)
        assert result.final_equity == 10_000.0
        assert result.equity_curve == []
        assert result.sharpe_annualized == 0.0


# ══════════════════════════════════════════════════════════════════════
# compare_baselines — BT #3 gate
# ══════════════════════════════════════════════════════════════════════


class TestBacktestGate3:
    """BT #3: run all baselines side by side and check invariants.

    This is the gate Prince reviews before enabling M3S authoritative
    mode. A passing run does NOT guarantee M3S beats fixed_weight — Prince
    still inspects numbers by hand. These tests only verify the simulator
    behaves sanely.
    """

    def test_compare_baselines_returns_all_methods(self):
        trades = _synthetic_trades(days=60)
        weights = {"bb_rsi_mr_opt": 0.4, "donchian_adx": 0.3, "vol_momentum": 0.3}
        results = compare_baselines(trades, fixed_weights=weights)
        assert "fixed_weight" in results
        assert "m3s_conservative" in results
        assert "m3s_standard" in results
        assert "m3s_growth" in results

    def test_all_baselines_positive_on_positive_stream(self):
        trades = _synthetic_trades(days=180)
        weights = {"bb_rsi_mr_opt": 0.4, "donchian_adx": 0.3, "vol_momentum": 0.3}
        results = compare_baselines(trades, fixed_weights=weights)
        for name, result in results.items():
            assert result.final_equity > 10_000.0, f"{name} lost money on +EV stream"

    def test_conservative_dd_leq_growth_dd(self):
        """CONSERVATIVE should have smaller max DD than GROWTH on a volatile
        stream with a synthetic crash."""
        # Build a stream with a big crash at day 60
        trades: list[MetaTrade] = []
        rng = np.random.default_rng(99)
        for day in range(120):
            for strat in ("bb_rsi_mr_opt", "donchian_adx", "vol_momentum"):
                if day == 60:
                    pnl = -800.0  # Synthetic crash day, all strategies lose
                else:
                    pnl = float(rng.normal(25, 40))
                trades.append(MetaTrade(strat, "BTC", pnl, _ts(day)))

        weights = {"bb_rsi_mr_opt": 0.4, "donchian_adx": 0.3, "vol_momentum": 0.3}
        results = compare_baselines(trades, fixed_weights=weights)
        # Conservative drawdown should be ≤ Growth drawdown
        dd_cons = results["m3s_conservative"].max_drawdown_pct
        dd_growth = results["m3s_growth"].max_drawdown_pct
        assert dd_cons <= dd_growth + 0.02, (
            f"CONSERVATIVE DD {dd_cons:.3f} > GROWTH DD {dd_growth:.3f}"
        )

    def test_all_modes_produce_finite_metrics(self):
        trades = _synthetic_trades(days=90)
        weights = {"bb_rsi_mr_opt": 0.4, "donchian_adx": 0.3, "vol_momentum": 0.3}
        results = compare_baselines(trades, fixed_weights=weights)
        import math
        for name, result in results.items():
            assert math.isfinite(result.sharpe_annualized), f"{name} Sharpe not finite"
            assert math.isfinite(result.max_drawdown_pct), f"{name} DD not finite"
            assert math.isfinite(result.final_equity), f"{name} equity not finite"
            assert math.isfinite(result.calmar), f"{name} calmar not finite"
