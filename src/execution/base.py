"""Base executor interface — the contract all executors implement.

Executors receive Signals from strategies and produce Fills.
Phase 2: PaperExecutor (simulated). Phase 4: BinanceExecutor, AlpacaExecutor, etc.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.utils.types import Fill, Position, Signal


class BaseExecutor(ABC):
    """Abstract base class for trade executors."""

    @abstractmethod
    async def execute(self, signal: Signal) -> Fill | None:
        """Execute a trading signal. Returns Fill if executed, None if rejected."""
        ...

    @abstractmethod
    async def get_positions(self) -> dict[str, Position]:
        """Get all open positions keyed by symbol."""
        ...

    @abstractmethod
    async def close_all(self) -> list[Fill]:
        """Emergency: close all open positions."""
        ...
