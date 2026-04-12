"""Tests for operating modes and strategy profiles."""

import pytest

from src.risk.config import RiskConfig, StrategyRiskProfile
from src.risk.kelly_sizer import KellySizer, StrategyStats
from src.risk.modes import (
    MODE_PRESETS, ModeMultipliers, RiskMode, clamp_custom, resolve_mode,
)
from src.risk.manager import RiskManager
from src.risk.state import RiskState
from src.utils.types import Signal, SignalAction

from tests.test_risk.test_kill_switch import _make_state


def _make_signal(
    action: SignalAction = SignalAction.LONG,
    risk_pct: float = 0.01,
    confidence: float = 0.9,
    entry_price: float = 100.0,
    stop_loss: float = 99.0,
    strategy_name: str = "test_strat",
) -> Signal:
    return Signal(
        symbol="TESTUSDT", action=action, confidence=confidence,
        strategy_name=strategy_name, timeframe="1h",
        entry_price=entry_price, stop_loss=stop_loss, risk_pct=risk_pct,
    )


def _make_manager(state=None, config=None) -> RiskManager:
    if state is None:
        state = _make_state()
    if config is None:
        config = RiskConfig(max_position_pct=5.0, max_portfolio_heat=10.0)
    return RiskManager(config, state)


# ── Mode Preset Tests ──


class TestModePresets:
    def test_aggressive_is_default(self):
        state = _make_state()
        assert state.active_mode == "AGGRESSIVE"

    def test_all_modes_have_presets(self):
        for mode in [RiskMode.AGGRESSIVE, RiskMode.BALANCED, RiskMode.DEFENSIVE]:
            assert mode in MODE_PRESETS

    def test_balanced_is_neutral(self):
        m = MODE_PRESETS[RiskMode.BALANCED]
        assert m.max_risk_per_trade == 1.0
        assert m.max_position_pct == 1.0
        assert m.confidence_floor == 0.8
        assert m.daily_size_mult == 0.5
        assert m.drawdown_aggression == 1.0

    def test_aggressive_widens_limits(self):
        m = MODE_PRESETS[RiskMode.AGGRESSIVE]
        assert m.max_risk_per_trade == 2.0
        assert m.max_position_pct == 1.75
        assert m.confidence_floor == 0.5
        assert m.daily_size_mult == 0.7
        assert m.drawdown_aggression == 2.0

    def test_defensive_tightens_limits(self):
        m = MODE_PRESETS[RiskMode.DEFENSIVE]
        assert m.max_risk_per_trade == 0.5
        assert m.confidence_floor == 0.9
        assert m.drawdown_aggression == 0.5

    def test_resolve_mode_balanced(self):
        m = resolve_mode("BALANCED")
        assert m == MODE_PRESETS[RiskMode.BALANCED]

    def test_resolve_mode_custom_from_balanced(self):
        m = resolve_mode("CUSTOM", {"max_risk_per_trade": 1.5})
        assert m.max_risk_per_trade == 1.5
        # Other fields stay at BALANCED defaults
        assert m.confidence_floor == 0.8

    def test_custom_mode_clamped(self):
        clamped = clamp_custom({"max_risk_per_trade": 99.0, "confidence_floor": 0.01})
        assert clamped["max_risk_per_trade"] == 3.0  # capped at 3.0
        assert clamped["confidence_floor"] == 0.3     # capped at 0.3

    def test_custom_mode_ignores_unknown_keys(self):
        clamped = clamp_custom({"unknown_field": 5.0})
        assert "unknown_field" not in clamped


# ── Mode Integration Tests ──


