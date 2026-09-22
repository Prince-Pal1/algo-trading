"""Tests for src/flow/narrative.py — the plain-English read of the situation.

The risk here is not a crash, it is a sentence that is confidently wrong. These
pin the claims that would mislead: which side is in control, whether price is
actually in the zone, and that nothing ever reads as advice.
"""

from __future__ import annotations

import pytest

from src.flow.features import ZoneAccumulator
from src.flow.level_registry import Level, LevelSide
from src.flow.narrative import describe
from src.flow.zone_state import ZonePhase, ZoneState
from src.utils.types import Tick

TS = 1_757_000_000_000


def _support(width: float = 100.0) -> Level:
    return Level("sup", "BTCUSDT", 98_000.0, width, LevelSide.LONG, note="pdl")


def _resistance(width: float = 100.0) -> Level:
    return Level("res", "BTCUSDT", 98_000.0, width, LevelSide.SHORT, note="vah")


def _features(level: Level, maker: bool = True, n: int = 60, price: float = 97_950.0):
    acc = ZoneAccumulator(
        level_price=level.price, is_support=level.side is LevelSide.LONG,
        baseline_range=200.0, baseline_trade_size=1.0, zone_width=level.width,
    )
    for i in range(n):
        acc.on_tick(Tick("BTCUSDT", price, 1.0, TS + i * 100, maker))
    return acc.features()


def _say(level, state, phase, features, price):
    out = describe(level, state, phase, features, price, level.distance(price))
    return out, " ".join(out["lines"])


class TestStates:
    def test_idle_says_nothing_is_measured(self):
        out, text = _say(_support(), ZoneState.IDLE, ZonePhase.WATCHING,
                         None, 99_000.0)
        assert out["headline"] == "Waiting"
        assert "until price is inside it" in text

    def test_approaching_names_the_zone(self):
        out, text = _say(_support(), ZoneState.APPROACHING, ZonePhase.WATCHING,
                         None, 98_150.0)
        assert out["headline"] == "Approaching"
        assert "97,900.00–98,000.00" in text

    def test_invalidated_says_so_plainly(self):
        lv = _support()
        out, _ = _say(lv, ZoneState.INVALIDATED, ZonePhase.FAILING,
                      _features(lv), 97_800.0)
        assert out["headline"] == "Invalidated — the level broke"

    @pytest.mark.parametrize("phase, word", [
        (ZonePhase.ABSORBING, "Absorbing"),
        (ZonePhase.TURNING, "Turning"),
        (ZonePhase.FAILING, "Failing"),
    ])
    def test_phase_drives_the_headline(self, phase, word):
        lv = _support()
        out, _ = _say(lv, ZoneState.EVALUATING, phase, _features(lv), 97_950.0)
        assert out["headline"].startswith(word)


class TestPosition:
    def test_thirds_inside_the_zone(self):
        lv = _support()
        for price, third in ((97_910.0, "lower third"), (97_950.0, "middle"),
                             (97_990.0, "upper third")):
            _, text = _say(lv, ZoneState.EVALUATING, ZonePhase.WATCHING,
                           _features(lv), price)
            assert third in text, f"{price} should read {third}"

    def test_price_outside_the_zone_is_named_not_clamped(self):
        """Clamping a price that has left into 'upper third' would describe the
        wrong situation, and leaving is the interesting moment."""
        lv = _support()
        _, above = _say(lv, ZoneState.CONFIRMED, ZonePhase.TURNING,
                        _features(lv), 98_060.0)
        assert "ABOVE your zone" in above and "back through the level" in above
        _, below = _say(lv, ZoneState.EVALUATING, ZonePhase.FAILING,
                        _features(lv), 97_800.0)
        assert "BELOW your zone" in below and "wrong way" in below

    def test_a_resistance_reads_the_other_way(self):
        lv = _resistance()
        _, below = _say(lv, ZoneState.CONFIRMED, ZonePhase.TURNING,
                        _features(lv, price=98_050.0), 97_940.0)
        assert "BELOW your zone" in below and "back through the level" in below


class TestPressure:
    def test_selling_is_attributed_to_sellers(self):
        lv = _support()
        _, text = _say(lv, ZoneState.EVALUATING, ZonePhase.ABSORBING,
                       _features(lv, maker=True), 97_950.0)
        assert "Sellers are heavily in control" in text

    def test_buying_is_attributed_to_buyers(self):
        lv = _support()
        _, text = _say(lv, ZoneState.EVALUATING, ZonePhase.WATCHING,
                       _features(lv, maker=False), 97_950.0)
        assert "Buyers are heavily in control" in text

    def test_a_pinned_price_reads_as_barely_moved(self):
        lv = _support()
        _, text = _say(lv, ZoneState.EVALUATING, ZonePhase.ABSORBING,
                       _features(lv), 97_950.0)
        assert "barely moved" in text

    def test_thin_tape_is_called_out(self):
        lv = _support()
        _, text = _say(lv, ZoneState.EVALUATING, ZonePhase.WATCHING,
                       _features(lv, n=4), 97_950.0)
        assert "Too few trades" in text


class TestDiscipline:
    def test_absorbing_warns_that_it_is_not_an_entry(self):
        lv = _support()
        _, text = _say(lv, ZoneState.EVALUATING, ZonePhase.ABSORBING,
                       _features(lv), 97_950.0)
        assert "not an entry" in text

    @pytest.mark.parametrize("phase", list(ZonePhase))
    def test_it_never_gives_advice(self, phase):
        """It reports measurements. The reader supplies the conclusion."""
        lv = _support()
        out, text = _say(lv, ZoneState.EVALUATING, phase, _features(lv), 97_950.0)
        lowered = (text + " " + out["headline"]).lower()
        for word in ("you should", "buy now", "sell now", "take the trade",
                     "will go", "is going to", "recommend"):
            assert word not in lowered, f"advice leaked: {word!r}"

    @pytest.mark.parametrize("state", list(ZoneState))
    def test_every_state_produces_something_readable(self, state):
        lv = _support()
        out, _ = _say(lv, state, ZonePhase.WATCHING, _features(lv), 97_950.0)
        assert out["headline"] and isinstance(out["lines"], list) and out["lines"]
