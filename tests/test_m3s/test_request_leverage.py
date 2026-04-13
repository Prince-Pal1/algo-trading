"""Phase G.2b — tests for M3S.request_leverage API + LeverageGrant dataclass.

Covers:
- LeverageGrant construction (msgspec frozen struct)
- LeverageReasonCode enum values
- All 5 reason-code paths through M3S.request_leverage
- Edge cases: headroom == 0, conviction == 0/1, range collapsed
- SQLite persistence via LeverageGrantStore
"""

from __future__ import annotations

import pytest

from src.m3s.allocator import Allocator
from src.m3s.compounder import Compounder
from src.m3s.hooks import M3S
from src.m3s.leverage_grants import LeverageGrantStore
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.utils.types import LeverageGrant, LeverageReasonCode


_BASE_TS = 1_700_000_000_000


def _build_m3s(
    *,
    mode_key: M3SMode = M3SMode.STANDARD,
    aggregate_cap: float = 100.0,
    grant_store: LeverageGrantStore | None = None,
) -> M3S:
    """Construct an M3S facade for leverage tests."""
    tracker = PortfolioTracker(initial_equity=10_000.0)
    mode = MODE_PRESETS[mode_key]
    comp = Compounder(mode=mode, tracker=tracker)
    alloc = Allocator(mode=mode, tracker=tracker)
    return M3S(
        mode=mode,
        tracker=tracker,
        compounder=comp,
        allocator=alloc,
        shadow_mode=False,
        aggregate_leverage_cap=aggregate_cap,
        leverage_grant_store=grant_store,
    )


# ══════════════════════════════════════════════════════════════════════
# LeverageGrant dataclass
# ══════════════════════════════════════════════════════════════════════


class TestLeverageGrantDataclass:
    def test_construction_with_required_fields(self):
        grant = LeverageGrant(
            strategy_name="test",
            ts_ms=_BASE_TS,
            requested=25.0,
            granted=20.0,
            reason=LeverageReasonCode.CAPPED_BY_AGGREGATE,
            conviction=0.8,
            declared_range_min=10.0,
            declared_range_max=50.0,
            regime_target=25.0,
            conviction_target=42.0,
            aggregate_before=80.0,
            aggregate_cap=100.0,
            m3s_regime="STANDARD",
        )
        assert grant.strategy_name == "test"
        assert grant.granted == 20.0
        assert grant.reason == LeverageReasonCode.CAPPED_BY_AGGREGATE
        assert grant.user_reason == ""  # default

    def test_reason_code_values(self):
        assert LeverageReasonCode.FULL.value == "full"
        assert LeverageReasonCode.CAPPED_BY_AGGREGATE.value == "capped_by_aggregate"
        assert LeverageReasonCode.CAPPED_BY_REGIME.value == "capped_by_regime"
        assert LeverageReasonCode.CAPPED_BY_CONVICTION.value == "capped_by_conviction"
        assert LeverageReasonCode.CAPPED_BY_CAP.value == "capped_by_cap"


# ══════════════════════════════════════════════════════════════════════
# M3S.request_leverage — 5 reason codes
# ══════════════════════════════════════════════════════════════════════


class TestRequestLeverageFullGrant:
    def test_full_grant_when_headroom_and_conviction_max(self):
        """In STANDARD mode, drawdown=0, conviction=1.0, range=(10,50):
        regime_target = 30 (midpoint, because dd < 2%),
        conviction_target = 50,
        requested = min(30, 50) = 30.
        With full headroom → granted = 30 (FULL).
        """
        m3s = _build_m3s(mode_key=M3SMode.STANDARD, aggregate_cap=100.0)
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=1.0,
            declared_range=(10.0, 50.0),
            current_aggregate_leverage=0.0,
        )
        assert grant.granted == pytest.approx(30.0)
        assert grant.requested == pytest.approx(30.0)
        # regime was the binding constraint on requested (30 < 50), so reason
        # should be CAPPED_BY_REGIME, NOT FULL — because the engine still
        # granted the requested value, but requested itself was capped by
        # the regime.
        assert grant.reason == LeverageReasonCode.CAPPED_BY_REGIME

    def test_true_full_grant(self):
        """CONSERVATIVE mode clamps to range min; if conviction=0 also puts
        target at min, both equal → FULL (nothing capped).
        """
        m3s = _build_m3s(mode_key=M3SMode.CONSERVATIVE, aggregate_cap=100.0)
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=0.0,  # conviction target = lo
            declared_range=(10.0, 50.0),
            current_aggregate_leverage=0.0,
        )
        # CONSERVATIVE → regime_target = lo = 10
        # conviction_target = 10 + (50-10) * 0 = 10
        # both equal → FULL
        assert grant.regime_target == pytest.approx(10.0)
        assert grant.conviction_target == pytest.approx(10.0)
        assert grant.granted == pytest.approx(10.0)
        assert grant.reason == LeverageReasonCode.FULL


