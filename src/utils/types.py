"""Shared data types used across all modules.

All types use msgspec.Struct for zero-copy deserialization and validation.
These are the canonical types — every module speaks this language.
"""

from __future__ import annotations

import enum
from datetime import datetime

import msgspec


# ── Enums ──────────────────────────────────────────────────────────────────


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class SignalAction(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


class RiskProfile(str, enum.Enum):
    SAFE = "SAFE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


class Timeframe(str, enum.Enum):
    TICK = "tick"
    S5 = "5s"
    S15 = "15s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


# ── Market Data ────────────────────────────────────────────────────────────


class Tick(msgspec.Struct, frozen=True):
    """Single trade tick from an exchange."""
    symbol: str
    price: float
    quantity: float
    timestamp: int              # Unix ms
    is_buyer_maker: bool


class Candle(msgspec.Struct, frozen=True):
    """OHLCV candle — the primary unit strategies operate on."""
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: int              # Unix ms, candle open time
    closed: bool                # True when candle is finalized


class OrderBookLevel(msgspec.Struct, frozen=True):
    price: float
    quantity: float


class OrderBookSnapshot(msgspec.Struct):
    symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp: int


# ── Signals ────────────────────────────────────────────────────────────────


class Signal(msgspec.Struct):
    """Output of BaseStrategy.on_candle() — a trading signal.

    G.0c (2026-04-13) added `leverage` and `margin_used_pct` fields for
    the leverage-first strategy architecture. They are optional — None
    preserves the pre-G.0c behavior (implicit 1× leverage).
    """
    symbol: str
    action: SignalAction
    confidence: float           # 0.0 to 1.0
    strategy_name: str
    timeframe: str
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_pct: float | None = None   # Suggested risk as fraction of equity
    metadata: dict | None = None
    timestamp: int = 0
    # G.0c — leverage is a first-class parameter
    leverage: float | None = None       # effective leverage requested (None = 1×)
    margin_used_pct: float | None = None  # % of account margin this position would consume


# ── M3S Leverage grants ──────────────────────────────────────────────


class LeverageReasonCode(str, enum.Enum):
    FULL = "full"
    CAPPED_BY_AGGREGATE = "capped_by_aggregate"
    CAPPED_BY_REGIME = "capped_by_regime"
    CAPPED_BY_CONVICTION = "capped_by_conviction"
    CAPPED_BY_CAP = "capped_by_cap"


class LeverageGrant(msgspec.Struct, frozen=True):
    strategy_name: str
    ts_ms: int
    requested: float
    granted: float
    reason: LeverageReasonCode
    conviction: float
    declared_range_min: float
    declared_range_max: float
    regime_target: float
    conviction_target: float
    aggregate_before: float
    aggregate_cap: float
    m3s_regime: str
    user_reason: str = ""


# ── Orders & Fills ─────────────────────────────────────────────────────────


class OrderRequest(msgspec.Struct):
    """What we send to the execution engine."""
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None = None      # For limit orders
    stop_price: float | None = None # For stop orders
    strategy_name: str = ""
    signal_id: str = ""


class Fill(msgspec.Struct):
    """Confirmation from the exchange."""
    order_id: str
    symbol: str
    side: Side
    price: float
    quantity: float
    commission: float
    timestamp: int
    exchange: str


# ── Portfolio ──────────────────────────────────────────────────────────────


class Position(msgspec.Struct):
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    strategy_name: str
    opened_at: int


# ── Risk Events ────────────────────────────────────────────────────────────


class RiskDecision(msgspec.Struct):
    approved: bool
    reason: str
    original_request: OrderRequest | None = None
    adjusted_quantity: float | None = None  # Risk may resize
    adjusted_risk_pct: float | None = None  # Kelly-computed risk fraction
    checks_passed: list[str] | None = None  # Audit trail of passed checks
    size_multiplier: float = 1.0            # Circuit breaker scaling factor
