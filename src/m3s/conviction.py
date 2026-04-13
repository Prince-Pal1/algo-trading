"""M3S Conviction Scorer — Tier 1 #3 (signal conviction-weighted sizing).

Not all signals from the same strategy have equal expected value. A breakout
on 3× volume is a stronger setup than a breakout on 0.5× volume. Currently
every strategy emits a flat `risk_pct` independent of signal quality. The
conviction scorer multiplies that `risk_pct` by a quality score derived from
the signal's context — before the compounder and allocator touch it.

Scoring inputs (all optional; missing fields default to neutral):
- `signal.confidence` — built into every Signal, already a [0,1] float.
- `signal.metadata["volume_z"]` — trade volume z-score vs 20-bar average.
- `signal.metadata["trigger_distance_atr"]` — how far from the entry level
  (in ATR units) the current price is; closer = cleaner setup.
- `signal.metadata["mtf_aligned"]` — bool; is a higher TF (e.g., 4h) also
  in the same direction as this 1h signal?

Output:
- A multiplier in `[multiplier_floor, multiplier_ceiling]` (default [0.5, 1.3]).
- Can shrink below 1.0 (low quality signal) or boost above 1.0 (high quality).
- The final post-conviction risk_pct is still clamped by the M3S hook layer
  so it never exceeds the original signal's risk_pct — conviction never
  boosts a signal above its unscaled baseline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.utils.logger import get_logger
from src.utils.types import Signal

log = get_logger("m3s.conviction")


@dataclass(frozen=True)
class ConvictionScore:
    """Structured output — `multiplier` is the field the hooks layer uses,
    the rest are for logging/dashboard surfacing."""
    multiplier: float
    confidence_component: float
    volume_component: float
    distance_component: float
    mtf_component: float
    reasoning: str


class ConvictionScorer:
    """Default distance/volume/MTF-aware conviction scorer.

    The components are combined multiplicatively (each ≈ 1.0 is neutral):
        multiplier = conf_c * vol_c * dist_c * mtf_c
    then clamped to [floor, ceiling].
    """

    def __init__(
        self,
        *,
        multiplier_floor: float = 0.5,
        multiplier_ceiling: float = 1.3,
        enabled: bool = True,
    ) -> None:
        if multiplier_floor <= 0:
            raise ValueError(f"multiplier_floor must be > 0, got {multiplier_floor}")
        if multiplier_ceiling < multiplier_floor:
            raise ValueError(
                f"multiplier_ceiling ({multiplier_ceiling}) must be >= "
                f"multiplier_floor ({multiplier_floor})"
            )
        self._floor = float(multiplier_floor)
        self._ceiling = float(multiplier_ceiling)
        self._enabled = bool(enabled)

    def score(self, signal: Signal) -> ConvictionScore:
        """Produce a conviction multiplier for `signal`.

        If `enabled=False`, always returns 1.0 (pass-through). Missing
        metadata fields default to neutral (1.0) — signals from strategies
        that don't populate metadata get a multiplier equal to their
        confidence clamped to the configured range.
        """
        if not self._enabled:
            return ConvictionScore(
                multiplier=1.0,
                confidence_component=1.0,
                volume_component=1.0,
                distance_component=1.0,
                mtf_component=1.0,
                reasoning="disabled",
            )

        meta = signal.metadata or {}

        # (1) Confidence: signal.confidence in [0, 1] → map to [0.7, 1.2]
        # (a 0.9 confidence gets 1.1x; a 0.5 gets 0.85x).
        conf = float(signal.confidence or 0.0)
        conf_c = 0.7 + 0.5 * max(0.0, min(1.0, conf))

        # (2) Volume z-score: >1 = above avg, scaled to [0.8, 1.2]
        vol_z_raw = meta.get("volume_z")
        if vol_z_raw is None:
            vol_c = 1.0
        else:
            vol_z = float(vol_z_raw)
            vol_c = 1.0 + max(-0.2, min(0.2, 0.1 * vol_z))

        # (3) Trigger distance in ATR units: closer (smaller) = cleaner
        # setup. 0 ATR = perfect, 2 ATR = max penalty.
        dist_raw = meta.get("trigger_distance_atr")
        if dist_raw is None:
            dist_c = 1.0
        else:
            dist = abs(float(dist_raw))
            dist_c = max(0.8, 1.1 - 0.15 * dist)

        # (4) MTF alignment: strong yes/no signal
        mtf = meta.get("mtf_aligned")
        if mtf is None:
            mtf_c = 1.0
        elif bool(mtf):
            mtf_c = 1.1
        else:
            mtf_c = 0.9

        raw = conf_c * vol_c * dist_c * mtf_c
        multiplier = max(self._floor, min(self._ceiling, raw))

        reasoning = (
            f"conf={conf:.2f}×{conf_c:.3f} "
            f"vol_z={meta.get('volume_z', 'n/a')}×{vol_c:.3f} "
            f"dist={meta.get('trigger_distance_atr', 'n/a')}×{dist_c:.3f} "
            f"mtf={meta.get('mtf_aligned', 'n/a')}×{mtf_c:.3f} "
            f"→ raw={raw:.3f} clamped={multiplier:.3f}"
        )
        if not math.isfinite(multiplier):
            multiplier = 1.0
            reasoning += " [nonfinite-fallback]"

        return ConvictionScore(
            multiplier=multiplier,
            confidence_component=conf_c,
            volume_component=vol_c,
            distance_component=dist_c,
            mtf_component=mtf_c,
            reasoning=reasoning,
        )
