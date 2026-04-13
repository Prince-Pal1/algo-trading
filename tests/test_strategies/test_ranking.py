"""Tests for src/strategies/ranking.py — B.2 implementation gate."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.strategies.ranking import (
    ClenowScore,
    RankCache,
    compute_clenow_score,
    rank_weights_from_scores,
    write_rank_cache_parquet,
)


_MS_PER_DAY = 86_400_000


# ══════════════════════════════════════════════════════════════════════
# compute_clenow_score
# ══════════════════════════════════════════════════════════════════════


class TestClenowScore:
    def test_insufficient_data_returns_zero(self):
        prices = np.array([100.0, 101.0, 102.0])
        score = compute_clenow_score(prices, trend_ma_window=100)
        assert score.score == 0.0
        assert score.n_points == 3

    def test_steady_uptrend_positive_score(self):
        """A perfectly linear log uptrend should give high R² and positive slope."""
        n = 120
        # Perfect exponential growth: price = 100 × exp(0.001 × t)
        prices = np.array([100.0 * math.exp(0.001 * t) for t in range(n)])
        score = compute_clenow_score(prices, annualization_factor=365.0)
        assert score.annualized_slope > 0.0
        assert score.r_squared > 0.99   # near-perfect fit
        assert score.score > 0.0
        assert score.passes_trend_filter
        assert score.passes_gap_filter

    def test_downtrend_negative_slope(self):
        n = 120
        prices = np.array([100.0 * math.exp(-0.001 * t) for t in range(n)])
        score = compute_clenow_score(prices)
        assert score.annualized_slope < 0.0
        assert score.r_squared > 0.99
        assert score.score < 0.0

    def test_noisy_trend_r_squared_drops(self):
        n = 120
        rng = np.random.default_rng(42)
        # Small upward drift + large noise
        prices = np.array([
            100.0 * math.exp(0.001 * t) * (1 + rng.normal(0, 0.02))
            for t in range(n)
        ])
        score = compute_clenow_score(prices)
        # Still positive slope but R² should be well below 1.0
        assert score.annualized_slope > 0.0
        assert 0.0 <= score.r_squared < 0.9

    def test_gap_filter_catches_large_single_day_move(self):
        n = 120
        prices = np.linspace(100.0, 110.0, n)
        prices[60] = prices[59] * 1.25   # 25% single-day spike
        score = compute_clenow_score(prices, gap_filter_pct=0.15)
        assert not score.passes_gap_filter

    def test_trend_filter_catches_below_ma(self):
        n = 120
        # Price rises then drops below the 100-MA at the end
        prices = np.concatenate([
            np.linspace(100, 150, 80),
            np.linspace(150, 90, 40),  # ends below the early MA
        ])
        score = compute_clenow_score(prices, trend_ma_window=100)
        # Current price (90) < mean of last 100 (~120) → trend filter fails
        assert not score.passes_trend_filter


# ══════════════════════════════════════════════════════════════════════
# rank_weights_from_scores
# ══════════════════════════════════════════════════════════════════════


class TestRankWeights:
    def test_inverse_rank_weights_sum_to_one(self):
        scores = {f"SYM{i}": float(i) for i in range(1, 21)}
        out = rank_weights_from_scores(scores, top_n=10, weighting="inverse_rank")
        top = [k for k, v in out.items() if v[2]]
        assert len(top) == 10
        total_weight = sum(v[1] for v in out.values() if v[2])
        assert total_weight == pytest.approx(1.0, abs=1e-9)

    def test_inverse_rank_highest_score_gets_most_weight(self):
        scores = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}
        out = rank_weights_from_scores(scores, top_n=4, weighting="inverse_rank")
        assert out["D"][0] == 1   # highest score = rank 1
        assert out["A"][0] == 4   # lowest score = rank 4
        assert out["D"][1] > out["A"][1]

    def test_equal_weights(self):
        scores = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}
        out = rank_weights_from_scores(scores, top_n=2, weighting="equal")
        top = [k for k, v in out.items() if v[2]]
        assert len(top) == 2
        for k in top:
            assert out[k][1] == pytest.approx(0.5)

    def test_negative_scores_excluded_from_top_n(self):
        scores = {"A": 1.0, "B": -2.0, "C": 3.0}
        out = rank_weights_from_scores(scores, top_n=3)
        assert out["B"][2] is False   # negative score → not in top-N
        assert out["A"][2] is True
        assert out["C"][2] is True

    def test_top_n_larger_than_positive_set(self):
        scores = {"A": 1.0, "B": 2.0}
        out = rank_weights_from_scores(scores, top_n=10)
        # Only 2 positive scores → top-N has exactly 2
        top = [k for k, v in out.items() if v[2]]
        assert len(top) == 2


# ══════════════════════════════════════════════════════════════════════
# RankCache
# ══════════════════════════════════════════════════════════════════════


def _sample_cache() -> RankCache:
    records = [
        # First rebalance: BTC=rank1, ETH=rank2, SOL=rank3 (top-3), DOGE out
        {"timestamp": 1_000_000, "symbol": "BTCUSDT", "score": 0.50, "rank": 1, "weight": 0.5, "in_top_n": True},
        {"timestamp": 1_000_000, "symbol": "ETHUSDT", "score": 0.40, "rank": 2, "weight": 0.33, "in_top_n": True},
        {"timestamp": 1_000_000, "symbol": "SOLUSDT", "score": 0.30, "rank": 3, "weight": 0.17, "in_top_n": True},
        {"timestamp": 1_000_000, "symbol": "DOGEUSDT", "score": -0.10, "rank": 4, "weight": 0.0, "in_top_n": False},
        # Second rebalance: SOL=rank1, BTC=rank2, DOGE=rank3, ETH out
        {"timestamp": 2_000_000, "symbol": "SOLUSDT", "score": 0.60, "rank": 1, "weight": 0.5, "in_top_n": True},
        {"timestamp": 2_000_000, "symbol": "BTCUSDT", "score": 0.45, "rank": 2, "weight": 0.33, "in_top_n": True},
        {"timestamp": 2_000_000, "symbol": "DOGEUSDT", "score": 0.20, "rank": 3, "weight": 0.17, "in_top_n": True},
        {"timestamp": 2_000_000, "symbol": "ETHUSDT", "score": -0.05, "rank": 4, "weight": 0.0, "in_top_n": False},
    ]
    return RankCache.from_records(records)


class TestRankCacheQueries:
    def test_latest_rebalance_before_exact(self):
        cache = _sample_cache()
        assert cache.latest_rebalance_before(1_000_000) == 1_000_000
        assert cache.latest_rebalance_before(1_500_000) == 1_000_000
        assert cache.latest_rebalance_before(2_500_000) == 2_000_000

    def test_latest_rebalance_before_first(self):
        cache = _sample_cache()
        assert cache.latest_rebalance_before(500_000) is None

    def test_get_weight_in_top_n(self):
        cache = _sample_cache()
        assert cache.get_weight(1_500_000, "BTCUSDT") == pytest.approx(0.5)
        assert cache.get_weight(1_500_000, "ETHUSDT") == pytest.approx(0.33)

    def test_get_weight_not_in_top_n(self):
        cache = _sample_cache()
        assert cache.get_weight(1_500_000, "DOGEUSDT") == 0.0

    def test_get_weight_unknown_symbol(self):
        cache = _sample_cache()
        assert cache.get_weight(1_500_000, "UNKNOWN") == 0.0

    def test_in_top_n_bool(self):
        cache = _sample_cache()
        assert cache.in_top_n(1_500_000, "BTCUSDT")
        assert not cache.in_top_n(1_500_000, "DOGEUSDT")

    def test_top_n_at_returns_ordered(self):
        cache = _sample_cache()
        top = cache.top_n_at(1_000_000)
        assert top == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_second_rebalance_different_top_n(self):
        cache = _sample_cache()
        # Before 2nd rebalance: BTC is rank 1
        assert cache.get_weight(1_999_999, "BTCUSDT") == pytest.approx(0.5)
        # At 2nd rebalance: SOL becomes rank 1, BTC drops to rank 2
        assert cache.get_weight(2_000_001, "SOLUSDT") == pytest.approx(0.5)
        assert cache.get_weight(2_000_001, "BTCUSDT") == pytest.approx(0.33)
        # ETH falls out of top-N
        assert cache.get_weight(2_000_001, "ETHUSDT") == 0.0

    def test_score_at_exact_match(self):
        cache = _sample_cache()
        assert cache.score_at(1_000_000, "BTCUSDT") == pytest.approx(0.50)
        assert cache.score_at(2_000_000, "SOLUSDT") == pytest.approx(0.60)

    def test_rebalance_timestamps(self):
        cache = _sample_cache()
        assert cache.n_rebalances() == 2
        assert cache.rebalance_timestamps() == [1_000_000, 2_000_000]


class TestRankCacheValidation:
    def test_missing_columns_raises(self):
        bad_df = pd.DataFrame({"timestamp": [1], "symbol": ["BTCUSDT"]})
        with pytest.raises(ValueError, match="missing columns"):
            RankCache(bad_df)


class TestRankCacheParquetRoundtrip:
    def test_write_and_read(self, tmp_path):
        records = [
            {"timestamp": 1_000, "symbol": "BTCUSDT", "score": 0.5, "rank": 1, "weight": 1.0, "in_top_n": True},
            {"timestamp": 1_000, "symbol": "ETHUSDT", "score": 0.3, "rank": 2, "weight": 0.0, "in_top_n": False},
        ]
        path = tmp_path / "rank_cache_test.parquet"
        write_rank_cache_parquet(records, path)
        assert path.exists()

        cache = RankCache.from_parquet(path)
        assert cache.n_rebalances() == 1
        assert cache.get_weight(1_000, "BTCUSDT") == 1.0

    def test_from_parquet_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            RankCache.from_parquet(tmp_path / "nonexistent.parquet")
