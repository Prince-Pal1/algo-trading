"""Triple-barrier labeling (AFML ch. 3) + meta-label derivation.

Pure math. No storage, no DB. Used by:
- Phase 0 audit pipeline → `meta_label_from_outcome` converts the
  engine's realized trade outcome (entry, exit, barrier_hit) into a
  meta-label at trade close
- Phase 2 training pipeline → `triple_barrier_label` generates labels
  from historical price series for offline retraining + replay of
  vetoed signals
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Sequence


class BarrierHit(str, enum.Enum):
    """Which barrier closed the trade."""
    PROFIT_TARGET = "pt"        # upper (for LONG) / lower (for SHORT)
    STOP_LOSS = "sl"            # opposite barrier
    TIME = "time"               # horizon expired
    SIGNAL = "signal"           # strategy's own CLOSE signal (no barrier)


@dataclass(frozen=True)
class TripleBarrierResult:
    label: int                  # +1 / -1 / 0
    barrier_hit: BarrierHit
    exit_index: int             # index into the future_prices array
    exit_price: float


def triple_barrier_label(
    entry_price: float,
    direction: int,                 # +1 LONG / -1 SHORT
    future_prices: Sequence[float],  # future close prices, ordered
    pt_mult: float,                  # profit target multiplier
    sl_mult: float,                  # stop loss multiplier
    volatility: float,               # volatility estimate (e.g., ATR / close)
    horizon: int | None = None,      # max bars; None = use len(future_prices)
) -> TripleBarrierResult:
    """AFML triple-barrier label for one signal.

    For a LONG signal:
        upper = entry × (1 + pt_mult × vol)
        lower = entry × (1 − sl_mult × vol)
    For a SHORT signal:
        upper = entry × (1 + sl_mult × vol)  ← stop-loss side
        lower = entry × (1 − pt_mult × vol)  ← profit-target side

    Label semantics:
        +1 if PT barrier hit first
        −1 if SL barrier hit first
        sign(exit_price − entry_price) if time expires first

    For a SHORT signal, the sign is flipped so that +1 still means "the
    primary model's direction was correct." The caller (meta-labeling)
    converts this to meta_label = 1 if label == +1 else 0.

    Args:
        entry_price: trade entry fill price
        direction: +1 LONG, -1 SHORT, 0 invalid
        future_prices: close prices of future bars. Index 0 = bar after
            entry; len defines the horizon unless `horizon` is provided.
        pt_mult, sl_mult: barrier multipliers. Typical: pt=2, sl=1.
        volatility: fractional volatility estimate (ATR / price is fine).
            Must be > 0.
        horizon: optional cap; if None, uses len(future_prices).

    Returns:
        TripleBarrierResult with label, which barrier hit, exit index, price.
    """
    if direction not in (-1, 1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price}")
    if volatility <= 0:
        raise ValueError(f"volatility must be > 0, got {volatility}")
    if not future_prices:
        # Horizon expires immediately — treat as no-movement timeout
        return TripleBarrierResult(
            label=0,
            barrier_hit=BarrierHit.TIME,
            exit_index=0,
            exit_price=entry_price,
        )

    n = len(future_prices) if horizon is None else min(horizon, len(future_prices))

    # Barrier levels (in absolute price units)
    if direction == 1:
        pt_level = entry_price * (1.0 + pt_mult * volatility)
        sl_level = entry_price * (1.0 - sl_mult * volatility)
    else:  # direction == -1
        pt_level = entry_price * (1.0 - pt_mult * volatility)
        sl_level = entry_price * (1.0 + sl_mult * volatility)

    for i in range(n):
        p = float(future_prices[i])
        if direction == 1:
            if p >= pt_level:
                return TripleBarrierResult(+1, BarrierHit.PROFIT_TARGET, i, p)
            if p <= sl_level:
                return TripleBarrierResult(-1, BarrierHit.STOP_LOSS, i, p)
        else:  # SHORT
            if p <= pt_level:
                return TripleBarrierResult(+1, BarrierHit.PROFIT_TARGET, i, p)
            if p >= sl_level:
                return TripleBarrierResult(-1, BarrierHit.STOP_LOSS, i, p)

    # Horizon expired — sign of residual return in the direction's favor
    exit_p = float(future_prices[n - 1])
    residual = (exit_p - entry_price) * direction
    if residual > 0:
        label = +1
    elif residual < 0:
        label = -1
    else:
        label = 0
    return TripleBarrierResult(label, BarrierHit.TIME, n - 1, exit_p)


def meta_label_from_outcome(
    direction: int,
    entry_price: float,
    exit_price: float,
    barrier_hit: str,
) -> tuple[int, int]:
    """Convert a realized trade outcome into (triple_barrier_label, meta_label).

    Used at trade close in the engine — when we already know the entry,
    exit, and which barrier closed the trade, we don't need to replay
    the triple barrier math; the engine's decision IS the triple-barrier
    decision.

    Args:
        direction: +1 LONG, -1 SHORT
        entry_price: fill price on open
        exit_price: fill price on close
        barrier_hit: one of 'pt', 'sl', 'time', 'signal'

    Returns:
        (tb_label, meta_label) where:
            tb_label ∈ {-1, 0, +1}
            meta_label ∈ {0, 1}   (1 if tb_label == direction)
    """
    if direction not in (-1, 1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")

    if barrier_hit == BarrierHit.PROFIT_TARGET.value:
        tb = +1
    elif barrier_hit == BarrierHit.STOP_LOSS.value:
        tb = -1
    else:
        # time expiry OR strategy-driven CLOSE — use residual sign
        residual = (exit_price - entry_price) * direction
        if residual > 0:
            tb = +1
        elif residual < 0:
            tb = -1
        else:
            tb = 0

    meta = 1 if tb == +1 else 0  # +1 = direction correct; anything else = wrong
    return tb, meta
