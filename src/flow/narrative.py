"""Plain-English read of what is happening at a level, right now.

The evidence list says WHICH rules fired. This says what the situation IS — the
connective tissue a trader would say out loud, in the order they would say it:
where price is, who is pushing, whether it is working, for how long, and what
has changed in the last half minute.

It states only what the features measured. No prediction, no advice, no "looks
bullish" — the reader supplies the conclusion, which is the same division of
labour as the rest of this system: the human brings the context, the machine
brings the measurement.

Kept server-side rather than in the page so it can be tested and logged. It runs
at UI cadence, never on the tick path.
"""

from __future__ import annotations

from src.flow.features import ZoneFeatures
from src.flow.level_registry import Level, LevelSide
from src.flow.zone_state import ZonePhase, ZoneState

# Below this the delta is not one-sided enough to name a side.
BALANCED_DELTA = 0.12
# |delta_ratio| above this is worth calling heavy rather than merely net.
HEAVY_DELTA = 0.45
# range_ratio under this is "price barely moved" — the absorption tell.
HELD_RANGE = 0.6
# range_ratio over this is "price is travelling".
WIDE_RANGE = 1.2


def _dur(ms: int) -> str:
    s = max(0, ms) // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if s else f"{m}m"


def _num(x: float, dp: int = 2) -> str:
    return f"{x:,.{dp}f}"


def describe(
    level: Level,
    state: ZoneState,
    phase: ZonePhase,
    features: ZoneFeatures | None,
    last_price: float,
    distance: float,
) -> dict:
    """Return {headline, lines[], invalidates} — short sentences, in reading order."""
    is_support = level.side is LevelSide.LONG
    side_word = "support" if is_support else "resistance"

    # APPROACHING is checked FIRST: there is no accumulator until price is
    # inside, so `features is None` is the normal case there. Ordering these the
    # other way round made every approach read "Waiting" — the one moment the
    # page most needs to say something different.
    if state is ZoneState.APPROACHING:
        return {
            "headline": "Approaching",
            "lines": [
                f"Price is {_num(distance)} from your {side_word} zone "
                f"({_num(level.low)}–{_num(level.high)}) and closing.",
                "Evidence starts accumulating when it enters.",
            ],
            "invalidates": "",
        }

    if state is ZoneState.IDLE or features is None:
        where = "below" if last_price < level.low else "above"
        return {
            "headline": "Waiting",
            "lines": [
                f"Price is {_num(distance)} {where} the zone. Nothing is measured "
                f"until price is inside it.",
            ],
            "invalidates": "",
        }

    lines: list[str] = []

    # 1. where price sits — thirds inside, named explicitly outside. Clamping a
    # price that has left the zone into "upper third" would describe the wrong
    # situation entirely, and the moment price leaves is the interesting one.
    zone = f"({_num(level.low)}–{_num(level.high)})"
    if last_price > level.high:
        lines.append(
            f"Price is {_num(last_price - level.high)} ABOVE your zone {zone} — "
            + ("it has come back through the level."
               if is_support else "it is through the level the wrong way.")
        )
    elif last_price < level.low:
        lines.append(
            f"Price is {_num(level.low - last_price)} BELOW your zone {zone} — "
            + ("it is through the level the wrong way."
               if is_support else "it has come back through the level.")
        )
    else:
        span = max(level.high - level.low, 1e-9)
        frac = (last_price - level.low) / span
        third = "lower third" if frac < 0.34 else (
            "upper third" if frac > 0.66 else "middle")
        lines.append(
            f"Price is in the {third} of your zone {zone}, "
            f"{_num(abs(last_price - level.price))} "
            f"{'below' if last_price < level.price else 'above'} the level."
        )

    # 2. who is pushing, and whether it is working
    dr = features.delta_ratio
    if abs(dr) < BALANCED_DELTA:
        pressure = "Buying and selling are roughly balanced"
    else:
        who = "Sellers" if dr < 0 else "Buyers"
        weight = "heavily" if abs(dr) >= HEAVY_DELTA else "net"
        pressure = f"{who} are {weight} in control ({dr:+.0%} of volume)"

    if features.range_ratio < HELD_RANGE:
        result = f"and price has barely moved ({features.range_ratio:.1f}x its normal range)"
    elif features.range_ratio > WIDE_RANGE:
        result = f"and price is travelling with them ({features.range_ratio:.1f}x normal range)"
    else:
        result = f"with price moving normally ({features.range_ratio:.1f}x range)"
    lines.append(f"{pressure} {result}, over {_dur(features.dwell_ms)} and "
                 f"{features.trade_count:,} trades.")

    if not features.sufficient:
        lines.append("Too few trades so far to read anything into it.")

    # 3. the last 30 seconds, which is what the turn is judged on
    late = features.late_delta_ratio
    if abs(late) >= BALANCED_DELTA:
        recent = "buying" if late > 0 else "selling"
        turned = (late > 0) == is_support
        lines.append(
            f"The last 30 seconds is net {recent} ({late:+.0%})"
            + (" — the side you need." if turned else " — against you.")
        )
    else:
        lines.append("The last 30 seconds is flat — no side is pressing.")

    # 4. the specifics worth saying out loud
    if features.iceberg_ratio >= 3:
        lines.append(
            f"{_num(features.iceberg_traded)} has traded at {_num(features.iceberg_price)} "
            f"against {_num(features.iceberg_displayed)} ever shown — hidden size is "
            f"defending that price."
        )
    if features.probe_count > 1:
        pct = features.retest_volume_ratio
        lines.append(
            f"Probe {features.probe_count} of the extreme traded {pct:.0%} of the first"
            + (" — the aggressors are spending out." if pct < 0.7
               else " — heavier than the first, this is a real attack.")
        )
    if features.adverse_excursion > 0:
        through = features.adverse_excursion / max(level.width, 1e-9)
        lines.append(
            f"Price has pushed {_num(features.adverse_excursion)} past the level — "
            f"{through:.0%} of the way to failing it."
        )

    headline = {
        ZonePhase.WATCHING: "In the zone",
        ZonePhase.ABSORBING: "Absorbing — someone is defending",
        ZonePhase.TURNING: "Turning — the aggressors are giving up",
        ZonePhase.FAILING: "Failing — the level is going",
    }[phase]
    if state is ZoneState.CONFIRMED:
        headline = "Confirmed — " + headline.split(" — ")[-1]
    elif state is ZoneState.INVALIDATED:
        headline = "Invalidated — the level broke"

    if phase is ZonePhase.ABSORBING:
        lines.append(
            "Absorption alone is not an entry: a defender can hold for twenty "
            "minutes and then step away. The turn is what you are waiting for."
        )

    return {"headline": headline, "lines": lines, "invalidates": ""}
