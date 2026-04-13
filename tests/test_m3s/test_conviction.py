"""Sub-phase 0.5 — tests for src/m3s/conviction.py (Tier 1 #3)."""

from __future__ import annotations

import pytest

from src.m3s.conviction import ConvictionScore, ConvictionScorer
from src.utils.types import Signal, SignalAction


def _sig(confidence: float = 0.8, metadata: dict | None = None) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        action=SignalAction.LONG,
        confidence=confidence,
        strategy_name="test",
        timeframe="1h",
        risk_pct=0.01,
        metadata=metadata,
    )


class TestConvictionBasics:
    def test_enabled_by_default(self):
        sc = ConvictionScorer()
        r = sc.score(_sig(confidence=0.8))
        assert 0.5 <= r.multiplier <= 1.3

    def test_disabled_returns_one(self):
        sc = ConvictionScorer(enabled=False)
        r = sc.score(_sig(confidence=0.8, metadata={"volume_z": 2.0}))
        assert r.multiplier == 1.0

    def test_custom_floor_ceiling(self):
        sc = ConvictionScorer(multiplier_floor=0.2, multiplier_ceiling=2.0)
        r = sc.score(_sig(confidence=1.0, metadata={"volume_z": 5.0, "mtf_aligned": True}))
        assert r.multiplier <= 2.0

    def test_invalid_floor_rejected(self):
        with pytest.raises(ValueError, match="multiplier_floor"):
            ConvictionScorer(multiplier_floor=0.0)

    def test_invalid_ceiling_rejected(self):
        with pytest.raises(ValueError, match="multiplier_ceiling"):
            ConvictionScorer(multiplier_floor=1.0, multiplier_ceiling=0.5)


class TestComponents:
    def test_high_confidence_boosts(self):
        sc = ConvictionScorer()
        low = sc.score(_sig(confidence=0.3))
        high = sc.score(_sig(confidence=0.95))
        assert high.multiplier > low.multiplier

    def test_volume_z_above_zero_boosts(self):
        sc = ConvictionScorer()
        baseline = sc.score(_sig(confidence=0.8))
        boosted = sc.score(_sig(confidence=0.8, metadata={"volume_z": 2.0}))
        assert boosted.multiplier > baseline.multiplier

    def test_volume_z_below_zero_shrinks(self):
        sc = ConvictionScorer()
        baseline = sc.score(_sig(confidence=0.8))
        shrunk = sc.score(_sig(confidence=0.8, metadata={"volume_z": -2.0}))
        assert shrunk.multiplier < baseline.multiplier

    def test_mtf_aligned_boosts(self):
        sc = ConvictionScorer()
        aligned = sc.score(_sig(confidence=0.8, metadata={"mtf_aligned": True}))
        not_aligned = sc.score(_sig(confidence=0.8, metadata={"mtf_aligned": False}))
        assert aligned.multiplier > not_aligned.multiplier

    def test_large_trigger_distance_penalizes(self):
        sc = ConvictionScorer()
        close = sc.score(_sig(confidence=0.8, metadata={"trigger_distance_atr": 0.1}))
        far = sc.score(_sig(confidence=0.8, metadata={"trigger_distance_atr": 2.0}))
        assert close.multiplier > far.multiplier


class TestClamping:
    def test_multiplier_clamped_to_ceiling(self):
        sc = ConvictionScorer(multiplier_ceiling=1.3)
        r = sc.score(
            _sig(
                confidence=1.0,
                metadata={"volume_z": 10.0, "mtf_aligned": True, "trigger_distance_atr": 0},
            )
        )
        assert r.multiplier <= 1.3

    def test_multiplier_clamped_to_floor(self):
        sc = ConvictionScorer(multiplier_floor=0.5)
        r = sc.score(
            _sig(
                confidence=0.0,
                metadata={"volume_z": -10.0, "mtf_aligned": False, "trigger_distance_atr": 5.0},
            )
        )
        assert r.multiplier >= 0.5


class TestReasoning:
    def test_reasoning_includes_components(self):
        sc = ConvictionScorer()
        r = sc.score(_sig(confidence=0.8, metadata={"volume_z": 1.5}))
        assert "conf=" in r.reasoning
        assert "vol_z=" in r.reasoning

    def test_score_struct_frozen(self):
        sc = ConvictionScorer()
        r = sc.score(_sig(confidence=0.8))
        assert isinstance(r, ConvictionScore)
        with pytest.raises((AttributeError, TypeError)):
            r.multiplier = 999.0  # type: ignore[misc]
