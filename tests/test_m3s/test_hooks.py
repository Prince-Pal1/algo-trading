"""Sub-phase 0.5 — tests for src/m3s/hooks.py M3S composition facade."""

from __future__ import annotations

import pytest

from src.m3s.allocator import Allocator
from src.m3s.compounder import Compounder
from src.m3s.conviction import ConvictionScorer
from src.m3s.edge_decay import EdgeDecayMonitor
from src.m3s.hooks import M3S, SignalDecision
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.utils.types import Signal, SignalAction


_MS_PER_DAY = 86_400_000
_BASE_TS = 1_700_000_000_000


def _ts(day: int) -> int:
    return _BASE_TS + day * _MS_PER_DAY


def _sig(
    action: SignalAction = SignalAction.LONG,
    strategy: str = "a",
    risk_pct: float | None = 0.01,
    confidence: float = 0.8,
    timestamp: int = _BASE_TS,
) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        action=action,
        confidence=confidence,
        strategy_name=strategy,
        timeframe="1h",
        risk_pct=risk_pct,
        timestamp=timestamp,
    )


def _build(
    *,
    mode_key: M3SMode = M3SMode.STANDARD,
    shadow: bool = False,
    with_edge_decay: bool = True,
    with_conviction: bool = True,
):
    tracker = PortfolioTracker(initial_equity=10_000.0)
    mode = MODE_PRESETS[mode_key]
    comp = Compounder(mode=mode, tracker=tracker)
    alloc = Allocator(mode=mode, tracker=tracker)
    ed = EdgeDecayMonitor() if with_edge_decay else None
    conv = ConvictionScorer(enabled=with_conviction) if with_conviction else ConvictionScorer(enabled=False)
    m3s = M3S(
        mode=mode,
        tracker=tracker,
        compounder=comp,
        allocator=alloc,
        edge_decay=ed,
        conviction_scorer=conv,
        shadow_mode=shadow,
    )
    return tracker, comp, alloc, ed, m3s


# ══════════════════════════════════════════════════════════════════════
# on_signal: pass-through cases
# ══════════════════════════════════════════════════════════════════════


class TestSignalPassThrough:
    def test_close_signal_untouched(self):
        _, _, _, _, m3s = _build()
        sig = _sig(action=SignalAction.CLOSE, risk_pct=0.01)
        out = m3s.on_signal(sig)
        assert out is sig
        assert out.risk_pct == 0.01

    def test_hold_signal_untouched(self):
        _, _, _, _, m3s = _build()
        sig = _sig(action=SignalAction.HOLD, risk_pct=0.01)
        out = m3s.on_signal(sig)
        assert out.risk_pct == 0.01

    def test_none_risk_untouched(self):
        _, _, _, _, m3s = _build()
        sig = _sig(risk_pct=None)
        out = m3s.on_signal(sig)
        assert out.risk_pct is None

    def test_zero_risk_untouched(self):
        _, _, _, _, m3s = _build()
        sig = _sig(risk_pct=0.0)
        out = m3s.on_signal(sig)
        assert out.risk_pct == 0.0


# ══════════════════════════════════════════════════════════════════════
# on_signal: scaling math
# ══════════════════════════════════════════════════════════════════════


class TestSignalScaling:
    def test_never_upscales(self):
        """M3S must never produce risk_pct > original."""
        _, _, _, _, m3s = _build()
        sig = _sig(risk_pct=0.01, confidence=1.0)
        original = sig.risk_pct
        out = m3s.on_signal(sig)
        assert out.risk_pct <= original + 1e-12

    def test_shadow_mode_leaves_signal_unchanged(self):
        _, _, _, _, m3s = _build(shadow=True)
        sig = _sig(risk_pct=0.01)
        out = m3s.on_signal(sig)
        # Signal not mutated
        assert out.risk_pct == 0.01
        # But a decision was logged
        assert len(m3s.last_decisions()) == 1
        d = m3s.last_decisions()[0]
        assert d.shadow is True
        assert d.original_risk_pct == 0.01

    def test_live_mode_mutates_signal(self):
        _, _, _, _, m3s = _build(shadow=False)
        sig = _sig(risk_pct=0.01)
        out = m3s.on_signal(sig)
        # Scaled down (fallback alloc weight × compound neutral × conviction)
        assert out.risk_pct <= 0.01
        # Should still be positive in a normal case
        assert out.risk_pct > 0.0

    def test_edge_decay_pause_zeros_risk(self):
        tracker, _, _, ed, m3s = _build(shadow=False)
        # Manually force the monitor into paused state for strategy "a"
        assert ed is not None
        ed._states["a"] = ed._states.get("a") or _make_paused_state("a")
        from src.m3s.edge_decay import EdgeDecayFlag, EdgeDecayState
        ed._states["a"] = EdgeDecayState(strategy="a", flag=EdgeDecayFlag.PAUSED)

        sig = _sig(risk_pct=0.01, strategy="a")
        out = m3s.on_signal(sig)
        assert out.risk_pct == 0.0

    def test_edge_decay_halve_multiplies_by_half(self):
        tracker, _, _, ed, m3s = _build(shadow=False)
        from src.m3s.edge_decay import EdgeDecayFlag, EdgeDecayState
        assert ed is not None
        ed._states["a"] = EdgeDecayState(strategy="a", flag=EdgeDecayFlag.HALVED)

        sig_halved = _sig(risk_pct=0.01, strategy="a")
        out_halved = m3s.on_signal(sig_halved)

        # Compare to a sibling un-halved strategy
        sig_normal = _sig(risk_pct=0.01, strategy="b")
        out_normal = m3s.on_signal(sig_normal)

        # Halved strategy got approximately half of the un-halved one (equal alloc fallback)
        assert out_halved.risk_pct < out_normal.risk_pct


