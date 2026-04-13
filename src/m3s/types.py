"""M3S data types — msgspec frozen structs for zero-copy serialization.

These are internal to M3S. The rest of the system speaks via Signal/Fill
(src.utils.types) and the M3S.on_signal() / on_fill() hooks bridge the two
in a later sub-phase.

Design rules:
- All snapshot/decision types are frozen (immutable) — they represent a
  point-in-time read of M3S state, not mutable state.
- CompoundState is mutable because it's the one write target during weekly
  close (see compounder.py, sub-phase 0.3).
- Field names follow the v1 plan section 5 interface sketches. Do not
  rename without updating docs/planning/m3s_plan_v1.md and any saved
  state (breaking change: migration row in m3s_state).
"""

from __future__ import annotations

import enum

import msgspec


# ── Event type enum (stable string keys; stored in m3s_events.event_type) ──


class M3SEventType(str, enum.Enum):
    ALLOCATION = "allocation"
    COMPOUND_UPDATE = "compound_update"
    MODE_TRANSITION = "mode_transition"
    STATE_CORRUPT = "state_corrupt"
    DD_FREEZE = "dd_freeze"
    DD_HALT = "dd_halt"
    STARTUP_DEGRADED = "startup_degraded"
    REGIME_CHANGE = "regime_change"                   # sub-phase 0.9
    EDGE_DECAY_TRIGGERED = "edge_decay_triggered"     # sub-phase 0.2
    CUSTOM_CONFIG_LOADED = "custom_config_loaded"     # CUSTOM mode traceability


# ── Snapshot types (frozen, read-only views of current state) ──


class StrategySnapshot(msgspec.Struct, frozen=True):
    """Per-strategy rolling stats used by allocator and compounder.

    All stats are forward-looking estimates: they describe the strategy
    *now*, based on the most recent window. Zero values mean "no data yet"
    (cold start); the allocator must handle that case with equal-weight
    fallback — see ModeConfig.cold_start_trades_required.
    """

    name: str
    n_trades_30d: int
    rolling_sharpe_30d: float
    realized_vol_30d: float
    pnl_30d: float
    # Optional lifetime stats (populated after enough history for edge-decay
    # detection in sub-phase 0.2). 0.0 when not yet available.
    lifetime_sharpe: float = 0.0
    lifetime_winrate: float = 0.0


class PortfolioSnapshot(msgspec.Struct, frozen=True):
    """Point-in-time view of the full portfolio state.

    Produced by PortfolioTracker.snapshot() (sub-phase 0.2) and consumed by
    Allocator.compute() (sub-phase 0.4) and Compounder.risk_scalar() (0.3).
    signal_corr uses stringified tuple keys ("strategy_a|strategy_b") because
    msgspec cannot serialize tuple keys.
    """

    ts_ms: int
    equity: float
    hwm: float
    drawdown_pct: float
    per_strategy: dict[str, StrategySnapshot]
    # Flat dict with "strat_a|strat_b" keys (sorted), values in [-1, 1].
    signal_corr: dict[str, float]


# ── Decision types (frozen, recorded into m3s_events) ──


class AllocationDecision(msgspec.Struct, frozen=True):
    """Output of Allocator.compute(). Logged verbatim to m3s_events so that
    any live weight can be traced back to the inputs that produced it.
    """

    ts_ms: int
    weights: dict[str, float]
    method: str                     # "cold_start_equal" | "inverse_vol" | "hrp_lite"
    inputs_hash: str                # stable hash of the snapshot inputs
    reasoning: str                  # one-sentence human-readable explanation


# ── Mutable state (the two scalars we care about for HWM-gated compounding) ──


class CompoundState(msgspec.Struct):
    """Mutable compounding state. Only Compounder.update_base() writes this.

    base_equity is the capital base for position sizing. It only moves UP
    (HWM gate). It is frozen during drawdown and during the auto-demote
    cooldown window (see ModeConfig.auto_demote_*).
    """

    base_equity: float
    hwm: float
    last_updated_ts_ms: int
    mode: str
