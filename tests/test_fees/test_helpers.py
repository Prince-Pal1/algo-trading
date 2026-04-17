"""Phase D — Signal-level cost helpers + RiskManager.projected_cost."""

from __future__ import annotations

import pytest

from src.fees import (
    cost_for_signal,
    cost_for_strategy_signal,
    explain_for_signal,
)
from src.fees.scenario import reset_news_cache
from src.utils.types import Signal, SignalAction


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_news_cache()
    yield


def _mk_signal(
    symbol: str = "XAUUSD",
    action: SignalAction = SignalAction.LONG,
    entry_price: float = 4865.0,
    quantity_lots: float | None = 1.0,
    timestamp: int = 0,
) -> Signal:
    meta = {"quantity_lots": quantity_lots} if quantity_lots is not None else None
    return Signal(
        symbol=symbol,
        action=action,
        confidence=0.7,
        strategy_name="test",
        timeframe="1h",
        entry_price=entry_price,
        metadata=meta,
        timestamp=timestamp,
    )


class TestCostForSignal:
    def test_derives_symbol_price_qty(self):
        s = _mk_signal()
        cp = cost_for_signal(s, style="swing", hold_hours=48.0)
        assert cp.total == cp.spread + cp.commission + cp.swap
        assert cp.scenario == "normal"
        assert cp.broker_id == "ic_markets_ctrader"

    def test_missing_entry_price_raises(self):
        s = _mk_signal(entry_price=None)
        with pytest.raises(ValueError, match="mid_price"):
            cost_for_signal(s, style="intraday")

    def test_short_side_uses_short_swap(self):
        long_s = _mk_signal(action=SignalAction.LONG)
        short_s = _mk_signal(action=SignalAction.SHORT)
        cp_long = cost_for_signal(long_s, style="swing", hold_hours=48.0)
        cp_short = cost_for_signal(short_s, style="swing", hold_hours=48.0)
        assert cp_long.swap < 0  # long gold usually pays swap
        assert cp_short.swap > 0  # short gold usually earns swap

    def test_timestamp_drives_scenario(self):
        # 03:00 UTC → illiquid
        from datetime import datetime
        ts_ms = int(datetime.fromisoformat("2026-04-17T03:00:00+00:00").timestamp() * 1000)
        s = _mk_signal(timestamp=ts_ms)
        cp = cost_for_signal(s, style="intraday")
        assert cp.scenario == "illiquid"

    def test_qty_scales_costs(self):
        s1 = _mk_signal(quantity_lots=1.0)
        s5 = _mk_signal(quantity_lots=5.0)
        cp1 = cost_for_signal(s1, style="scalping")
        cp5 = cost_for_signal(s5, style="scalping")
        assert cp5.spread == pytest.approx(cp1.spread * 5, rel=1e-6)
        assert cp5.commission == pytest.approx(cp1.commission * 5, rel=1e-6)

    def test_explicit_qty_lots_override(self):
        s = _mk_signal(quantity_lots=1.0)
        cp = cost_for_signal(s, style="scalping", qty_lots=3.0)
        cp1 = cost_for_signal(s, style="scalping")
        assert cp.spread == pytest.approx(cp1.spread * 3, rel=1e-6)


class TestCostForStrategySignal:
    def test_pulls_style_from_strategy(self):
        class FakeStrat:
            fee_style = "position"

        s = _mk_signal()
        cp = cost_for_strategy_signal(FakeStrat(), s)
        # position style → swap accrues using default 480h (20 nights)
        assert cp.swap != 0

    def test_missing_fee_style_defaults_to_intraday(self):
        class MinimalStrat:
            pass  # no fee_style attribute

        s = _mk_signal()
        cp = cost_for_strategy_signal(MinimalStrat(), s)
        # intraday → no swap
        assert cp.swap == 0.0


class TestExplainForSignal:
    def test_returns_context_dict(self):
        s = _mk_signal()
        info = explain_for_signal(s, style="swing")
        assert info["symbol"] == "XAUUSD"
        assert info["style"] == "swing"
        assert info["broker_id"] == "ic_markets_ctrader"


class TestRiskManagerProjectedCost:
    def test_risk_manager_exposes_projected_cost(self):
        from src.risk.manager import RiskManager
        from src.risk.config import RiskConfig
        from src.risk.state import RiskState

        cfg = RiskConfig()
        rm = RiskManager(cfg, RiskState())

        s = _mk_signal()
        cp = rm.projected_cost(s, style="swing", hold_hours=48.0)
        assert cp.broker_id == "ic_markets_ctrader"
        assert cp.spread > 0
        assert cp.commission > 0
