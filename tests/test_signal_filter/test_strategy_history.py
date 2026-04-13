"""Tests for StrategyHistoryCache — in-process ring buffer for meta-label features."""

from __future__ import annotations

import pytest

from src.m3s.signal_filter.strategy_history import (
    StrategyHistoryCache,
    StrategyStats,
)


_MS_HOUR = 3_600_000


class TestStrategyHistoryCache:
    def test_empty_cache_returns_all_none(self):
        cache = StrategyHistoryCache(window=20)
        stats = cache.stats_for("vol_momentum", now_ms=1_000_000)
        assert stats.strategy_win_rate_last_20 is None
        assert stats.strategy_pnl_z_last_20 is None
        assert stats.hours_since_last_signal is None
        assert stats.bars_since_last_trade_close is None

    def test_record_signal_populates_time_gap(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_signal("vol_momentum", ts_ms=1_000_000)
        stats = cache.stats_for("vol_momentum", now_ms=1_000_000 + 5 * _MS_HOUR)
        assert stats.hours_since_last_signal == pytest.approx(5.0, rel=1e-6)

    def test_record_close_populates_win_rate_and_pnl_z(self):
        cache = StrategyHistoryCache(window=20)
        # Deterministic pnl sequence: 3 wins, 2 losses.
        pnls = [10.0, -5.0, 12.0, -3.0, 8.0]
        labels = [1, 0, 1, 0, 1]
        for i, (p, l) in enumerate(zip(pnls, labels)):
            cache.record_close("vol_momentum", pnl=p, meta_label=l, ts_ms=1_000_000 + i * _MS_HOUR)

        stats = cache.stats_for("vol_momentum", now_ms=1_000_000 + 10 * _MS_HOUR)
        # Win rate: 3/5 = 0.6
        assert stats.strategy_win_rate_last_20 == pytest.approx(0.6, rel=1e-6)
        # Pnl z should be defined (std > 0, n >= 3)
        assert stats.strategy_pnl_z_last_20 is not None
        assert abs(stats.strategy_pnl_z_last_20) < 5  # sanity range

    def test_ring_buffer_respects_window(self):
        cache = StrategyHistoryCache(window=3)
        for i in range(5):
            cache.record_close("vol_momentum", pnl=float(i), meta_label=1, ts_ms=i * _MS_HOUR)
        # Only the last 3 should be retained
        assert cache.n_recorded("vol_momentum") == 3

    def test_bars_since_last_close(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_close("vol_momentum", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        stats = cache.stats_for("vol_momentum", now_ms=1_000_000 + 3 * _MS_HOUR)
        assert stats.bars_since_last_trade_close == pytest.approx(3.0, rel=1e-6)

    def test_pnl_z_needs_three_samples(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_close("vol_momentum", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        cache.record_close("vol_momentum", pnl=2.0, meta_label=1, ts_ms=2_000_000)
        stats = cache.stats_for("vol_momentum", now_ms=3_000_000)
        assert stats.strategy_pnl_z_last_20 is None  # need ≥ 3 for variance
        # But win rate is defined with any number of samples
        assert stats.strategy_win_rate_last_20 is not None

    def test_zero_variance_pnl_z_is_none(self):
        cache = StrategyHistoryCache(window=20)
        for i in range(5):
            cache.record_close("vol_momentum", pnl=5.0, meta_label=1, ts_ms=i * _MS_HOUR)
        stats = cache.stats_for("vol_momentum", now_ms=10 * _MS_HOUR)
        # std == 0 → z undefined
        assert stats.strategy_pnl_z_last_20 is None

    def test_strategy_isolation(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_close("vol_momentum", pnl=10.0, meta_label=1, ts_ms=1_000_000)
        cache.record_close("bb_rsi_mr", pnl=-5.0, meta_label=0, ts_ms=1_000_000)
        stats_vm = cache.stats_for("vol_momentum", now_ms=2_000_000)
        stats_bb = cache.stats_for("bb_rsi_mr", now_ms=2_000_000)
        assert stats_vm.strategy_win_rate_last_20 == 1.0
        assert stats_bb.strategy_win_rate_last_20 == 0.0

    def test_empty_strategy_name_is_noop(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_signal("", ts_ms=1_000_000)
        cache.record_close("", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        stats = cache.stats_for("", now_ms=2_000_000)
        assert stats.strategy_win_rate_last_20 is None

    def test_reset_one_strategy(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_close("vol_momentum", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        cache.record_close("bb_rsi_mr", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        cache.reset("vol_momentum")
        assert cache.n_recorded("vol_momentum") == 0
        assert cache.n_recorded("bb_rsi_mr") == 1

    def test_reset_all(self):
        cache = StrategyHistoryCache(window=20)
        cache.record_close("vol_momentum", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        cache.record_close("bb_rsi_mr", pnl=1.0, meta_label=1, ts_ms=1_000_000)
        cache.reset()
        assert cache.n_recorded("vol_momentum") == 0
        assert cache.n_recorded("bb_rsi_mr") == 0

    def test_window_must_be_positive(self):
        with pytest.raises(ValueError):
            StrategyHistoryCache(window=0)
