from __future__ import annotations

import pytest

from src.strategies.momentum.vol_momentum_gold import (
    VolMomentumGoldStrategy,
    _is_in_session,
)
from src.utils.types import Tier


class TestConstruction:
    def test_defaults(self):
        s = VolMomentumGoldStrategy()
        assert s.markets == ["XAUUSD"]
        assert s.timeframe == "1h"
        assert s.leverage_range == (10.0, 50.0)
        assert s.tier == Tier.INSTITUTIONAL_MR
        assert s.session_filter is True   # tuned default
        assert s.momentum_window == 240   # tuned default
        assert s.long_only is True        # tuned default

    def test_custom_leverage_range(self):
        s = VolMomentumGoldStrategy(leverage_range=(5.0, 100.0))
        assert s.leverage_range == (5.0, 100.0)

    def test_invalid_leverage_range_rejected(self):
        with pytest.raises(ValueError, match="leverage_range"):
            VolMomentumGoldStrategy(leverage_range=(0.5, 10.0))

    def test_tier_is_institutional_mr(self):
        s = VolMomentumGoldStrategy()
        assert s.tier == Tier.INSTITUTIONAL_MR


class TestSessionWindows:
    def _ts(self, hour: int, minute: int = 0) -> int:
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 2, hour, minute, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def test_london_session(self):
        assert _is_in_session(self._ts(8, 30)) is True

    def test_ny_session(self):
        assert _is_in_session(self._ts(14, 0)) is True

    def test_asia_out(self):
        assert _is_in_session(self._ts(3, 0)) is False

    def test_between_sessions_out(self):
        assert _is_in_session(self._ts(12, 0)) is False
