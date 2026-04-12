"""Kill switch — global trading halt.

Checked FIRST before any other risk logic. When active, all new entries
are rejected. Only CLOSE signals pass through (must be able to reduce exposure).

Activated by: CLI, circuit breaker escalation, or manual trigger.
Deactivated by: explicit call only (never auto-resume).
"""

from __future__ import annotations

from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

from .state import RiskState

log = get_logger("kill_switch")


class KillSwitch:
    """Global kill switch — highest priority risk check."""

    def __init__(self, state: RiskState):
        self._state = state

    def check(self, signal: Signal) -> str | None:
        """Check if kill switch blocks this signal.

        Returns rejection reason string if blocked, None if OK to proceed.
        CLOSE signals always pass (must be able to exit positions).
        """
        if not self._state.kill_switch_active:
            return None

        if signal.action == SignalAction.CLOSE:
            return None  # Always allow exits

        return f"KILL_SWITCH_ACTIVE: {self._state.kill_switch_reason}"

    def activate(self, reason: str) -> None:
        """Activate kill switch — halts all new entries."""
        self._state.kill_switch_active = True
        self._state.kill_switch_reason = reason
        self._state.persist()
        log.critical("kill_switch_activated", reason=reason)

    def deactivate(self, reason: str) -> None:
        """Deactivate kill switch — only via explicit call."""
        self._state.kill_switch_active = False
        self._state.kill_switch_reason = ""
        self._state.persist()
        log.warning("kill_switch_deactivated", reason=reason)

    @property
    def is_active(self) -> bool:
        return self._state.kill_switch_active
