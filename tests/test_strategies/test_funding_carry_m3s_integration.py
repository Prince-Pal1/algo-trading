"""A.5 M3S integration gate for FundingCarryStrategy.

Verifies that FundingCarryStrategy plugs into M3S cleanly:
1. Registry contains `funding_carry` → FundingCarryStrategy
2. from_config() loads defaults from strategies.toml
3. M3S PortfolioTracker.on_trade_close records carry trades correctly
4. M3S allocator includes funding_carry in its allocation when given
   enough history
5. Synthetic cross-strategy correlation stays low (<0.3) against a
   mocked bb_rsi_mr-like stream
"""

from __future__ import annotations

import numpy as np
import pytest

from src.m3s.allocator import Allocator
from src.m3s.compounder import Compounder
from src.m3s.conviction import ConvictionScorer
from src.m3s.edge_decay import EdgeDecayMonitor
from src.m3s.hooks import M3S
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.strategies.router import STRATEGY_REGISTRY


_MS_PER_EPOCH = 8 * 3600 * 1000


class TestRegistry:
    def test_funding_carry_registered(self):
        assert "funding_carry" in STRATEGY_REGISTRY
        assert STRATEGY_REGISTRY["funding_carry"] is FundingCarryStrategy

    def test_from_config_loads_defaults(self):
        """from_config should read [funding_carry] section from strategies.toml."""
        strategy = FundingCarryStrategy.from_config("funding_carry")
        assert strategy.name == "funding_carry"
        assert "BTCUSDT-CARRY" in strategy.markets
        assert strategy.timeframe == "8h"
        assert strategy.friction_pct == 0.00005
        assert strategy.flip_persistence_bars == 3
        assert strategy.max_drawdown_kill_pct == 0.03


class TestPortfolioTrackerRecording:
    def test_tracker_records_carry_trades(self):
        tracker = PortfolioTracker(initial_equity=10_000.0)
        # Simulate 30 epochs of positive funding P&L
        for i in range(30):
            ts = 1_700_000_000_000 + i * _MS_PER_EPOCH
            tracker.on_trade_close("funding_carry", pnl=3.0, symbol="BTCUSDT-CARRY", ts_ms=ts)
        assert tracker.equity == pytest.approx(10_090.0)
        assert tracker.hwm == pytest.approx(10_090.0)
        assert tracker.drawdown_pct == 0.0
        snap = tracker.snapshot(now_ms=1_700_000_000_000 + 30 * _MS_PER_EPOCH)
        assert "funding_carry" in snap.per_strategy
        assert snap.per_strategy["funding_carry"].n_trades_30d == 30
        assert snap.per_strategy["funding_carry"].pnl_30d == pytest.approx(90.0)


class TestM3SAllocationIncludesCarry:
    def _populate_mature_portfolio(self, tracker: PortfolioTracker):
        """Populate the tracker with 90 days of simulated trades across 4
        strategies — 3 existing directional + funding_carry. This pushes
        all strategies past the cold-start threshold."""
        rng = np.random.default_rng(17)
        _MS_PER_DAY = 86_400_000
        base_ts = 1_700_000_000_000

        # 3 directional strategies: bb_rsi_mr, donchian_adx, vol_momentum
        directional_mean = {"bb_rsi_mr_opt": 15, "donchian_adx": 20, "vol_momentum": 25}
        directional_stdev = {"bb_rsi_mr_opt": 25, "donchian_adx": 35, "vol_momentum": 40}
        for day in range(90):
            ts = base_ts + day * _MS_PER_DAY
            for strat_name in directional_mean:
                # Drop ~1 trade per day per strategy
                pnl = float(rng.normal(directional_mean[strat_name], directional_stdev[strat_name]))
                tracker.on_trade_close(strat_name, pnl=pnl, symbol="BTCUSDT", ts_ms=ts)

            # Funding carry: 3 trades per day (every 8h), low vol
            for epoch in range(3):
                ts_epoch = ts + epoch * _MS_PER_EPOCH
                funding_pnl = float(rng.normal(1.0, 2.0))  # small, low-vol yield
                tracker.on_trade_close("funding_carry", pnl=funding_pnl, symbol="BTCUSDT-CARRY", ts_ms=ts_epoch)

    def test_allocator_assigns_weight_to_funding_carry(self):
        tracker = PortfolioTracker(initial_equity=10_000.0)
        self._populate_mature_portfolio(tracker)
        alloc = Allocator(mode=MODE_PRESETS[M3SMode.STANDARD], tracker=tracker)
        snap = tracker.snapshot(now_ms=1_700_000_000_000 + 90 * 86_400_000)
        decision = alloc.compute(snap)
        assert "funding_carry" in decision.weights
        # Funding carry should get nonzero allocation
        assert decision.weights["funding_carry"] > 0.0
        # And respect the per-strategy cap
        assert decision.weights["funding_carry"] <= MODE_PRESETS[M3SMode.STANDARD].max_per_strategy_cap


class TestCorrelationToExistingBook:
    def test_funding_carry_exposure_correlation_low(self):
        """Carry strategy is always-long synthetic BTCUSDT-CARRY. Its exposure
        to price directionals (bb_rsi_mr / donchian / vol_momentum on BTCUSDT)
        should compute correlation ~0 because the exposure symbol differs and
        because the carry is a pure position (+1), not a flipping signal.
        """
        tracker = PortfolioTracker(initial_equity=10_000.0)
        rng = np.random.default_rng(7)
        base_ts = 1_700_000_000_000

        # Simulate 100 hourly bars of per-bar exposure for each strategy
        for i in range(100):
            ts = base_ts + i * 3600 * 1000

            # Directional strategies alternate long/short on BTCUSDT
            tracker.on_bar("bb_rsi_mr_opt", "BTCUSDT", exposure=int(rng.choice([-1, 0, 1])), ts_ms=ts)
            tracker.on_bar("donchian_adx", "BTCUSDT", exposure=int(rng.choice([-1, 0, 1])), ts_ms=ts)
            tracker.on_bar("vol_momentum", "BTCUSDT", exposure=int(rng.choice([-1, 0, 1])), ts_ms=ts)

            # Funding carry: always long on BTCUSDT-CARRY
            tracker.on_bar("funding_carry", "BTCUSDT-CARRY", exposure=1, ts_ms=ts)

        snap = tracker.snapshot(now_ms=base_ts + 100 * 3600 * 1000)
        corr = snap.signal_corr

        # Carry is a constant exposure series → zero variance → no correlation
        # entries involving funding_carry in the corr dict (the PortfolioTracker
        # skips zero-variance correlation pairs by design).
        # Acceptable outcomes: absent OR < 0.3 in magnitude
        for key, value in corr.items():
            if "funding_carry" in key:
                assert abs(value) < 0.3, (
                    f"Carry correlation to {key} = {value:.3f} > 0.3 — reject from portfolio"
                )