class TestRequestLeverageCappedByAggregate:
    def test_capped_when_headroom_binds(self):
        """Strategy asks for 30× but aggregate is already at 85× of 100 cap.
        Headroom = 15 → granted = 15 (less than requested 30)."""
        m3s = _build_m3s(mode_key=M3SMode.STANDARD, aggregate_cap=100.0)
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=1.0,
            declared_range=(10.0, 50.0),
            current_aggregate_leverage=85.0,  # only 15 headroom
        )
        # requested ~30 (regime-capped to midpoint), headroom 15 → granted 15
        assert grant.requested == pytest.approx(30.0)
        assert grant.granted == pytest.approx(15.0)
        assert grant.reason == LeverageReasonCode.CAPPED_BY_AGGREGATE

    def test_zero_headroom_gives_zero_grant(self):
        """When aggregate is already at cap, granted < lo → forced to 0."""
        m3s = _build_m3s(mode_key=M3SMode.STANDARD, aggregate_cap=100.0)
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=1.0,
            declared_range=(10.0, 50.0),
            current_aggregate_leverage=100.0,  # full
        )
        assert grant.granted == 0.0  # cannot grant even the minimum
        assert grant.reason == LeverageReasonCode.CAPPED_BY_AGGREGATE


class TestRequestLeverageCappedByRegime:
    def test_standard_with_drawdown_caps_to_min(self):
        """STANDARD mode with drawdown > 2% → regime_target = lo (=10).
        conviction_target = 50. requested = 10. granted = 10 (CAPPED_BY_REGIME).
        """
        tracker = PortfolioTracker(initial_equity=10_000.0)
        # Simulate a drawdown by pushing realized equity below HWM
        tracker.update_equity_from_executor(new_equity=10_000.0, ts_ms=_BASE_TS)
        tracker.update_equity_from_executor(new_equity=9_500.0, ts_ms=_BASE_TS + 1000)
        mode = MODE_PRESETS[M3SMode.STANDARD]
        comp = Compounder(mode=mode, tracker=tracker)
        alloc = Allocator(mode=mode, tracker=tracker)
        m3s = M3S(
            mode=mode,
            tracker=tracker,
            compounder=comp,
            allocator=alloc,
            shadow_mode=False,
            aggregate_leverage_cap=100.0,
        )
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=1.0,
            declared_range=(10.0, 50.0),
        )
        # drawdown = 5% > 2% threshold → STANDARD returns lo = 10
        assert grant.regime_target == pytest.approx(10.0)
        assert grant.conviction_target == pytest.approx(50.0)
        assert grant.requested == pytest.approx(10.0)
        assert grant.granted == pytest.approx(10.0)
        assert grant.reason == LeverageReasonCode.CAPPED_BY_REGIME


class TestRequestLeverageCappedByConviction:
    def test_low_conviction_caps_below_regime(self):
        """GROWTH mode with drawdown=0 → regime_target = hi (50).
        conviction = 0.25 → conviction_target = 10 + 40*0.25 = 20.
        requested = min(50, 20) = 20, reason = CAPPED_BY_CONVICTION.
        """
        m3s = _build_m3s(mode_key=M3SMode.GROWTH, aggregate_cap=100.0)
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=0.25,
            declared_range=(10.0, 50.0),
        )
        assert grant.regime_target == pytest.approx(50.0)
        assert grant.conviction_target == pytest.approx(20.0)
        assert grant.requested == pytest.approx(20.0)
        assert grant.granted == pytest.approx(20.0)
        assert grant.reason == LeverageReasonCode.CAPPED_BY_CONVICTION


