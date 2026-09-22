"""Rule-based evidence scorer.

Deliberately rules, not machine learning, and that is not a placeholder.

1. There is no labelled data yet. ML needs outcomes this system has not
   collected.
2. Rules are debuggable. When a signal is wrong you can read exactly which rule
   fired and why.
3. **The rules generate the labels.** Every fired signal plus its outcome is a
   training row. The ML step cannot come first; it is downstream of running
   this for months.

The threshold is the weakest part of the whole system and it is a guess until
outcomes calibrate it. Treat `DEFAULT_THRESHOLD` as a starting placeholder, not
a tuned parameter.


WEIGHTING: TAPE OVER BOOK
-------------------------
Executed flow (delta, absorption) is weighted roughly 2x resting liquidity
(walls, imbalance). Crypto book display is heavily spoofed and largely
unpoliced — a wall can be pulled the instant price arrives — whereas a trade
that printed cannot be un-printed. Book evidence is supporting, never leading.

Every report carries BOTH sides. A scorer that only surfaces confirming evidence
manufactures confidence, which is the specific way tools like this lose money.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.flow.features import ZoneFeatures
from src.flow.level_registry import Level, LevelSide

# Placeholder until outcomes calibrate it. See module docstring.
DEFAULT_THRESHOLD = 0.62

# Rule weights. Tape-derived rules dominate by design.
W_ABSORPTION = 0.30
W_LATE_TURN = 0.15
W_ADVERSE = 0.15
W_BREAK = 0.30
W_BOOK_SIZE = 0.08
W_BOOK_REFILL = 0.10
W_BOOK_IMBALANCE = 0.07
W_REPEAT_TEST = 0.12


@dataclass(frozen=True)
class EvidenceItem:
    name: str
    direction: int        # +1 supports the level holding, -1 argues against
    weight: float         # magnitude of contribution
    detail: str

    @property
    def contribution(self) -> float:
        return self.direction * self.weight


@dataclass
class EvidenceReport:
    """Scored evidence, with the reasoning attached."""

    score: float                  # [0, 1]; 0.5 = no evidence either way
    items: list[EvidenceItem]
    threshold: float
    sufficient: bool              # enough activity for the score to mean anything
    side: LevelSide

    @property
    def supporting(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.direction > 0]

    @property
    def opposing(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.direction < 0]

    @property
    def confirmed(self) -> bool:
        return self.sufficient and self.score >= self.threshold

    def summary(self) -> str:
        state = "CONFIRMED" if self.confirmed else (
            "insufficient activity" if not self.sufficient else "below threshold"
        )
        return f"{self.side.value.upper()} score {self.score:.2f}/{self.threshold:.2f} — {state}"


def score_zone(
    level: Level,
    features: ZoneFeatures,
    threshold: float = DEFAULT_THRESHOLD,
    absorption_floor: float = 0.25,
    adverse_tolerance: float = 0.5,
) -> EvidenceReport:
    """Turn zone features into scored, explained evidence.

    Args:
        level: the level being tested.
        features: accumulated flow inside its zone.
        threshold: score at or above which the report reads CONFIRMED.
        absorption_floor: minimum absorption to count as evidence at all.
        adverse_tolerance: adverse excursion beyond this fraction of the zone
            half-width counts against the level.

    Returns:
        An `EvidenceReport`. Note it always scores; `sufficient` is what says
        whether the score should be acted on.
    """
    is_support = level.side is LevelSide.LONG
    items: list[EvidenceItem] = []

    # ── Tape: absorption ───────────────────────────────────────────────
    # At support, bullish absorption is aggressive SELLING that failed to move
    # price. At resistance, aggressive BUYING that failed. See features.py.
    aggression_is_opposing = (
        features.delta_ratio < 0 if is_support else features.delta_ratio > 0
    )
    if features.absorption >= absorption_floor and aggression_is_opposing:
        items.append(EvidenceItem(
            "absorption", +1, W_ABSORPTION * min(1.0, features.absorption / 0.5),
            f"{'sell' if is_support else 'buy'} delta {features.delta:+,.2f} "
            f"absorbed — range {features.range_ratio:.2f}x normal",
        ))
    elif features.absorption >= absorption_floor:
        # One-sided aggression in the level's own direction that still isn't
        # moving price: the level is being leaned on, not defended.
        items.append(EvidenceItem(
            "absorption_wrong_side", -1, W_ABSORPTION * 0.5,
            f"one-sided flow {features.delta_ratio:+.2f} with no follow-through",
        ))

    # ── Tape: late flow turning toward the level's direction ───────────
    late_favours = (
        features.late_delta_ratio > 0.15 if is_support else features.late_delta_ratio < -0.15
    )
    if late_favours:
        items.append(EvidenceItem(
            "late_flow_turn", +1, W_LATE_TURN,
            f"recent delta ratio {features.late_delta_ratio:+.2f} turning "
            f"{'up' if is_support else 'down'}",
        ))

    # ── Tape: break in progress ────────────────────────────────────────
    # Heavy opposing aggression that IS moving price is the opposite of
    # absorption — the level is failing right now.
    breaking = (
        (features.delta_ratio < -0.30 if is_support else features.delta_ratio > 0.30)
        and features.range_ratio > 1.2
    )
    if breaking:
        items.append(EvidenceItem(
            "breaking", -1, W_BREAK,
            f"delta {features.delta_ratio:+.2f} with range {features.range_ratio:.2f}x "
            "normal — level giving way",
        ))

    # ── Structure: adverse excursion ───────────────────────────────────
    adverse_limit = level.width * adverse_tolerance
    if features.adverse_excursion > adverse_limit:
        items.append(EvidenceItem(
            "adverse_excursion", -1,
            W_ADVERSE * min(1.0, features.adverse_excursion / max(level.width, 1e-9)),
            f"price pushed {features.adverse_excursion:.2f} past the level "
            f"(tolerance {adverse_limit:.2f})",
        ))
    elif features.dwell_ms > 0:
        items.append(EvidenceItem(
            "level_holding", +1, W_ADVERSE * 0.6,
            f"adverse excursion only {features.adverse_excursion:.2f}",
        ))

    # ── Structure: repeated tests weaken a level ───────────────────────
    if features.test_count >= 3:
        items.append(EvidenceItem(
            "repeated_test", -1, W_REPEAT_TEST,
            f"test #{features.test_count} this session — levels thin with retesting",
        ))

    # ── Book: supporting only, never leading (spoofable) ───────────────
    if features.has_book:
        if features.book_refills >= 2:
            items.append(EvidenceItem(
                "book_refills", +1, W_BOOK_REFILL,
                f"resting size replenished {features.book_refills}x — iceberg behaviour",
            ))
        imbalance_favours = (
            features.book_imbalance > 0.20 if is_support else features.book_imbalance < -0.20
        )
        if imbalance_favours:
            items.append(EvidenceItem(
                "book_imbalance", +1, W_BOOK_IMBALANCE,
                f"book imbalance {features.book_imbalance:+.2f} favours the level",
            ))
        elif abs(features.book_imbalance) > 0.20:
            items.append(EvidenceItem(
                "book_imbalance_against", -1, W_BOOK_IMBALANCE,
                f"book imbalance {features.book_imbalance:+.2f} leans the other way",
            ))
        if features.book_size_at_level > 0:
            items.append(EvidenceItem(
                "book_size", +1, W_BOOK_SIZE,
                f"{features.book_size_at_level:,.2f} resting at the level",
            ))

    score = 0.5 + sum(item.contribution for item in items)
    score = max(0.0, min(1.0, score))

    return EvidenceReport(
        score=score,
        items=items,
        threshold=threshold,
        sufficient=features.sufficient,
        side=level.side,
    )