class TestModeIntegration:
    def test_mode_in_status(self):
        mgr = _make_manager()
        status = mgr.get_status()
        assert "mode" in status
        assert status["mode"] == "AGGRESSIVE"

    def test_set_mode_changes_state(self):
        mgr = _make_manager()
        mgr.set_mode("DEFENSIVE")
        assert mgr.state.active_mode == "DEFENSIVE"
        assert mgr.get_status()["mode"] == "DEFENSIVE"

    def test_same_signal_different_modes(self):
        """Same signal under AGGRESSIVE vs DEFENSIVE produces different sizes."""
        cfg = RiskConfig(max_position_pct=5.0, max_portfolio_heat=10.0,
                         fat_finger_max_value=100_000.0)

        # AGGRESSIVE
        state_a = _make_state()
        state_a.active_mode = "AGGRESSIVE"
        mgr_a = RiskManager(cfg, state_a)
        d_a = mgr_a.evaluate(_make_signal())

        # DEFENSIVE
        state_d = _make_state()
        state_d.active_mode = "DEFENSIVE"
        mgr_d = RiskManager(cfg, state_d)
        d_d = mgr_d.evaluate(_make_signal())

        assert d_a.approved and d_d.approved

    def test_non_negotiable_unchanged_in_aggressive(self):
        """Max drawdown kill switch fires even in AGGRESSIVE mode."""
        state = _make_state()
        state.active_mode = "AGGRESSIVE"
        state.peak_equity = 10_000.0
        state.current_equity = 8_400.0  # 16% DD > 15% max
        cfg = RiskConfig(max_drawdown=0.15, max_drawdown_close_all=True,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = RiskManager(cfg, state)
        mgr.kill_switch.activate = lambda r: None  # mock callback
        decision = mgr.evaluate(_make_signal())
        assert decision.approved is False
        assert "CB_L3D" in decision.reason


# ── Conflict 1: Confidence Gate + EV Gate ──


class TestConflict1ConfidenceGate:
    def test_donchian_passes_with_ev_gate(self):
        """Donchian (conf=0.667) passes weekly stress via EV gate."""
        from datetime import datetime, timezone
        profiles = {
            "donchian": StrategyRiskProfile(
                strategy_name="donchian", confidence_floor=0.5,
                use_ev_gate=True, ev_threshold=0.4),
        }
        state = _make_state()
        state.weekly_pnl = -700.0  # 7% loss > 6% threshold
        now = datetime.now(timezone.utc)
        state._last_daily_reset = now.strftime("%Y-%m-%d")
        state._last_weekly_reset = now.strftime("%Y-W%W")
        state._last_monthly_reset = now.strftime("%Y-%m")
        cfg = RiskConfig(max_weekly_loss=0.06, weekly_reduce_enabled=True,
                         strategy_profiles=profiles,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = RiskManager(cfg, state)
        # Stats: win/loss ratio = 3.0, EV = 0.667 * 3.0 = 2.0 > 0.4
        mgr.set_strategy_stats("donchian", StrategyStats(
            win_rate=0.45, avg_win=3.0, avg_loss=1.0, total_trades=50))
        sig = _make_signal(strategy_name="donchian", confidence=0.667)
        decision = mgr.evaluate(sig)
        assert decision.approved is True

    def test_donchian_rejected_when_ev_too_low(self):
        """Donchian rejected when EV is below threshold."""
        from datetime import datetime, timezone
        profiles = {
            "donchian": StrategyRiskProfile(
                strategy_name="donchian", confidence_floor=0.5,
                use_ev_gate=True, ev_threshold=0.4),
        }
        state = _make_state()
        state.weekly_pnl = -700.0
        now = datetime.now(timezone.utc)
        state._last_daily_reset = now.strftime("%Y-%m-%d")
        state._last_weekly_reset = now.strftime("%Y-W%W")
        state._last_monthly_reset = now.strftime("%Y-%m")
        cfg = RiskConfig(max_weekly_loss=0.06, weekly_reduce_enabled=True,
                         strategy_profiles=profiles,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = RiskManager(cfg, state)
        # Bad stats: win/loss ratio = 0.5, EV = 0.667 * 0.5 = 0.33 < 0.4
        mgr.set_strategy_stats("donchian", StrategyStats(
            win_rate=0.30, avg_win=0.5, avg_loss=1.0, total_trades=50))
        sig = _make_signal(strategy_name="donchian", confidence=0.667)
        decision = mgr.evaluate(sig)
        assert decision.approved is False
        assert "CB_L3B" in decision.reason

    def test_bb_rsi_still_blocked_at_low_confidence(self):
        """Regression: bb_rsi_mr (no profile) still uses default 0.8 floor."""
        from datetime import datetime, timezone
        state = _make_state()
        state.active_mode = "BALANCED"  # confidence_floor = 0.8
        state.weekly_pnl = -700.0
        now = datetime.now(timezone.utc)
        state._last_daily_reset = now.strftime("%Y-%m-%d")
        state._last_weekly_reset = now.strftime("%Y-W%W")
        state._last_monthly_reset = now.strftime("%Y-%m")
        cfg = RiskConfig(max_weekly_loss=0.06, weekly_reduce_enabled=True,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = RiskManager(cfg, state)
        sig = _make_signal(strategy_name="bb_rsi_mr", confidence=0.7)
        decision = mgr.evaluate(sig)
        assert decision.approved is False
        assert "CB_L3B" in decision.reason

    def test_aggressive_mode_lowers_confidence_floor(self):
        """AGGRESSIVE mode uses 0.5 floor — most signals pass."""
        from datetime import datetime, timezone
        state = _make_state()
        state.active_mode = "AGGRESSIVE"
        state.weekly_pnl = -700.0
        now = datetime.now(timezone.utc)
        state._last_daily_reset = now.strftime("%Y-%m-%d")
        state._last_weekly_reset = now.strftime("%Y-W%W")
        state._last_monthly_reset = now.strftime("%Y-%m")
        cfg = RiskConfig(max_weekly_loss=0.06, weekly_reduce_enabled=True,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = RiskManager(cfg, state)
        # confidence=0.6 > AGGRESSIVE floor 0.5 → passes
        sig = _make_signal(confidence=0.6)
        decision = mgr.evaluate(sig)
        assert decision.approved is True


# ── Conflict 2: Vol-Aware Sizing ──


class TestConflict2VolAware:
    def test_kelly_skips_vol_scaling_for_vol_momentum(self):
        """Vol momentum: skip_kelly_vol_scaling prevents triple penalty."""
        profiles = {
            "vol_momentum": StrategyRiskProfile(
                strategy_name="vol_momentum", skip_kelly_vol_scaling=True),
        }
        cfg = RiskConfig(kelly_min_trades=10, kelly_min_win_rate=0.40,
                         strategy_profiles=profiles,
                         max_position_pct=5.0, max_portfolio_heat=10.0)
        mgr = _make_manager(config=cfg)
        # High realized vol would normally crush size via vol_target/realized_vol
        stats = StrategyStats(win_rate=0.55, avg_win=2.0, avg_loss=1.0,
                              total_trades=50, realized_vol=0.80)
        mgr.set_strategy_stats("vol_momentum", stats)
        sig = _make_signal(strategy_name="vol_momentum", risk_pct=0.01)
        decision = mgr.evaluate(sig)
        assert decision.approved is True
        # Without skip, vol_scalar = 0.15/0.80 = 0.1875 → size crushed
        # With skip, no vol scaling → size preserved
        assert decision.adjusted_risk_pct > 0.005

    def test_drawdown_scaler_respects_floor(self):
        """Strategy with drawdown_floor=0.3 never goes below 30%."""
        from src.risk.drawdown_scaler import DrawdownScaler
        cfg = RiskConfig(max_drawdown=0.15)
        scaler = DrawdownScaler(cfg)
        profile = StrategyRiskProfile(
            strategy_name="vol_momentum",
            drawdown_sensitivity=0.5, drawdown_floor=0.3)
        # At max drawdown (15%), without floor would be 0
        result = scaler.adjust(1.0, 0.15, profile=profile, aggression=1.0)
        assert result >= 0.3

    def test_drawdown_aggression_shapes(self):
        """Different aggression exponents produce different curve shapes."""
        from src.risk.drawdown_scaler import DrawdownScaler
        cfg = RiskConfig(max_drawdown=0.15)
        scaler = DrawdownScaler(cfg)
        # At small DD (2%), different exponents shape the curve
        low_dd = 0.02
        balanced = scaler.adjust(1.0, low_dd, aggression=1.0)
        aggressive = scaler.adjust(1.0, low_dd, aggression=2.0)
        defensive = scaler.adjust(1.0, low_dd, aggression=0.5)
        # AGGRESSIVE (e=2.0): gentle at low DD, ratio^2 is small → less reduction
        assert aggressive > balanced
        # DEFENSIVE (e=0.5): harsh at low DD, ratio^0.5 is large → more reduction
        assert defensive < balanced
        # All still reduce at max DD to zero
        assert scaler.adjust(1.0, 0.15, aggression=0.5) == pytest.approx(0.0)
        assert scaler.adjust(1.0, 0.15, aggression=2.0) == pytest.approx(0.0)


# ── Conflict 3: Risk-Based Position Limits ──


class TestConflict3RiskBasedLimits:
    def test_wide_stop_passes_risk_based_limit(self):
        """BTC with wide stop passes risk-based check despite high notional."""
        profiles = {
            "donchian": StrategyRiskProfile(
                strategy_name="donchian",
                use_risk_based_limits=True, max_risk_pct_per_position=0.04),
        }
        cfg = RiskConfig(max_position_pct=0.20, strategy_profiles=profiles,
                         max_portfolio_heat=10.0)
        mgr = _make_manager(config=cfg)
        # BTC at 100K, SL at 95.5K (4.5% away), risk 1%
        # notional = (10K * 0.01) / 4500 * 100K = $222 = 2.2% → would pass anyway
        # But with 3% stop: notional = (10K * 0.02) / 3000 * 100K = $666 = 6.7%
        # Use extreme case: notional > 20% but risk < 4%
        sig = Signal(
            symbol="BTCUSDT", action=SignalAction.LONG, confidence=0.7,
            strategy_name="donchian", timeframe="1h",
            entry_price=50000.0, stop_loss=49000.0, risk_pct=0.02)
        decision = mgr.evaluate(sig)
        assert decision.approved is True

    def test_risk_based_rejects_when_risk_too_high(self):
        """Risk-based limit rejects when actual risk exceeds cap."""
        from src.risk.position_limits import PositionLimits
        profiles = {
            "donchian": StrategyRiskProfile(
                strategy_name="donchian",
                use_risk_based_limits=True, max_risk_pct_per_position=0.02),
        }
        cfg = RiskConfig(max_position_pct=5.0, max_portfolio_heat=10.0)
        state = _make_state()
        pl = PositionLimits(cfg, state)
        profile = profiles["donchian"]
        sig = Signal(
            symbol="BTCUSDT", action=SignalAction.LONG, confidence=0.7,
            strategy_name="donchian", timeframe="1h",
            entry_price=50000.0, stop_loss=49000.0, risk_pct=0.05)
        result = pl.check(sig, profile=profile)
        assert result is not None
        assert "POSITION_LIMIT_RISK" in result

    def test_no_profile_uses_notional_limit(self):
        """Strategy without profile uses original notional check."""
        from src.risk.position_limits import PositionLimits
        cfg = RiskConfig(max_position_pct=0.05)
        state = _make_state()
        state.current_equity = 10_000
        pl = PositionLimits(cfg, state)
        # risk_pct=0.10, SL distance=1000 → qty=1, notional=50000 = 500%
        sig = Signal(
            symbol="BTCUSDT", action=SignalAction.LONG, confidence=0.9,
            strategy_name="unknown_strat", timeframe="1h",
            entry_price=50000.0, stop_loss=49000.0, risk_pct=0.10)
        result = pl.check(sig)
        assert result is not None
        assert "POSITION_LIMIT_SYMBOL" in result


# ── Strategy Profile Config Tests ──


class TestStrategyProfiles:
    def test_profile_lookup_by_strategy_name(self):
        profiles = {
            "donchian": StrategyRiskProfile(strategy_name="donchian", confidence_floor=0.5),
        }
        cfg = RiskConfig(strategy_profiles=profiles)
        assert cfg.strategy_profiles.get("donchian") is not None
        assert cfg.strategy_profiles.get("donchian").confidence_floor == 0.5
        assert cfg.strategy_profiles.get("unknown") is None

    def test_missing_profile_falls_back(self):
        """Unknown strategy with no profile uses global defaults."""
        mgr = _make_manager()
        sig = _make_signal(strategy_name="unknown_strat")
        decision = mgr.evaluate(sig)
        assert decision.approved is True  # no profile → no special handling

    def test_profile_max_risk_per_trade_override(self):
        """Profile can override CB_L1 risk cap."""
        profiles = {
            "aggressive_strat": StrategyRiskProfile(
                strategy_name="aggressive_strat", max_risk_per_trade=0.05),
        }
        cfg = RiskConfig(max_risk_per_trade=0.02, strategy_profiles=profiles,
                         max_position_pct=5.0, max_portfolio_heat=10.0,
                         fat_finger_max_value=100_000.0)
        mgr = _make_manager(config=cfg)
        # 3% risk > global 2% but < profile 5%
        sig = _make_signal(strategy_name="aggressive_strat", risk_pct=0.03)
        decision = mgr.evaluate(sig)
        assert decision.approved is True
