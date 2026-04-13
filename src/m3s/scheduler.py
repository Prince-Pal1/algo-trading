"""M3S Async Scheduler + Persistence.

Two responsibilities:

1. **Scheduler.** An async asyncio task that calls `M3S.rebalance()` on a
   fixed cadence (daily / weekly / per-minute for test). The scheduler is
   the ONLY component that advances compounder base on a clock; per-trade
   compounding (CUSTOM mode) bypasses the scheduler and goes via
   `M3S.on_trade_close` directly.

2. **Persistence.** `save_state` / `load_state` wrappers that flush the
   compounder state, the latest allocation decision, the active mode, and
   the edge-decay flags into `M3SStore` so the engine can crash-recover
   without losing the carefully-earned compounding base.

## Persistence layout

All M3S state lives in three namespaces in `m3s_state`:

    namespace  key       value
    ---------  --------  ----------------------------------------
    compound   state     {base_equity, hwm, last_updated_ts_ms, mode}
    allocation latest    {ts_ms, weights, method, inputs_hash, reasoning}
    mode       current   "STANDARD" | "CONSERVATIVE" | "GROWTH" | "CUSTOM"
    edge_decay flags     {strategy: {flag, unhealthy_since_ms, halved_since_ms}}

Events (every rebalance, every compound advance, every edge-decay
transition) are appended to `m3s_events` for audit and dashboard replay.

## Boot semantics

- If any namespace is missing on load, the loader fails gracefully: it logs
  `STARTUP_DEGRADED` and M3S boots with fresh state. The engine continues
  to run on fixed-weight fallback until the next rebalance populates the
  state again. Never fail closed — the risk server is the real safety rail.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import msgspec

from src.m3s.state import M3SStore
from src.m3s.types import AllocationDecision, CompoundState, M3SEventType
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.m3s.hooks import M3S

log = get_logger("m3s.scheduler")


# ══════════════════════════════════════════════════════════════════════
# Persistence helpers (import-time callables — not methods on M3S so
# M3S doesn't import M3SStore and risk circular deps).
# ══════════════════════════════════════════════════════════════════════


def save_state(m3s: "M3S", store: M3SStore) -> None:
    """Flush all M3S runtime state into the store.

    Called on every rebalance tick by the scheduler, and on graceful
    shutdown from main.py. Safe to call concurrently — each put is a
    transactional SQLite upsert.
    """
    # Compounder state (via the Compounder inside M3S)
    compound = m3s._compounder.state
    store.put(
        "compound",
        "state",
        {
            "base_equity": compound.base_equity,
            "hwm": compound.hwm,
            "last_updated_ts_ms": compound.last_updated_ts_ms,
            "mode": compound.mode,
        },
    )

    # Latest allocation
    alloc = m3s.last_allocation()
    if alloc is not None:
        store.put(
            "allocation",
            "latest",
            {
                "ts_ms": alloc.ts_ms,
                "weights": alloc.weights,
                "method": alloc.method,
                "inputs_hash": alloc.inputs_hash,
                "reasoning": alloc.reasoning,
            },
        )

    # Active mode
    store.put("mode", "current", m3s.mode.name.value)

    # Edge-decay flags (per strategy)
    if m3s._edge_decay is not None:
        flags_payload: dict[str, dict] = {}
        for name, state in m3s._edge_decay._states.items():
            flags_payload[name] = {
                "flag": state.flag.value,
                "unhealthy_since_ms": state.unhealthy_since_ms,
                "halved_since_ms": state.halved_since_ms,
                "last_triggers": list(state.last_triggers),
            }
        store.put("edge_decay", "flags", flags_payload)


def load_state(m3s: "M3S", store: M3SStore) -> bool:
    """Restore M3S state from the store. Returns True if anything loaded.

    Missing namespaces are treated as first-boot: log `STARTUP_DEGRADED` if
    partial, continue with fresh state if none. Never raise.
    """
    loaded_any = False

    # Compounder
    compound_raw = store.get("compound", "state")
    if compound_raw is not None:
        try:
            m3s._compounder._state = CompoundState(
                base_equity=float(compound_raw["base_equity"]),
                hwm=float(compound_raw["hwm"]),
                last_updated_ts_ms=int(compound_raw["last_updated_ts_ms"]),
                mode=str(compound_raw["mode"]),
            )
            loaded_any = True
        except (KeyError, TypeError, ValueError) as e:
            log.warning("m3s.state.compound_load_failed", error=str(e))

    # Allocation
    alloc_raw = store.get("allocation", "latest")
    if alloc_raw is not None:
        try:
            m3s._last_alloc = AllocationDecision(
                ts_ms=int(alloc_raw["ts_ms"]),
                weights={k: float(v) for k, v in alloc_raw["weights"].items()},
                method=str(alloc_raw["method"]),
                inputs_hash=str(alloc_raw["inputs_hash"]),
                reasoning=str(alloc_raw["reasoning"]),
            )
            loaded_any = True
        except (KeyError, TypeError, ValueError) as e:
            log.warning("m3s.state.alloc_load_failed", error=str(e))

    # Edge-decay flags
    flags_raw = store.get("edge_decay", "flags")
    if flags_raw is not None and m3s._edge_decay is not None:
        from src.m3s.edge_decay import EdgeDecayFlag, EdgeDecayState

        try:
            for name, f in flags_raw.items():
                m3s._edge_decay._states[name] = EdgeDecayState(
                    strategy=name,
                    flag=EdgeDecayFlag(f.get("flag", "normal")),
                    unhealthy_since_ms=f.get("unhealthy_since_ms"),
                    halved_since_ms=f.get("halved_since_ms"),
                    last_triggers=list(f.get("last_triggers", [])),
                )
            loaded_any = True
        except (KeyError, TypeError, ValueError) as e:
            log.warning("m3s.state.edge_decay_load_failed", error=str(e))

    if not loaded_any:
        log.info("m3s.state.first_boot_or_empty")
    return loaded_any


# ══════════════════════════════════════════════════════════════════════
# Scheduler
# ══════════════════════════════════════════════════════════════════════


class M3SScheduler:
    """Async scheduler wrapping `M3S.rebalance()` on a fixed cadence.

    Usage:
        scheduler = M3SScheduler(m3s, store, cadence_seconds=86400)
        task = asyncio.create_task(scheduler.run())
        # ... eventually ...
        scheduler.stop()
        await task
    """

    def __init__(
        self,
        m3s: "M3S",
        store: M3SStore,
        *,
        cadence_seconds: float = 86400.0,
    ) -> None:
        if cadence_seconds <= 0:
            raise ValueError(f"cadence_seconds must be > 0, got {cadence_seconds}")
        self._m3s = m3s
        self._store = store
        self._cadence = float(cadence_seconds)
        self._running = False
        self._tick_count = 0
        self._error_count = 0

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main loop. Exits when `stop()` is called."""
        self._running = True
        log.info("m3s.scheduler.start", cadence_seconds=self._cadence)
        while self._running:
            try:
                await asyncio.sleep(self._cadence)
            except asyncio.CancelledError:
                log.info("m3s.scheduler.cancelled")
                break
            if not self._running:
                break
            await self.tick()
        log.info("m3s.scheduler.stopped", ticks=self._tick_count)

    async def tick(self) -> None:
        """Run one rebalance cycle + persist. Catches and logs all errors."""
        self._tick_count += 1
        try:
            decision = self._m3s.rebalance()
            save_state(self._m3s, self._store)
            self._store.append_event(
                M3SEventType.ALLOCATION.value,
                {
                    "ts_ms": decision.ts_ms,
                    "method": decision.method,
                    "weights": decision.weights,
                    "inputs_hash": decision.inputs_hash,
                    "reasoning": decision.reasoning,
                },
                ts_ms=decision.ts_ms,
                inputs_hash=decision.inputs_hash,
            )
            log.info(
                "m3s.scheduler.tick_done",
                tick=self._tick_count,
                method=decision.method,
                n_weights=len(decision.weights),
            )
        except Exception as e:
            self._error_count += 1
            log.exception("m3s.scheduler.tick_failed", error=str(e))


# Convenience factory ── wires an M3S instance to a store path in one call ──


def make_scheduler(
    m3s: "M3S",
    *,
    db_path: str,
    cadence_seconds: float = 86400.0,
    restore: bool = True,
) -> tuple[M3SScheduler, M3SStore]:
    """Build an `(M3SScheduler, M3SStore)` pair.

    If `restore=True`, attempts to restore M3S state from the store before
    returning the scheduler. Used from main.py during engine boot.
    """
    store = M3SStore(db_path)
    if restore:
        load_state(m3s, store)
    scheduler = M3SScheduler(m3s, store, cadence_seconds=cadence_seconds)
    return scheduler, store
