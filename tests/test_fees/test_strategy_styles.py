"""Phase C — Strategy fee_style declarations are present and valid.

Every concrete strategy must declare a `fee_style` class attribute from
the approved set. This test is the enforcement — if someone adds a new
strategy without declaring a style, it shows up here.
"""

from __future__ import annotations

import pytest

from src.strategies.aggressive.candle_burst_hunter import CandleBurstHunterStrategy
from src.strategies.aggressive.hedged_structure_play import HedgedStructurePlayStrategy
from src.strategies.aggressive.news_spike_fade import NewsSpikeFadeStrategy
from src.strategies.base import BaseStrategy
from src.strategies.base_scalper import ScalperStrategy
from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.strategies.carry.funding_mean_reversion import FundingMeanReversionStrategy
from src.strategies.day_trading.bb_rsi_mr import BBRSIMeanRevStrategy
from src.strategies.momentum.clenow_momentum import ClenowMomentumStrategy
from src.strategies.momentum.vol_momentum import VolMomentumStrategy
from src.strategies.momentum.vol_momentum_gold import VolMomentumGoldStrategy
from src.strategies.stat_arb.btc_neutral_mr import BTCNeutralMRStrategy
from src.strategies.trend_following.donchian_ensemble import DonchianEnsembleStrategy
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy
from src.strategies.trend_following.swift_alma import SwiftAlmaStrategy
from src.strategies.trend_following.swift_alma_v2 import SwiftAlmaV2Strategy
from src.strategies.verification.rsi2_mr import RSI2MeanRevStrategy


VALID_STYLES = {"scalping", "intraday", "swing", "position", "arbitrage"}


# Expected style per strategy. Update this list when adding a new strategy —
# the test catches silent fee_style drift.
EXPECTED = [
    # Scalping tier (aggressive / HFT / ema-cross)
    (ScalperStrategy,                 "scalping"),
    (CandleBurstHunterStrategy,       "scalping"),
    (HedgedStructurePlayStrategy,     "scalping"),
    (NewsSpikeFadeStrategy,           "scalping"),
    # Intraday (same-day, no swap)
    (BBRSIMeanRevStrategy,            "intraday"),
    (FundingMeanReversionStrategy,    "intraday"),
    (RSI2MeanRevStrategy,             "intraday"),
    # Swing (multi-day, swap-aware)
    (DonchianEnsembleStrategy,        "swing"),
    (DonchianGoldStrategy,            "swing"),  # inherits from DonchianEnsembleStrategy
    (VolMomentumStrategy,             "swing"),
    (VolMomentumGoldStrategy,         "swing"),  # inherits from VolMomentumStrategy
    (SwiftAlmaStrategy,               "swing"),
    (SwiftAlmaV2Strategy,             "swing"),
    # Position (multi-week+, swap-dominant)
    (ClenowMomentumStrategy,          "position"),
    (FundingCarryStrategy,            "position"),
    # Arbitrage (paired / spread, precision-critical)
    (BTCNeutralMRStrategy,            "arbitrage"),
]


@pytest.mark.parametrize("strategy_cls,expected_style", EXPECTED)
def test_strategy_declares_expected_fee_style(strategy_cls, expected_style):
    assert hasattr(strategy_cls, "fee_style"), (
        f"{strategy_cls.__name__} does not declare fee_style"
    )
    assert strategy_cls.fee_style in VALID_STYLES, (
        f"{strategy_cls.__name__}.fee_style={strategy_cls.fee_style!r} "
        f"is not in the valid set {VALID_STYLES}"
    )
    assert strategy_cls.fee_style == expected_style, (
        f"{strategy_cls.__name__}.fee_style={strategy_cls.fee_style!r} "
        f"but test expected {expected_style!r} — update either the class or this test"
    )


def test_base_strategy_has_sensible_default():
    assert BaseStrategy.fee_style == "intraday"
    assert BaseStrategy.fee_style in VALID_STYLES


def test_gold_variants_inherit_style_from_parent():
    """DonchianGold / VolMomentumGold shouldn't need to re-declare — they
    inherit. This test pins that implicit inheritance works."""
    assert DonchianGoldStrategy.fee_style == DonchianEnsembleStrategy.fee_style
    assert VolMomentumGoldStrategy.fee_style == VolMomentumStrategy.fee_style


def test_fee_manager_accepts_all_declared_styles():
    """Every style we use in the strategies must be accepted by FeeManager.project_cost()."""
    from src.fees import FeeManager
    for strategy_cls, _ in EXPECTED:
        cp = FeeManager.project_cost(
            symbol="XAUUSD",
            qty_lots=1.0,
            style=strategy_cls.fee_style,
            mid_price=4865.0,
            scenario="normal",
        )
        # Should produce a valid CostProjection with finite numbers
        assert cp.total == cp.spread + cp.commission + cp.swap
        assert cp.spread >= 0
        assert cp.commission >= 0