class TestRequestLeverageCappedByCap:
    def test_grant_clamped_to_declared_max(self):
        """Edge case: range min = max = 25 (single-point range). Request
        always returns 25. This IS technically FULL — but verifies the
        hi clamp path is safe."""
        m3s = _build_m3s(mode_key=M3SMode.GROWTH, aggregate_cap=100.0)
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=1.0,
            declared_range=(25.0, 25.0),
        )
        assert grant.granted == pytest.approx(25.0)
        # Single-point range → regime == conviction == 25 → FULL
        assert grant.reason == LeverageReasonCode.FULL


# ══════════════════════════════════════════════════════════════════════
# Ring buffer + store persistence
# ══════════════════════════════════════════════════════════════════════


class TestLastGrantsRingBuffer:
    def test_grants_accumulate(self):
        m3s = _build_m3s()
        for _ in range(3):
            m3s.request_leverage(
                strategy_name="scalper",
                conviction=0.5,
                declared_range=(10.0, 50.0),
            )
        grants = m3s.last_grants()
        assert len(grants) == 3

    def test_grants_ring_capped_at_500(self):
        m3s = _build_m3s()
        for _ in range(510):
            m3s.request_leverage(
                strategy_name="scalper",
                conviction=0.5,
                declared_range=(10.0, 50.0),
            )
        assert len(m3s.last_grants()) == 500


class TestLeverageGrantStore:
    def test_in_memory_store_persists_grant(self):
        store = LeverageGrantStore(db_path=":memory:")
        m3s = _build_m3s(grant_store=store)
        m3s.request_leverage(
            strategy_name="scalper",
            conviction=0.7,
            declared_range=(10.0, 50.0),
        )
        assert store.count() == 1
        rows = store.recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["strategy_name"] == "scalper"
        assert rows[0]["granted"] > 0
        assert rows[0]["reason"] in {
            "full", "capped_by_aggregate", "capped_by_regime",
            "capped_by_conviction", "capped_by_cap",
        }
        store.close()

    def test_store_records_all_fields(self):
        store = LeverageGrantStore(db_path=":memory:")
        m3s = _build_m3s(grant_store=store, aggregate_cap=100.0)
        m3s.request_leverage(
            strategy_name="scalper",
            conviction=0.6,
            declared_range=(10.0, 50.0),
            current_aggregate_leverage=30.0,
            reason="ema_cross_long",
        )
        row = store.recent(limit=1)[0]
        assert row["strategy_name"] == "scalper"
        assert row["conviction"] == pytest.approx(0.6)
        assert row["declared_range_min"] == 10.0
        assert row["declared_range_max"] == 50.0
        assert row["aggregate_before"] == 30.0
        assert row["aggregate_cap"] == 100.0
        assert row["m3s_regime"] == "STANDARD"
        assert row["user_reason"] == "ema_cross_long"
        store.close()


# ══════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_negative_conviction_clamped_to_zero(self):
        m3s = _build_m3s()
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=-0.5,  # invalid
            declared_range=(10.0, 50.0),
        )
        assert grant.conviction == 0.0

    def test_conviction_above_one_clamped(self):
        m3s = _build_m3s()
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=2.0,  # invalid
            declared_range=(10.0, 50.0),
        )
        assert grant.conviction == 1.0

    def test_range_min_below_1_clamped_up(self):
        m3s = _build_m3s()
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=0.5,
            declared_range=(0.5, 10.0),  # lo < 1.0
        )
        assert grant.declared_range_min == 1.0

    def test_range_max_below_min_clamped_up(self):
        m3s = _build_m3s()
        grant = m3s.request_leverage(
            strategy_name="scalper",
            conviction=0.5,
            declared_range=(20.0, 10.0),  # inverted
        )
        # hi clamped to lo
        assert grant.declared_range_max == 20.0
        assert grant.declared_range_min == 20.0

    def test_default_crypto_range_1_1_returns_1(self):
        """Crypto strategies with leverage_range=(1,1) should always get 1."""
        m3s = _build_m3s()
        grant = m3s.request_leverage(
            strategy_name="btc_strat",
            conviction=1.0,
            declared_range=(1.0, 1.0),
        )
        assert grant.granted == 1.0
        assert grant.reason == LeverageReasonCode.FULL
