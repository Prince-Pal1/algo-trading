"""Tests for ClenowMomentumStrategy — B.3 implementation gate."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.momentum.clenow_momentum import ClenowMomentumStrategy
from src.strategies.ranking import RankCache
from src.utils.types import RiskProfile, SignalAction


_MS_PER_DAY = 86_400_000


def _cache_with(records: list[dict]) -> RankCache:
    return RankCache.from_records(records)


def _strat(cache: RankCache, markets: list[str] | None = None) -> ClenowMomentumStrategy:
    return ClenowMomentumStrategy(
        name="clenow_momentum",
        markets=markets or ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        timeframe="1d",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.01,
        rank_cache=cache,
        cooldown_bars=0,
    )


def _feat(close: float, ts_ms: int) -> pd.Series:
    return pd.Series({"close": close, "timestamp": ts_ms})


# ══════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════


class TestValidation:
    def test_requires_cache_or_path(self):
        with pytest.raises(ValueError, match="rank_cache"):
            ClenowMomentumStrategy(
                name="clenow_momentum",
                markets=["BTCUSDT"],
                timeframe="1d",
            )

    def test_accepts_in_memory_cache(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 1.0, "in_top_n": True},
        ])
        s = _strat(cache)
        assert s._rank_cache.n_rebalances() == 1

    def test_negative_cooldown_rejected(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 1.0, "in_top_n": True},
        ])
        with pytest.raises(ValueError, match="cooldown_bars"):
            ClenowMomentumStrategy(
                name="clenow_momentum",
                markets=["BTCUSDT"],
                timeframe="1d",
                rank_cache=cache,
                cooldown_bars=-1,
            )


# ══════════════════════════════════════════════════════════════════════
# Entry / exit behavior
# ══════════════════════════════════════════════════════════════════════


class TestEntryExit:
    def test_enter_when_in_top_n(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 0.5, "in_top_n": True},
        ])
        s = _strat(cache)
        sig = s.process("BTCUSDT", "1d", _feat(close=50_000, ts_ms=2_000))
        assert sig is not None
        assert sig.action == SignalAction.LONG
        # Rank weight scales risk_pct: 0.01 × 0.5 = 0.005
        assert sig.risk_pct == pytest.approx(0.005)
        assert sig.metadata["rank_weight"] == pytest.approx(0.5)
        assert s._position == "LONG"

    def test_no_entry_when_not_in_top_n(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 0.5, "in_top_n": True},
            {"timestamp": 1_000, "symbol": "ETHUSDT", "score": -0.1,
             "rank": 2, "weight": 0.0, "in_top_n": False},
        ])
        s = _strat(cache)
        sig = s.process("ETHUSDT", "1d", _feat(close=3_000, ts_ms=2_000))
        assert sig is None
        assert s._position == "FLAT"

    def test_exit_when_drops_out_of_top_n(self):
        cache = _cache_with([
            # First rebalance: BTC in top-N
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 0.5, "in_top_n": True},
            # Second rebalance: BTC drops out
            {"timestamp": 2_000, "symbol": "BTCUSDT", "score": -0.1,
             "rank": 10, "weight": 0.0, "in_top_n": False},
        ])
        s = _strat(cache)
        # Enter at ts=1500 (reads rebalance 1000)
        s.process("BTCUSDT", "1d", _feat(close=50_000, ts_ms=1_500))
        assert s._position == "LONG"
        # Next bar at ts=2500 (reads rebalance 2000): BTC out → CLOSE
        sig = s.process("BTCUSDT", "1d", _feat(close=49_000, ts_ms=2_500))
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "rank_drop"
        assert s._position == "FLAT"

    def test_hold_while_still_in_top_n(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 0.5, "in_top_n": True},
            {"timestamp": 2_000, "symbol": "BTCUSDT", "score": 0.6,
             "rank": 1, "weight": 0.5, "in_top_n": True},
        ])
        s = _strat(cache)
        s.process("BTCUSDT", "1d", _feat(close=50_000, ts_ms=1_500))   # LONG
        sig = s.process("BTCUSDT", "1d", _feat(close=51_000, ts_ms=2_500))
        assert sig is None   # still in top-N, hold
        assert s._position == "LONG"


# ══════════════════════════════════════════════════════════════════════
# Rank-weighted sizing
# ══════════════════════════════════════════════════════════════════════


class TestRankWeightedSizing:
    def test_rank_1_gets_highest_risk_pct(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 0.6, "in_top_n": True},
            {"timestamp": 1_000, "symbol": "ETHUSDT", "score": 0.4,
             "rank": 2, "weight": 0.3, "in_top_n": True},
            {"timestamp": 1_000, "symbol": "SOLUSDT", "score": 0.3,
             "rank": 3, "weight": 0.1, "in_top_n": True},
        ])
        btc_strat = _strat(cache, markets=["BTCUSDT"])
        eth_strat = _strat(cache, markets=["ETHUSDT"])
        sol_strat = _strat(cache, markets=["SOLUSDT"])

        sig_btc = btc_strat.process("BTCUSDT", "1d", _feat(close=50_000, ts_ms=2_000))
        sig_eth = eth_strat.process("ETHUSDT", "1d", _feat(close=3_000, ts_ms=2_000))
        sig_sol = sol_strat.process("SOLUSDT", "1d", _feat(close=100, ts_ms=2_000))

        # All three enter, but with different sizing
        assert sig_btc.risk_pct == pytest.approx(0.01 * 0.6)
        assert sig_eth.risk_pct == pytest.approx(0.01 * 0.3)
        assert sig_sol.risk_pct == pytest.approx(0.01 * 0.1)
        # Ordered largest to smallest
        assert sig_btc.risk_pct > sig_eth.risk_pct > sig_sol.risk_pct


# ══════════════════════════════════════════════════════════════════════
# Cooldown (optional)
# ══════════════════════════════════════════════════════════════════════


class TestCooldown:
    def test_no_cooldown_default(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 1.0, "in_top_n": True},
            {"timestamp": 2_000, "symbol": "BTCUSDT", "score": -0.5,
             "rank": 10, "weight": 0.0, "in_top_n": False},
            {"timestamp": 3_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 1.0, "in_top_n": True},
        ])
        s = _strat(cache)
        s.process("BTCUSDT", "1d", _feat(close=50_000, ts_ms=1_500))  # enter
        s.process("BTCUSDT", "1d", _feat(close=49_000, ts_ms=2_500))  # exit
        # Immediate re-entry allowed (cooldown=0)
        sig = s.process("BTCUSDT", "1d", _feat(close=48_000, ts_ms=3_500))
        assert sig is not None
        assert sig.action == SignalAction.LONG

    def test_cooldown_blocks_immediate_reentry(self):
        cache = _cache_with([
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 1.0, "in_top_n": True},
            {"timestamp": 2_000, "symbol": "BTCUSDT", "score": -0.5,
             "rank": 10, "weight": 0.0, "in_top_n": False},
            {"timestamp": 3_000, "symbol": "BTCUSDT", "score": 0.5,
             "rank": 1, "weight": 1.0, "in_top_n": True},
        ])
        s = ClenowMomentumStrategy(
            name="clenow_momentum",
            markets=["BTCUSDT"],
            timeframe="1d",
            rank_cache=cache,
            cooldown_bars=3,
        )
        s.process("BTCUSDT", "1d", _feat(close=50_000, ts_ms=1_500))  # enter
        s.process("BTCUSDT", "1d", _feat(close=49_000, ts_ms=2_500))  # exit, bse=0
        # Next bar: bse=1 → blocked
        sig1 = s.process("BTCUSDT", "1d", _feat(close=48_000, ts_ms=3_500))
        assert sig1 is None


# ══════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_registered(self):
        from src.strategies.router import STRATEGY_REGISTRY
        assert "clenow_momentum" in STRATEGY_REGISTRY
        assert STRATEGY_REGISTRY["clenow_momentum"] is ClenowMomentumStrategy