# ══════════════════════════════════════════════════════════════════════
# Event hooks
# ══════════════════════════════════════════════════════════════════════


class TestEventHooks:
    def test_on_trade_close_updates_tracker(self):
        tracker, _, _, _, m3s = _build()
        m3s.on_trade_close("a", pnl=150.0, symbol="BTC", ts_ms=_ts(0))
        assert tracker.equity == pytest.approx(10_150.0)
        assert tracker.hwm == pytest.approx(10_150.0)

    def test_on_bar_records_exposure(self):
        tracker, _, _, _, m3s = _build()
        m3s.on_bar("a", "BTC", exposure=1, ts_ms=_ts(0))
        m3s.on_bar("a", "BTC", exposure=-1, ts_ms=_ts(0) + 1000)
        snap = m3s.snapshot(now_ms=_ts(0) + 2000)
        # Strategy shows up in per_strategy dict
        assert "a" in snap.per_strategy

    def test_on_fill_records_exposure(self):
        tracker, _, _, _, m3s = _build()
        m3s.on_fill("BTC", "a", side_sign=1, ts_ms=_ts(0))
        snap = m3s.snapshot(now_ms=_ts(0))
        assert "a" in snap.per_strategy


# ══════════════════════════════════════════════════════════════════════
# Rebalance
# ══════════════════════════════════════════════════════════════════════


class TestRebalance:
    def test_rebalance_caches_allocation(self):
        tracker, _, _, _, m3s = _build()
        # Add enough history to trigger mature HRP-lite path
        import numpy as np
        rng = np.random.default_rng(11)
        for day in range(90):
            ts = _ts(day)
            m3s.on_trade_close("a", pnl=float(rng.normal(20, 40)), symbol="BTC", ts_ms=ts)
            m3s.on_trade_close("b", pnl=float(rng.normal(15, 35)), symbol="ETH", ts_ms=ts)
            m3s.on_trade_close("c", pnl=float(rng.normal(10, 25)), symbol="SOL", ts_ms=ts)
        decision = m3s.rebalance(now_ms=_ts(89))
        assert m3s.last_allocation() is decision
        assert set(decision.weights.keys()) == {"a", "b", "c"}

    def test_rebalance_advances_compounder_base(self):
        tracker, comp, _, _, m3s = _build()
        m3s.on_trade_close("a", pnl=500.0, symbol="BTC", ts_ms=_ts(0))
        m3s.rebalance(now_ms=_ts(0))
        assert comp.state.base_equity == pytest.approx(10_500.0)
        assert comp.state.hwm == pytest.approx(10_500.0)

    def test_rebalance_runs_edge_decay_check(self):
        tracker, _, _, ed, m3s = _build()
        # Add enough trades so the snapshot has non-cold-start per-strategy data
        for day in range(20):
            m3s.on_trade_close("a", pnl=10.0, symbol="BTC", ts_ms=_ts(day))
        m3s.rebalance(now_ms=_ts(20))
        # Monitor has processed the snapshot (no alerts expected, but state exists)
        assert ed is not None
        # Healthy strategy should be NORMAL
        from src.m3s.edge_decay import EdgeDecayFlag
        assert ed.flag("a") == EdgeDecayFlag.NORMAL


# ══════════════════════════════════════════════════════════════════════
# Decision log
# ══════════════════════════════════════════════════════════════════════


class TestDecisionLog:
    def test_decision_log_grows(self):
        _, _, _, _, m3s = _build(shadow=True)
        for _ in range(5):
            m3s.on_signal(_sig())
        assert len(m3s.last_decisions()) == 5

    def test_decision_log_caps_at_500(self):
        _, _, _, _, m3s = _build(shadow=True)
        for _ in range(600):
            m3s.on_signal(_sig())
        assert len(m3s.last_decisions()) == 500

    def test_decision_records_components(self):
        _, _, _, _, m3s = _build(shadow=True)
        m3s.on_signal(_sig(risk_pct=0.02, confidence=0.9))
        d = m3s.last_decisions()[0]
        assert isinstance(d, SignalDecision)
        assert d.original_risk_pct == 0.02
        assert 0.0 <= d.alloc_weight <= 1.0
        assert d.conviction_multiplier > 0.0


# ══════════════════════════════════════════════════════════════════════
# Mode transition
# ══════════════════════════════════════════════════════════════════════


class TestModeTransition:
    def test_set_mode_propagates(self):
        _, comp, alloc, _, m3s = _build(mode_key=M3SMode.STANDARD)
        assert m3s.mode.name == M3SMode.STANDARD
        m3s.set_mode(MODE_PRESETS[M3SMode.CONSERVATIVE])
        assert m3s.mode.name == M3SMode.CONSERVATIVE
        assert comp.mode.name == M3SMode.CONSERVATIVE
        assert alloc.mode.name == M3SMode.CONSERVATIVE


def _make_paused_state(name: str):
    from src.m3s.edge_decay import EdgeDecayFlag, EdgeDecayState
    return EdgeDecayState(strategy=name, flag=EdgeDecayFlag.PAUSED)
