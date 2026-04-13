"""Tests for FundingCarryStrategy — A.3 implementation gate."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.utils.types import RiskProfile, SignalAction


def _strat(**kw) -> FundingCarryStrategy:
    defaults = dict(
        name="funding_carry",
        markets=["BTCUSDT-CARRY"],
        timeframe="8h",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.01,
        friction_pct=0.00005,
        flip_persistence_bars=3,
        max_drawdown_kill_pct=0.03,
        max_hold_bars=0,
        cooldown_bars=3,
    )
    defaults.update(kw)
    return FundingCarryStrategy(**defaults)


def _feat(close: float, funding_rate: float | None = None) -> pd.Series:
    data = {"close": close}
    if funding_rate is not None:
        data["funding_rate"] = funding_rate
    return pd.Series(data)


# ══════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════


class TestValidation:
    def test_negative_friction_rejected(self):
        with pytest.raises(ValueError, match="friction_pct"):
            _strat(friction_pct=-0.001)

    def test_zero_flip_persistence_rejected(self):
        with pytest.raises(ValueError, match="flip_persistence_bars"):
            _strat(flip_persistence_bars=0)

    def test_invalid_drawdown_rejected(self):
        with pytest.raises(ValueError, match="max_drawdown_kill_pct"):
            _strat(max_drawdown_kill_pct=1.5)
        with pytest.raises(ValueError, match="max_drawdown_kill_pct"):
            _strat(max_drawdown_kill_pct=0)


# ══════════════════════════════════════════════════════════════════════
# Entry behavior
# ══════════════════════════════════════════════════════════════════════


class TestEntry:
    def test_first_bar_enters_long(self):
        s = _strat()
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        assert sig is not None
        assert sig.action == SignalAction.LONG
        assert s._position == "LONG"
        assert s._entry_price == 100.0

    def test_second_bar_holds(self):
        s = _strat()
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        sig2 = s.process("BTCUSDT-CARRY", "8h", _feat(100.01, funding_rate=0.0001))
        assert sig2 is None  # no duplicate entry
        assert s._position == "LONG"
        assert s._bars_in_position == 1  # incremented on the hold bar

    def test_no_entry_while_funding_negative(self):
        s = _strat()
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=-0.0001))
        assert sig is None
        assert s._position == "FLAT"


# ══════════════════════════════════════════════════════════════════════
# Kill condition 1: persistent negative funding
# ══════════════════════════════════════════════════════════════════════


class TestNegativeFundingKill:
    def test_one_negative_epoch_does_not_kill(self):
        s = _strat(flip_persistence_bars=3)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(99.98, funding_rate=-0.0001))
        assert sig is None
        assert s._position == "LONG"
        assert s._negative_funding_streak == 1

    def test_three_consecutive_negative_kills(self):
        s = _strat(flip_persistence_bars=3)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(99.98, funding_rate=-0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(99.96, funding_rate=-0.0001))
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(99.94, funding_rate=-0.0001))
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "negative_funding"
        assert s._position == "FLAT"

    def test_streak_resets_on_positive_epoch(self):
        s = _strat(flip_persistence_bars=3)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(99.98, funding_rate=-0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(99.99, funding_rate=0.0001))
        # Streak reset; next negative starts the count over
        assert s._negative_funding_streak == 0
        s.process("BTCUSDT-CARRY", "8h", _feat(99.97, funding_rate=-0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(99.95, funding_rate=-0.0001))
        # Only 2 negatives in a row; should not kill
        assert s._position == "LONG"


# ══════════════════════════════════════════════════════════════════════
# Kill condition 2: unrealized drawdown
# ══════════════════════════════════════════════════════════════════════


class TestDrawdownKill:
    def test_dd_under_threshold_holds(self):
        s = _strat(max_drawdown_kill_pct=0.03)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(100.5, funding_rate=0.0001))   # peak = 100.5
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(99.0, funding_rate=-0.0001))  # dd = 1.49%
        assert sig is None  # dd < 3%
        assert s._position == "LONG"

    def test_dd_at_threshold_kills(self):
        s = _strat(max_drawdown_kill_pct=0.03, flip_persistence_bars=999)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(100.5, funding_rate=0.0001))   # peak = 100.5
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(97.0, funding_rate=0.0001))   # dd = 3.48%
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert sig.metadata["exit_reason"] == "drawdown_kill"

    def test_peak_tracks_upward_only(self):
        s = _strat()
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(101.0, funding_rate=0.0001))
        s.process("BTCUSDT-CARRY", "8h", _feat(100.5, funding_rate=0.0001))
        assert s._peak_price == 101.0  # peak didn't go down


# ══════════════════════════════════════════════════════════════════════
# Kill condition 3: time stop
# ══════════════════════════════════════════════════════════════════════


class TestTimeStop:
    def test_zero_max_hold_never_triggers(self):
        s = _strat(max_hold_bars=0, flip_persistence_bars=999)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        for _ in range(1000):
            sig = s.process("BTCUSDT-CARRY", "8h", _feat(100.01, funding_rate=0.0001))
            if sig is not None:
                break
        # With max_hold_bars=0 we should never emit a time-stop exit; the
        # only way out is negative funding or drawdown — neither triggered.
        assert s._position == "LONG"

    def test_time_stop_fires_at_max_hold(self):
        s = _strat(max_hold_bars=10, flip_persistence_bars=999)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))
        exit_sig = None
        for i in range(20):
            sig = s.process("BTCUSDT-CARRY", "8h", _feat(100.01 + i * 0.001, funding_rate=0.0001))
            if sig is not None:
                exit_sig = sig
                break
        assert exit_sig is not None
        assert exit_sig.metadata["exit_reason"] == "time_stop"


# ══════════════════════════════════════════════════════════════════════
# Re-entry after cooldown
# ══════════════════════════════════════════════════════════════════════


class TestCooldownAndReentry:
    def test_cooldown_blocks_immediate_reentry(self):
        s = _strat(flip_persistence_bars=1, cooldown_bars=3)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))   # enter
        s.process("BTCUSDT-CARRY", "8h", _feat(99.98, funding_rate=-0.0001))  # exit (1 neg)
        assert s._position == "FLAT"
        # Immediately positive funding but cooldown blocks
        sig = s.process("BTCUSDT-CARRY", "8h", _feat(99.99, funding_rate=0.0001))
        assert sig is None
        assert s._position == "FLAT"

    def test_reentry_after_cooldown(self):
        """After exit, cooldown_bars=3 means bars_since_exit must reach 3
        before re-entry. bars_since_exit increments on each FLAT bar, so
        the strategy re-enters on the 3rd bar after the exit bar.
        """
        s = _strat(flip_persistence_bars=1, cooldown_bars=3)
        s.process("BTCUSDT-CARRY", "8h", _feat(100.0, funding_rate=0.0001))  # enter LONG
        s.process("BTCUSDT-CARRY", "8h", _feat(99.98, funding_rate=-0.0001)) # exit, _bse=0
        # bar 3: _bse=1 → blocked
        r1 = s.process("BTCUSDT-CARRY", "8h", _feat(99.99, funding_rate=0.0001))
        assert r1 is None
        # bar 4: _bse=2 → blocked
        r2 = s.process("BTCUSDT-CARRY", "8h", _feat(99.99, funding_rate=0.0001))
        assert r2 is None
        # bar 5: _bse=3 → unblocked, re-enters
        r3 = s.process("BTCUSDT-CARRY", "8h", _feat(99.99, funding_rate=0.0001))
        assert r3 is not None
        assert r3.action == SignalAction.LONG
