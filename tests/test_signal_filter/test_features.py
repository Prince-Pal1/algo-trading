"""Tests for src/m3s/signal_filter/features.py — Phase 0 feature builder."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.m3s.signal_filter.features import (
    FEATURE_KEYS,
    build_meta_features,
)
from src.m3s.types import PortfolioSnapshot, StrategySnapshot
from src.utils.types import Signal, SignalAction


def _signal(
    action: SignalAction = SignalAction.LONG,
    confidence: float = 0.8,
    timestamp: int = 1_700_000_000_000,  # 2023-11-14 22:13 UTC
    entry_price: float = 100.0,
) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        action=action,
        confidence=confidence,
        strategy_name="test_strat",
        timeframe="1h",
        entry_price=entry_price,
        stop_loss=95.0,
        take_profit=110.0,
        risk_pct=0.01,
        timestamp=timestamp,
    )


def _feat(**cols) -> pd.Series:
    """Build a features Series with given columns. Always includes close=100."""
    base = {"close": 100.0}
    base.update(cols)
    return pd.Series(base)


def _snap(equity: float = 10_000.0, drawdown: float = 0.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts_ms=1_700_000_000_000,
        equity=equity,
        hwm=equity / (1 - drawdown) if drawdown > 0 else equity,
        drawdown_pct=drawdown,
        per_strategy={
            "test_strat": StrategySnapshot(
                name="test_strat",
                n_trades_30d=0,
                rolling_sharpe_30d=0.0,
                realized_vol_30d=0.0,
                pnl_30d=0.0,
            ),
        },
        signal_corr={},
    )


# ══════════════════════════════════════════════════════════════════════
# FEATURE_KEYS contract
# ══════════════════════════════════════════════════════════════════════


class TestFeatureKeys:
    def test_keys_are_stable_list(self):
        """FEATURE_KEYS must contain all expected keys (Phase 0 v1 + Session 22 enrichment)."""
        expected = {
            # Phase 0 v1 (15 keys)
            "signal_direction", "signal_confidence",
            "atr_14_pct", "bb_width_pct", "rsi_14", "close_over_ema_21",
            "adx_14",
            "hour_of_day_sin", "hour_of_day_cos", "day_of_week",
            "portfolio_equity", "portfolio_drawdown_pct",
            "portfolio_hwm", "n_strategies_tracked",
            "entry_price_ref",
            # Session 22 sprint enrichment (9 keys → 24 total)
            "volume_zscore_20", "macd_hist_zscore_50",
            "vol_regime_idx", "funding_rate_abs",
            "strategy_win_rate_last_20", "strategy_pnl_z_last_20",
            "hours_since_last_signal", "bars_since_last_trade_close",
            "m3s_alloc_weight_now",
        }
        assert set(FEATURE_KEYS) == expected
        # No duplicates
        assert len(FEATURE_KEYS) == len(set(FEATURE_KEYS))

    def test_build_always_returns_all_keys(self):
        """Every key in FEATURE_KEYS must appear in the output dict."""
        result = build_meta_features(_signal(), _feat())
        assert set(result.keys()) == set(FEATURE_KEYS)


# ══════════════════════════════════════════════════════════════════════
# Signal-derived features
# ══════════════════════════════════════════════════════════════════════


class TestSignalDerived:
    def test_long_direction(self):
        r = build_meta_features(_signal(action=SignalAction.LONG), _feat())
        assert r["signal_direction"] == 1.0

    def test_short_direction(self):
        r = build_meta_features(_signal(action=SignalAction.SHORT), _feat())
        assert r["signal_direction"] == -1.0

    def test_close_direction(self):
        r = build_meta_features(_signal(action=SignalAction.CLOSE), _feat())
        assert r["signal_direction"] == 0.0

    def test_hold_direction(self):
        r = build_meta_features(_signal(action=SignalAction.HOLD), _feat())
        assert r["signal_direction"] is None

    def test_confidence(self):
        r = build_meta_features(_signal(confidence=0.75), _feat())
        assert r["signal_confidence"] == 0.75


# ══════════════════════════════════════════════════════════════════════
# Microstructure features
# ══════════════════════════════════════════════════════════════════════


class TestMicrostructure:
    def test_atr_14_pct(self):
        r = build_meta_features(_signal(), _feat(close=100.0, ATR_14=2.5))
        assert r["atr_14_pct"] == pytest.approx(0.025)

    def test_bb_width_pct(self):
        r = build_meta_features(_signal(), _feat(close=100.0, BBU_20=110.0, BBL_20=95.0))
        assert r["bb_width_pct"] == pytest.approx(0.15)

    def test_rsi_14(self):
        r = build_meta_features(_signal(), _feat(RSI_14=45.5))
        assert r["rsi_14"] == 45.5

    def test_close_over_ema_21(self):
        r = build_meta_features(_signal(), _feat(close=105.0, EMA_21=100.0))
        assert r["close_over_ema_21"] == pytest.approx(0.05)

    def test_missing_indicator_returns_none(self):
        r = build_meta_features(_signal(), _feat())  # no indicators
        assert r["rsi_14"] is None
        assert r["atr_14_pct"] is None
        assert r["bb_width_pct"] is None

    def test_nan_indicator_returns_none(self):
        r = build_meta_features(_signal(), _feat(RSI_14=float("nan")))
        assert r["rsi_14"] is None

    def test_missing_features_series(self):
        r = build_meta_features(_signal(), None)
        assert r["atr_14_pct"] is None
        assert r["rsi_14"] is None
        # Signal-derived features should still be populated
        assert r["signal_direction"] == 1.0


# ══════════════════════════════════════════════════════════════════════
# Time-of-day features
# ══════════════════════════════════════════════════════════════════════


class TestTimeOfDay:
    def test_hour_of_day_encoding(self):
        # 22:13 UTC on 2023-11-14
        r = build_meta_features(
            _signal(timestamp=1_700_000_000_000), _feat(),
        )
        # Cyclic encoding of hour 22
        expected_sin = math.sin(2 * math.pi * 22 / 24.0)
        expected_cos = math.cos(2 * math.pi * 22 / 24.0)
        assert r["hour_of_day_sin"] == pytest.approx(expected_sin)
        assert r["hour_of_day_cos"] == pytest.approx(expected_cos)

    def test_day_of_week(self):
        # 2023-11-14 is a Tuesday → weekday() == 1
        r = build_meta_features(
            _signal(timestamp=1_700_000_000_000), _feat(),
        )
        assert r["day_of_week"] == 1.0

    def test_zero_timestamp_returns_none(self):
        r = build_meta_features(_signal(timestamp=0), _feat())
        assert r["hour_of_day_sin"] is None
        assert r["hour_of_day_cos"] is None


# ══════════════════════════════════════════════════════════════════════
# Book-level features
# ══════════════════════════════════════════════════════════════════════


class TestBookLevel:
    def test_without_snapshot(self):
        r = build_meta_features(_signal(), _feat(), snapshot=None)
        assert r["portfolio_equity"] is None
        assert r["portfolio_drawdown_pct"] is None
        assert r["portfolio_hwm"] is None
        assert r["n_strategies_tracked"] is None

    def test_with_snapshot(self):
        r = build_meta_features(_signal(), _feat(), snapshot=_snap(equity=10_500.0))
        assert r["portfolio_equity"] == 10_500.0
        assert r["portfolio_hwm"] == 10_500.0
        assert r["n_strategies_tracked"] == 1.0

    def test_drawdown(self):
        r = build_meta_features(_signal(), _feat(),
                                snapshot=_snap(equity=9_500.0, drawdown=0.05))
        assert r["portfolio_drawdown_pct"] == pytest.approx(0.05)
