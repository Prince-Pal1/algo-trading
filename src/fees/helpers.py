"""Convenience helpers that bundle common cost-query patterns.

The raw FeeManager API takes explicit (symbol, qty_lots, style, hold_hours,
mid_price) — these helpers accept higher-level objects (Signal, Strategy)
and fill in the details.

Public helpers:

    cost_for_signal(signal, style, qty_lots=None, hold_hours=None,
                    broker_id=None) → CostProjection

    cost_for_strategy_signal(strategy, signal, qty_lots=None,
                             hold_hours=None) → CostProjection

    explain_for_signal(signal, style) → dict

These are the "every part of the system can pull info automatically"
convenience layer — called from RiskManager.projected_cost(), from the
dashboard, from backtest reporting, etc.
"""

from __future__ import annotations

from typing import Any

from src.fees.manager import CostProjection, FeeManager
from src.fees.scenario import ScenarioName


def _qty_lots_from_signal(signal: Any, default: float = 1.0) -> float:
    """Derive qty (in lots) from a Signal. Signal.metadata may carry
    'quantity_lots' or the raw risk_pct — we accept either and fall back."""
    if signal is None:
        return default
    metadata = getattr(signal, "metadata", None) or {}
    qty = metadata.get("quantity_lots") if isinstance(metadata, dict) else None
    if qty is not None:
        return float(qty)
    # Raw quantity (might be in different units; caller should verify)
    raw = metadata.get("quantity") if isinstance(metadata, dict) else None
    if raw is not None:
        return float(raw)
    return default


def cost_for_signal(
    signal: Any,
    style: str = "intraday",
    *,
    qty_lots: float | None = None,
    hold_hours: float | None = None,
    mid_price: float | None = None,
    broker_id: str | None = None,
    scenario: ScenarioName | None = None,
) -> CostProjection:
    """Project cost for an intended Signal.

    Infers:
      - symbol from signal.symbol
      - mid_price from signal.entry_price (if not passed explicitly)
      - timestamp from signal.timestamp for scenario auto-detect
      - qty_lots from signal.metadata['quantity_lots'] (fallback 1.0)
      - side from SignalAction (LONG/SHORT; CLOSE falls back to 'long')
    """
    symbol = getattr(signal, "symbol", None)
    if not symbol:
        raise ValueError("signal must have a .symbol attribute")

    resolved_qty = qty_lots if qty_lots is not None else _qty_lots_from_signal(signal)

    if mid_price is None:
        mid_price = float(getattr(signal, "entry_price", 0.0) or 0.0)
    if mid_price <= 0:
        raise ValueError(
            f"cost_for_signal needs a positive mid_price (signal.entry_price={getattr(signal,'entry_price',None)!r})"
        )

    timestamp_ms = getattr(signal, "timestamp", None)
    # Signal.timestamp is int; sometimes 0 if unset → treat 0 as "unknown"
    if timestamp_ms == 0:
        timestamp_ms = None

    # Side: derive from action enum if available
    action = getattr(signal, "action", None)
    side = "long"
    if action is not None:
        action_name = getattr(action, "name", str(action)).upper()
        if "SHORT" in action_name:
            side = "short"

    return FeeManager.project_cost(
        symbol=symbol,
        qty_lots=resolved_qty,
        style=style,
        hold_hours=hold_hours,
        mid_price=mid_price,
        scenario=scenario,
        timestamp_ms=timestamp_ms,
        broker_id=broker_id,
        side=side,
    )


def cost_for_strategy_signal(
    strategy: Any,
    signal: Any,
    *,
    qty_lots: float | None = None,
    hold_hours: float | None = None,
    mid_price: float | None = None,
    broker_id: str | None = None,
    scenario: ScenarioName | None = None,
) -> CostProjection:
    """Same as cost_for_signal but pulls style from strategy.fee_style."""
    style = getattr(strategy, "fee_style", "intraday")
    return cost_for_signal(
        signal=signal,
        style=style,
        qty_lots=qty_lots,
        hold_hours=hold_hours,
        mid_price=mid_price,
        broker_id=broker_id,
        scenario=scenario,
    )


def explain_for_signal(signal: Any, style: str = "intraday") -> dict:
    """Return the FeeManager.explain() dict for this signal's context."""
    symbol = getattr(signal, "symbol", None)
    if not symbol:
        raise ValueError("signal must have .symbol")
    timestamp_ms = getattr(signal, "timestamp", None)
    if timestamp_ms == 0:
        timestamp_ms = None
    return FeeManager.explain(
        symbol=symbol, style=style, timestamp_ms=timestamp_ms,
    )
