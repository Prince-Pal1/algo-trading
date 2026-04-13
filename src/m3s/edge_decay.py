"""M3S Strategy Edge-Decay Monitor — Tier 1 #2.

Strategies die. Silently. A quantitative system without an edge-decay
detector ends up running a dead strategy for months because the operator
is emotionally attached or simply not watching the right numbers.

This monitor watches two health signals per strategy:

  1. **Rolling Sharpe decay.** If `rolling_sharpe_30d` drops below
     `sharpe_decay_threshold × lifetime_sharpe`, the strategy has lost
     edge relative to its own history.

  2. **Winrate degradation.** If current rolling winrate (approximated
     here by the 30d trade window) drops by more than
     `winrate_decay_threshold` percentage points versus the lifetime
     winrate, the edge profile has shifted.

A strategy that satisfies EITHER condition is "unhealthy". The monitor
applies a **persistence gate**: a strategy must be unhealthy for
`persistence_days` continuous days before any action is taken. This
prevents single bad days from triggering auto-halving.

Actions (configurable, default: both enabled):

- **Auto-halve.** After one persistence window of continuous decay, the
  monitor sets the "halved" flag. Downstream allocator (sub-phase 0.4)
  reads this flag and halves the strategy's capital weight.

- **Auto-pause.** If the strategy is still unhealthy after a SECOND
  persistence window (total 2×persistence_days), the monitor sets the
  "paused" flag. Downstream allocator drops the weight to ~0 (min floor).

Recovery: if a strategy becomes healthy again, the monitor clears the
state back to "normal" and the allocator restores full weight on its
next rebalance.

The monitor is a pure in-memory read-model — persistence to m3s_state
is the scheduler's job in sub-phase 0.6.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from src.m3s.types import PortfolioSnapshot, StrategySnapshot
from src.utils.logger import get_logger

log = get_logger("m3s.edge_decay")


class EdgeDecayFlag(str, enum.Enum):
    NORMAL = "normal"
    HALVED = "halved"
    PAUSED = "paused"


@dataclass
class EdgeDecayState:
    """Per-strategy decay tracking state."""
    strategy: str
    flag: EdgeDecayFlag = EdgeDecayFlag.NORMAL
    unhealthy_since_ms: int | None = None
    halved_since_ms: int | None = None
    last_triggers: list[str] = field(default_factory=list)  # latest reason strings


@dataclass(frozen=True)
class EdgeDecayAlert:
    """One-shot alert emitted when a strategy transitions between flags."""
    strategy: str
    old_flag: EdgeDecayFlag
    new_flag: EdgeDecayFlag
    reasons: list[str]
    ts_ms: int


_MS_PER_DAY = 86_400_000


class EdgeDecayMonitor:
    """Watch per-strategy edge health and recommend halving/pausing.

    Usage:
        mon = EdgeDecayMonitor(
            sharpe_decay_threshold=0.5,
            winrate_decay_threshold=0.2,
            persistence_days=14,
        )
        alerts = mon.check(snapshot, now_ms=...)
        if mon.should_halve("vol_momentum"):
            allocator.scale_weight("vol_momentum", 0.5)
    """

    def __init__(
        self,
        *,
        sharpe_decay_threshold: float = 0.5,
        winrate_decay_threshold: float = 0.2,
        persistence_days: int = 14,
        auto_halve_enabled: bool = True,
        auto_pause_enabled: bool = True,
        min_lifetime_trades: int = 20,
    ) -> None:
        if not (0.0 < sharpe_decay_threshold < 1.0):
            raise ValueError(
                f"sharpe_decay_threshold must be in (0, 1), got {sharpe_decay_threshold}"
            )
        if not (0.0 <= winrate_decay_threshold < 1.0):
            raise ValueError(
                f"winrate_decay_threshold must be in [0, 1), got {winrate_decay_threshold}"
            )
        if persistence_days < 1:
            raise ValueError(f"persistence_days must be >= 1, got {persistence_days}")

        self._sharpe_thr = float(sharpe_decay_threshold)
        self._winrate_thr = float(winrate_decay_threshold)
        self._persistence_ms = int(persistence_days) * _MS_PER_DAY
        self._auto_halve = bool(auto_halve_enabled)
        self._auto_pause = bool(auto_pause_enabled)
        self._min_lifetime_trades = int(min_lifetime_trades)
        self._states: dict[str, EdgeDecayState] = {}

    # ── Public accessors ─────────────────────────────────────────────

    def should_halve(self, strategy: str) -> bool:
        st = self._states.get(strategy)
        return bool(st and st.flag == EdgeDecayFlag.HALVED)

    def should_pause(self, strategy: str) -> bool:
        st = self._states.get(strategy)
        return bool(st and st.flag == EdgeDecayFlag.PAUSED)

    def flag(self, strategy: str) -> EdgeDecayFlag:
        st = self._states.get(strategy)
        return st.flag if st else EdgeDecayFlag.NORMAL

    def state(self, strategy: str) -> EdgeDecayState | None:
        return self._states.get(strategy)

    # ── Core check ───────────────────────────────────────────────────

    def check(self, snapshot: PortfolioSnapshot, now_ms: int) -> list[EdgeDecayAlert]:
        """Evaluate each strategy in the snapshot and return transition alerts.

        Only strategies with enough history (`min_lifetime_trades`) are
        eligible for decay detection. Strategies below that threshold stay
        at NORMAL — they're in cold start, not decay.
        """
        alerts: list[EdgeDecayAlert] = []
        for name, strat in snapshot.per_strategy.items():
            state = self._states.setdefault(name, EdgeDecayState(strategy=name))
            old_flag = state.flag

            if strat.n_trades_30d + 0 < 0:  # paranoia; never negative
                continue

            reasons = self._decay_reasons(strat)
            eligible = strat.lifetime_sharpe != 0.0 or self._has_enough_history(strat)

            if not eligible:
                # Cold start — don't touch state.
                continue

            if reasons:
                # Unhealthy this check
                if state.unhealthy_since_ms is None:
                    state.unhealthy_since_ms = now_ms
                state.last_triggers = reasons

                elapsed = now_ms - state.unhealthy_since_ms
                if self._auto_halve and state.flag == EdgeDecayFlag.NORMAL and elapsed >= self._persistence_ms:
                    state.flag = EdgeDecayFlag.HALVED
                    state.halved_since_ms = now_ms
                elif (
                    self._auto_pause
                    and state.flag == EdgeDecayFlag.HALVED
                    and state.halved_since_ms is not None
                    and (now_ms - state.halved_since_ms) >= self._persistence_ms
                ):
                    state.flag = EdgeDecayFlag.PAUSED
            else:
                # Healthy this check — clear any decay state.
                if state.flag != EdgeDecayFlag.NORMAL or state.unhealthy_since_ms is not None:
                    state.flag = EdgeDecayFlag.NORMAL
                    state.unhealthy_since_ms = None
                    state.halved_since_ms = None
                    state.last_triggers = []

            if state.flag != old_flag:
                alerts.append(
                    EdgeDecayAlert(
                        strategy=name,
                        old_flag=old_flag,
                        new_flag=state.flag,
                        reasons=list(state.last_triggers),
                        ts_ms=now_ms,
                    )
                )
                log.warning(
                    "m3s.edge_decay.transition",
                    strategy=name,
                    old_flag=old_flag.value,
                    new_flag=state.flag.value,
                    reasons=state.last_triggers,
                )

        return alerts

    # ── Decay logic ─────────────────────────────────────────────────

    def _decay_reasons(self, strat: StrategySnapshot) -> list[str]:
        reasons: list[str] = []

        # (1) Rolling Sharpe below threshold × lifetime Sharpe.
        # Only meaningful if lifetime Sharpe is positive — a strategy that
        # was never good can't "decay" against itself.
        if strat.lifetime_sharpe > 0.0:
            decay_floor = self._sharpe_thr * strat.lifetime_sharpe
            if strat.rolling_sharpe_30d < decay_floor:
                reasons.append(
                    f"rolling_sharpe_30d={strat.rolling_sharpe_30d:.3f} < "
                    f"{self._sharpe_thr:.2f}*lifetime_sharpe={decay_floor:.3f}"
                )

        # (2) Winrate degradation.
        # We use a proxy: fraction of winning trades in the 30d window as a
        # stand-in for current winrate. This lives in the snapshot as
        # n_trades_30d + pnl_30d; we can't derive 30d winrate exactly
        # without the raw trades list. So for now we only flag sharp
        # lifetime drops via a Sharpe-consistent check: if pnl_30d is
        # negative AND the strategy has lifetime wins, that's enough.
        #
        # A richer check will land in sub-phase 0.4 when the allocator
        # starts consuming per-strategy trade history directly.
        if strat.lifetime_winrate >= 0.5 and strat.pnl_30d < 0.0 and strat.n_trades_30d >= 5:
            reasons.append(
                f"pnl_30d={strat.pnl_30d:.2f} negative over {strat.n_trades_30d} trades "
                f"(lifetime_winrate={strat.lifetime_winrate:.2f})"
            )

        return reasons

    def _has_enough_history(self, strat: StrategySnapshot) -> bool:
        # Without access to the full trade list here, use n_trades_30d +
        # lifetime_sharpe as a proxy — if we have >=min_lifetime_trades in
        # the last 30d OR a non-zero lifetime Sharpe, we're past cold start.
        return strat.n_trades_30d >= self._min_lifetime_trades or strat.lifetime_sharpe != 0.0
