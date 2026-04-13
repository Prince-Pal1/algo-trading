"""Sub-phase 0.2 — tests for src/m3s/portfolio.py PortfolioTracker."""

from __future__ import annotations

import math

import pytest

from src.m3s.portfolio import PortfolioTracker
from src.m3s.types import PortfolioSnapshot, StrategySnapshot


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_700_000_000_000  # 2023-11-14 ~22:13 UTC, arbitrary anchor


def _ts(day_offset: int) -> int:
    return _BASE_TS + day_offset * _MS_PER_DAY


class TestEquityAndHWM:
    def test_initial_state(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        assert pt.equity == 10_000.0
        assert pt.hwm == 10_000.0
        assert pt.drawdown_pct == 0.0

    def test_positive_trade_raises_equity_and_hwm(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("a", pnl=120.0, symbol="BTCUSDT", ts_ms=_ts(0))
        assert pt.equity == pytest.approx(10_120.0)
        assert pt.hwm == pytest.approx(10_120.0)
        assert pt.drawdown_pct == 0.0

    def test_negative_trade_drops_equity_keeps_hwm(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("a", pnl=200.0, symbol="BTCUSDT", ts_ms=_ts(0))  # hwm=10200
        pt.on_trade_close("a", pnl=-400.0, symbol="BTCUSDT", ts_ms=_ts(1))  # eq=9800
        assert pt.equity == pytest.approx(9_800.0)
        assert pt.hwm == pytest.approx(10_200.0)
        assert pt.drawdown_pct == pytest.approx(400 / 10_200, rel=1e-6)

    def test_hwm_only_ratchets_up(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("a", pnl=500.0, symbol="BTCUSDT", ts_ms=_ts(0))
        pt.on_trade_close("a", pnl=-300.0, symbol="BTCUSDT", ts_ms=_ts(1))
        pt.on_trade_close("a", pnl=100.0, symbol="BTCUSDT", ts_ms=_ts(2))
        # Equity = 10300, HWM = 10500 (set by first trade)
        assert pt.equity == pytest.approx(10_300.0)
        assert pt.hwm == pytest.approx(10_500.0)

    def test_update_equity_from_executor(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.update_equity_from_executor(11_500.0, ts_ms=_ts(0))
        assert pt.equity == pytest.approx(11_500.0)
        assert pt.hwm == pytest.approx(11_500.0)


class TestSnapshotBasic:
    def test_empty_snapshot_is_frozen(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        snap = pt.snapshot(now_ms=_ts(0))
        assert isinstance(snap, PortfolioSnapshot)
        assert snap.equity == 10_000.0
        assert snap.hwm == 10_000.0
        assert snap.drawdown_pct == 0.0
        assert snap.per_strategy == {}
        assert snap.signal_corr == {}
        # Frozen struct — mutation raises
        with pytest.raises((AttributeError, TypeError)):
            snap.equity = 0.0  # type: ignore[misc]

    def test_snapshot_cold_start_strategy(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        # Bar exposure alone shouldn't crash snapshot or create a strategy
        # row with bogus trade stats.
        pt.on_bar("bb_rsi_mr_opt", "BTCUSDT", exposure=1, ts_ms=_ts(0))
        snap = pt.snapshot(now_ms=_ts(0))
        assert "bb_rsi_mr_opt" in snap.per_strategy
        sstat = snap.per_strategy["bb_rsi_mr_opt"]
        assert isinstance(sstat, StrategySnapshot)
        assert sstat.n_trades_30d == 0
        assert sstat.rolling_sharpe_30d == 0.0
        assert sstat.realized_vol_30d == 0.0
        assert sstat.pnl_30d == 0.0

    def test_snapshot_counts_trades_in_window(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        # 5 trades at days 0, 10, 20, 30, 40
        for i, pnl in enumerate([50.0, -20.0, 30.0, 10.0, -5.0]):
            pt.on_trade_close("a", pnl=pnl, symbol="BTCUSDT", ts_ms=_ts(i * 10))
        now = _ts(45)
        snap = pt.snapshot(now_ms=now)
        # Window default 30 days, cutoff at day 15: only days 20, 30, 40 qualify
        sstat = snap.per_strategy["a"]
        assert sstat.n_trades_30d == 3
        assert sstat.pnl_30d == pytest.approx(30.0 + 10.0 + -5.0)

    def test_snapshot_drawdown_from_peak(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("a", pnl=1000.0, symbol="BTCUSDT", ts_ms=_ts(0))
        pt.on_trade_close("a", pnl=-2000.0, symbol="BTCUSDT", ts_ms=_ts(1))
        snap = pt.snapshot(now_ms=_ts(1))
        # HWM=11000, eq=9000 → dd = 2000/11000
        assert snap.drawdown_pct == pytest.approx(2000 / 11_000, rel=1e-6)
        assert snap.equity == pytest.approx(9_000.0)


class TestRollingStats:
    def test_rolling_sharpe_positive_streak(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        # Consistent daily gains for 30 days — should produce positive Sharpe
        for day in range(30):
            pt.on_trade_close("a", pnl=100.0, symbol="BTCUSDT", ts_ms=_ts(day))
        snap = pt.snapshot(now_ms=_ts(29))
        sstat = snap.per_strategy["a"]
        # All 30 days have +100, zero variance → our tracker returns 0.0 (not inf)
        assert sstat.rolling_sharpe_30d == 0.0
        # pnl_30d should reflect all 30 trades (cutoff is day 29 - 30 = day -1)
        assert sstat.pnl_30d == pytest.approx(3000.0)
        assert sstat.n_trades_30d == 30

    def test_rolling_sharpe_with_variance(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        # Alternating +100 / -50 daily — positive mean, nonzero std
        for day in range(30):
            pnl = 100.0 if day % 2 == 0 else -50.0
            pt.on_trade_close("a", pnl=pnl, symbol="BTCUSDT", ts_ms=_ts(day))
        snap = pt.snapshot(now_ms=_ts(29))
        sstat = snap.per_strategy["a"]
        # Positive mean return + real variance → positive Sharpe, annualized
        assert sstat.rolling_sharpe_30d > 0.0
        assert sstat.realized_vol_30d > 0.0
        # sanity: annualized vol on crypto is a meaningful percentage (not raw)
        assert sstat.realized_vol_30d < 10.0  # not insane

    def test_rolling_window_excludes_older_trades(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        # One big winner 60 days ago, nothing recent
        pt.on_trade_close("a", pnl=5000.0, symbol="BTCUSDT", ts_ms=_ts(0))
        pt.on_trade_close("a", pnl=10.0, symbol="BTCUSDT", ts_ms=_ts(55))
        snap = pt.snapshot(now_ms=_ts(60))
        sstat = snap.per_strategy["a"]
        # Only the day-55 trade is within the 30-day window (cutoff day 30)
        assert sstat.n_trades_30d == 1
        assert sstat.pnl_30d == pytest.approx(10.0)

    def test_lifetime_winrate(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pnls = [100.0, -50.0, 75.0, -30.0, 200.0]  # 3 wins of 5
        for i, p in enumerate(pnls):
            pt.on_trade_close("a", pnl=p, symbol="BTCUSDT", ts_ms=_ts(i))
        snap = pt.snapshot(now_ms=_ts(5))
        sstat = snap.per_strategy["a"]
        assert sstat.lifetime_winrate == pytest.approx(0.6)

    def test_lifetime_sharpe_with_history(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        for i in range(10):
            pt.on_trade_close("a", pnl=50.0 + i, symbol="BTCUSDT", ts_ms=_ts(i))
        snap = pt.snapshot(now_ms=_ts(10))
        sstat = snap.per_strategy["a"]
        # Positive mean, nonzero std → positive lifetime sharpe
        assert sstat.lifetime_sharpe > 0.0
        assert math.isfinite(sstat.lifetime_sharpe)


class TestExposureAndCorrelation:
    def test_invalid_exposure_raises(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        with pytest.raises(ValueError, match="exposure"):
            pt.on_bar("a", "BTCUSDT", exposure=2, ts_ms=_ts(0))

    def test_signal_corr_empty_when_single_strategy(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        for i in range(10):
            pt.on_bar("a", "BTCUSDT", exposure=1, ts_ms=_ts(0) + i * 1000)
        snap = pt.snapshot(now_ms=_ts(0) + 10_000)
        assert snap.signal_corr == {}

    def test_signal_corr_perfect_positive(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        # Two strategies, same exposure at same timestamps
        for i in range(20):
            ts = _ts(0) + i * 3600_000
            sign = 1 if i % 2 == 0 else -1
            pt.on_bar("a", "BTCUSDT", exposure=sign, ts_ms=ts)
            pt.on_bar("b", "BTCUSDT", exposure=sign, ts_ms=ts)
        snap = pt.snapshot(now_ms=_ts(0) + 20 * 3600_000)
        # Pair key is sorted alphabetically
        assert "a|b" in snap.signal_corr
        assert snap.signal_corr["a|b"] == pytest.approx(1.0, abs=1e-9)

    def test_signal_corr_perfect_negative(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        for i in range(20):
            ts = _ts(0) + i * 3600_000
            sign = 1 if i % 2 == 0 else -1
            pt.on_bar("a", "BTCUSDT", exposure=sign, ts_ms=ts)
            pt.on_bar("b", "BTCUSDT", exposure=-sign, ts_ms=ts)
        snap = pt.snapshot(now_ms=_ts(0) + 20 * 3600_000)
        assert snap.signal_corr["a|b"] == pytest.approx(-1.0, abs=1e-9)

    def test_signal_corr_constant_series_skipped(self):
        """Zero-variance series returns None → no key in dict."""
        pt = PortfolioTracker(initial_equity=10_000.0)
        # a always flat, b alternates — a has zero variance
        for i in range(20):
            ts = _ts(0) + i * 3600_000
            pt.on_bar("a", "BTCUSDT", exposure=0, ts_ms=ts)
            pt.on_bar("b", "BTCUSDT", exposure=(1 if i % 2 == 0 else -1), ts_ms=ts)
        snap = pt.snapshot(now_ms=_ts(0) + 20 * 3600_000)
        assert "a|b" not in snap.signal_corr


class TestMultiStrategyIsolation:
    def test_trades_do_not_bleed_across_strategies(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("a", pnl=300.0, symbol="BTCUSDT", ts_ms=_ts(0))
        pt.on_trade_close("b", pnl=-100.0, symbol="ETHUSDT", ts_ms=_ts(0))
        snap = pt.snapshot(now_ms=_ts(0))
        # Portfolio equity is the sum
        assert snap.equity == pytest.approx(10_200.0)
        # Per-strategy pnl attribution is isolated
        assert snap.per_strategy["a"].pnl_30d == pytest.approx(300.0)
        assert snap.per_strategy["b"].pnl_30d == pytest.approx(-100.0)
        assert snap.per_strategy["a"].n_trades_30d == 1
        assert snap.per_strategy["b"].n_trades_30d == 1

    def test_strategy_names_sorted(self):
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("zebra", pnl=10.0, symbol="BTCUSDT", ts_ms=_ts(0))
        pt.on_trade_close("apple", pnl=10.0, symbol="BTCUSDT", ts_ms=_ts(0))
        pt.on_trade_close("mango", pnl=10.0, symbol="BTCUSDT", ts_ms=_ts(0))
        assert pt.strategy_names() == ["apple", "mango", "zebra"]
