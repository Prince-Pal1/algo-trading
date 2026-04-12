"""Risk management module — pre-trade checks, position sizing, circuit breakers.

Public API:
    RiskManager    — orchestrates all risk checks
    RiskClient     — ZMQ client for live/paper trading
    InlineRiskClient — direct wrapper for backtests (no ZMQ)
    RiskConfig     — typed config from risk.toml
    RiskState      — in-memory state with SQLite persistence
"""

from .client import InlineRiskClient, RiskClient
from .config import RiskConfig
from .manager import RiskManager
from .state import RiskState

__all__ = [
    "RiskManager",
    "RiskClient",
    "InlineRiskClient",
    "RiskConfig",
    "RiskState",
]
