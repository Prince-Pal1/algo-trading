"""M3S Integration Hooks — the single surface touched by main.py in 0.8.

`M3S` is the composition facade: it holds the tracker, compounder, allocator,
edge-decay monitor, and conviction scorer, and exposes a tiny interface that
the trading engine wires in:

    on_signal(sig)       → mutate risk_pct (shrink only); never reject
    on_fill(fill, strat) → record per-bar exposure from the fill
    on_trade_close(...)  → update tracker + advance compounder base
    on_bar(...)          → record per-bar exposure direction
    snapshot()           → PortfolioSnapshot for dashboard / logs
    rebalance()          → recompute allocation (callable from scheduler)

## Invariants (enforced, not just documented)

1. **M3S never upscales a signal.** `risk_pct_after ≤ risk_pct_before`.
   If scalars would multiply above 1.0, the result is clamped to the
   original. This is the contract that makes M3S safe to install in
   front of the risk server.

2. **M3S never rejects a signal outright.** If edge-decay has paused a
   strategy, `risk_pct` is set to 0.0 and the signal passes through with
   zero size — the risk server still sees it and logs the decision.

3. **Shadow mode.** When `shadow_mode=True`, `on_signal` computes the
   proposed scaled risk_pct, logs it, but **returns the signal unchanged**.
   This is the default in sub-phase 0.1–0.7. Sub-phase 0.8 wires M3S in
   shadow=True so the engine runs with zero functional change.

4. **Cold-start allocation cache.** On boot, before `rebalance()` has run,
   `_last_alloc` is None. `on_signal` falls back to equal-weight across
   currently-known strategies to avoid zeroing everything out during the
   first few minutes of the engine's life.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.m3s.allocator import Allocator
from src.m3s.compounder import Compounder, TradeCloseEvent
from src.m3s.conviction import ConvictionScorer
from src.m3s.edge_decay import EdgeDecayMonitor
from src.m3s.leverage_grants import LeverageGrantStore
from src.m3s.modes import ModeConfig
from src.m3s.portfolio import PortfolioTracker
from src.m3s.types import AllocationDecision, PortfolioSnapshot
from src.utils.logger import get_logger
from src.utils.types import (
    LeverageGrant,
    LeverageReasonCode,
    Signal,
    SignalAction,
)

log = get_logger("m3s.hooks")


# ── Hook decision log (for observability / tests) ─────────────────────


@dataclass(frozen=True)
class SignalDecision:
    """What M3S did (or would have done) for one signal.

    Emitted from on_signal for every non-CLOSE/HOLD signal. Used by the
    dashboard (sub-phase 0.7) and pinned in the shadow-mode audit log.
    """
    strategy: str
    original_risk_pct: float
    scaled_risk_pct: float
    alloc_weight: float
    compound_scalar: float
    conviction_multiplier: float
    edge_decay_factor: float
    shadow: bool
    reasoning: str


# ── M3S composition facade ────────────────────────────────────────────


class M3S:
    """Facade that wires the six M3S components into a single interface."""

    def __init__(
        self,
        *,
        mode: ModeConfig,
        tracker: PortfolioTracker,
        compounder: Compounder,
        allocator: Allocator,
        edge_decay: EdgeDecayMonitor | None = None,
        conviction_scorer: ConvictionScorer | None = None,
        shadow_mode: bool = True,
        aggregate_leverage_cap: float = 100.0,
        leverage_grant_store: LeverageGrantStore | None = None,
    ) -> None:
        self._mode = mode
        self._tracker = tracker
        self._compounder = compounder
        self._allocator = allocator
        self._edge_decay = edge_decay
        self._conviction = conviction_scorer or ConvictionScorer()
        self._shadow_mode = bool(shadow_mode)

        # G.2b — leverage policy state
        self._aggregate_leverage_cap = float(aggregate_leverage_cap)
        self._leverage_grant_store = leverage_grant_store
        self._last_grants: list[LeverageGrant] = []

        self._last_alloc: AllocationDecision | None = None
        self._last_decisions: list[SignalDecision] = []

    # ── Introspection ───────────────────────────────────────────────

    @property
    def mode(self) -> ModeConfig:
        return self._mode

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    def set_shadow_mode(self, shadow: bool) -> None:
        log.info("m3s.hooks.shadow_set", shadow=bool(shadow))
        self._shadow_mode = bool(shadow)

    def set_mode(self, mode: ModeConfig) -> None:
        self._mode = mode
        self._compounder.set_mode(mode)
        self._allocator.set_mode(mode)

    def last_allocation(self) -> AllocationDecision | None:
        return self._last_alloc

    def last_decisions(self) -> list[SignalDecision]:
        return list(self._last_decisions)

    def last_grants(self) -> list[LeverageGrant]:
        return list(self._last_grants)

    # ── Leverage policy entrypoint (G.2b) ───────────────────────────

    def request_leverage(
        self,
        *,
        strategy_name: str,
        conviction: float,
        declared_range: tuple[float, float],
        current_aggregate_leverage: float = 0.0,
        reason: str = "",
        ts_ms: int | None = None,
    ) -> LeverageGrant:
        import time

        lo, hi = float(declared_range[0]), float(declared_range[1])
        if lo < 1.0:
            lo = 1.0
        if hi < lo:
            hi = lo

        conviction_clamped = max(0.0, min(1.0, float(conviction)))

        if ts_ms is None:
            ts_ms = self._tracker._last_equity_ts_ms or int(time.time() * 1000)

        snapshot = self._tracker.snapshot(now_ms=ts_ms)

        regime_target = self._compounder.target_leverage((lo, hi), snapshot)
        conviction_target = lo + (hi - lo) * conviction_clamped
        requested_raw = min(regime_target, conviction_target)

        headroom = max(0.0, self._aggregate_leverage_cap - current_aggregate_leverage)

        granted = min(requested_raw, headroom, hi)
        if granted < lo:
            granted = 0.0

        reason_code = self._classify_grant_reason(
            requested=requested_raw,
            granted=granted,
            regime_target=regime_target,
            conviction_target=conviction_target,
            headroom=headroom,
            declared_max=hi,
            declared_min=lo,
        )

        grant = LeverageGrant(
            strategy_name=strategy_name,
            ts_ms=int(snapshot.ts_ms),
            requested=float(requested_raw),
            granted=float(granted),
            reason=reason_code,
            conviction=conviction_clamped,
            declared_range_min=lo,
            declared_range_max=hi,
            regime_target=float(regime_target),
            conviction_target=float(conviction_target),
            aggregate_before=float(current_aggregate_leverage),
            aggregate_cap=float(self._aggregate_leverage_cap),
            m3s_regime=self._mode.name.value,
            user_reason=reason,
        )

        # Keep an in-memory ring of recent grants for dashboard / tests.
        self._last_grants.append(grant)
        if len(self._last_grants) > 500:
            self._last_grants = self._last_grants[-500:]

        # Persist to SQLite when a store is configured.
        if self._leverage_grant_store is not None:
            try:
                self._leverage_grant_store.append(grant)
            except Exception as e:
                log.warning(
                    "m3s.leverage_grant_store_failed",
                    strategy=strategy_name,
                    error=str(e),
                )

        log.info(
            "m3s.leverage_grant",
            strategy=strategy_name,
            requested=float(requested_raw),
            granted=float(granted),
            reason=reason_code.value,
            regime_target=float(regime_target),
            conviction_target=float(conviction_target),
            aggregate_before=float(current_aggregate_leverage),
            aggregate_cap=float(self._aggregate_leverage_cap),
            mode=self._mode.name.value,
        )

        return grant

    @staticmethod
    def _classify_grant_reason(
        *,
        requested: float,
        granted: float,
        regime_target: float,
        conviction_target: float,
        headroom: float,
        declared_max: float,
        declared_min: float,
    ) -> LeverageReasonCode:
        eps = 1e-9

        if granted + eps < requested:
            # Something reduced the requested value further.
            if abs(granted - headroom) < eps or headroom < requested:
                return LeverageReasonCode.CAPPED_BY_AGGREGATE
            if granted >= declared_max - eps:
                return LeverageReasonCode.CAPPED_BY_CAP
            return LeverageReasonCode.CAPPED_BY_AGGREGATE

        # granted ~= requested. Now see what made requested what it is.
        # If regime_target and conviction_target are equal within eps, we
        # consider it FULL regardless.
        if abs(regime_target - conviction_target) < eps:
            return LeverageReasonCode.FULL

        if regime_target < conviction_target and regime_target < declared_max - eps:
            return LeverageReasonCode.CAPPED_BY_REGIME
        if conviction_target < regime_target and conviction_target < declared_max - eps:
            return LeverageReasonCode.CAPPED_BY_CONVICTION

        return LeverageReasonCode.FULL

    # ── Primary entrypoint: on_signal ───────────────────────────────

    def on_signal(self, sig: Signal) -> Signal:
        """Mutate `sig.risk_pct` (or return unchanged in shadow mode).

        Pass-through for CLOSE/HOLD signals and for signals without a
        `risk_pct` (strategy is using the default sizer upstream).
        """
        if sig.action in (SignalAction.CLOSE, SignalAction.HOLD):
            return sig
        if sig.risk_pct is None or sig.risk_pct <= 0.0:
            return sig

        original_risk = float(sig.risk_pct)
        strategy = sig.strategy_name

        snapshot = self._tracker.snapshot(now_ms=sig.timestamp or self._tracker._last_equity_ts_ms)

        # (1) Allocation weight — from the last successful rebalance, or
        #     equal-weight fallback over the tracker's currently-known set.
        alloc_weight = self._alloc_weight_for(strategy, snapshot)

        # (2) Compounder risk scalar (vol target × pace × CVaR × DD).
        compound_scalar = self._compounder.risk_scalar(snapshot)

        # (3) Conviction multiplier.
        conviction = self._conviction.score(sig)

        # (4) Edge-decay flag: halved → 0.5x, paused → 0.0.
        edge_factor = self._edge_decay_factor(strategy)

        # Compose the scalar. Final multiplier is the product of everything.
        combined = alloc_weight * compound_scalar * conviction.multiplier * edge_factor
        # Never upscale above original — hard safety rail.
        scaled = max(0.0, min(1.0, combined)) * original_risk

        reasoning = (
            f"alloc={alloc_weight:.3f} cmp={compound_scalar:.3f} "
            f"conv={conviction.multiplier:.3f} edge={edge_factor:.2f} → "
            f"{original_risk:.4f} × {combined:.3f} = {scaled:.4f}"
        )

        decision = SignalDecision(
            strategy=strategy,
            original_risk_pct=original_risk,
            scaled_risk_pct=scaled,
            alloc_weight=alloc_weight,
            compound_scalar=compound_scalar,
            conviction_multiplier=conviction.multiplier,
            edge_decay_factor=edge_factor,
            shadow=self._shadow_mode,
            reasoning=reasoning,
        )
        self._last_decisions.append(decision)
        # Cap the log at 500 to prevent memory growth during a long run.
        if len(self._last_decisions) > 500:
            self._last_decisions = self._last_decisions[-500:]

        log.info(
            "m3s.signal_scaled",
            strategy=strategy,
            original=original_risk,
            scaled=scaled,
            alloc_weight=alloc_weight,
            compound_scalar=compound_scalar,
            conviction=conviction.multiplier,
            edge_factor=edge_factor,
            shadow=self._shadow_mode,
        )

        if self._shadow_mode:
            # Shadow mode: log but do NOT mutate the signal.
            return sig

        sig.risk_pct = scaled
        return sig

    # ── Event hooks ─────────────────────────────────────────────────

    def on_fill(self, fill_symbol: str, strategy: str, side_sign: int, ts_ms: int) -> None:
        """Record a fill's directional exposure for signal correlation.

        `side_sign` is -1 (short fill) / +1 (long fill) / 0 (close).
        """
        self._tracker.on_bar(strategy, fill_symbol, exposure=side_sign, ts_ms=ts_ms)

    def on_trade_close(
        self,
        strategy: str,
        pnl: float,
        symbol: str,
        ts_ms: int,
    ) -> None:
        """Record a closed trade. Updates tracker; triggers per-trade
        compounding when the current mode uses that cadence.
        """
        self._tracker.on_trade_close(strategy, pnl, symbol, ts_ms)
        if self._mode.compound_cadence == "per_trade":
            self._compounder.on_trade_close(
                TradeCloseEvent(ts_ms=ts_ms, strategy=strategy, pnl=pnl),
                self._tracker.snapshot(now_ms=ts_ms),
            )

    def on_bar(self, strategy: str, symbol: str, exposure: int, ts_ms: int) -> None:
        """Per-bar exposure update (+1 long / 0 flat / -1 short)."""
        self._tracker.on_bar(strategy, symbol, exposure=exposure, ts_ms=ts_ms)

    def snapshot(self, now_ms: int | None = None) -> PortfolioSnapshot:
        return self._tracker.snapshot(now_ms=now_ms)

    # ── Scheduled operations (called from scheduler in sub-phase 0.6) ──

    def rebalance(self, now_ms: int | None = None) -> AllocationDecision:
        """Recompute allocation + advance compounder base + edge-decay check.

        Returns the new AllocationDecision for logging/audit purposes. The
        decision is cached internally for `on_signal` to read from on the
        next tick.

        When `now_ms` is None and the tracker has no activity yet, fall back
        to the current wall-clock time so downstream audit events get a
        meaningful timestamp instead of 0.
        """
        import time
        if now_ms is None:
            now_ms = self._tracker._last_equity_ts_ms or int(time.time() * 1000)
        snap = self._tracker.snapshot(now_ms=now_ms)
        decision = self._allocator.compute(snap)
        self._last_alloc = decision
        self._compounder.update_base(snap, trigger="scheduled")
        if self._edge_decay is not None:
            self._edge_decay.check(snap, now_ms=snap.ts_ms)
        return decision

    # ── Internals ───────────────────────────────────────────────────

    def _alloc_weight_for(self, strategy: str, snapshot: PortfolioSnapshot) -> float:
        """Return the allocation weight for `strategy`.

        Priority:
        1. Recent `AllocationDecision` (post-rebalance) — use the stored weight.
        2. Fallback: equal-weight over {known strategies ∪ this signal's strategy},
           clamped by `mode.max_per_strategy_cap`. A fresh engine that has never
           seen `strategy` before gets `max_per_strategy_cap` as the default,
           which is the safest permissible value until the first rebalance runs.
        """
        if self._last_alloc is not None:
            w = self._last_alloc.weights.get(strategy)
            if w is not None:
                return float(w)

        known = set(snapshot.per_strategy.keys())
        # Include this signal's strategy even if the tracker hasn't seen it yet.
        known.add(strategy)
        equal = 1.0 / len(known)
        return min(equal, self._mode.max_per_strategy_cap)

    def _edge_decay_factor(self, strategy: str) -> float:
        """Translate edge-decay flag into a size multiplier."""
        if self._edge_decay is None:
            return 1.0
        if self._edge_decay.should_pause(strategy):
            return 0.0
        if self._edge_decay.should_halve(strategy):
            return 0.5
        return 1.0
