"""Fat finger guard — order sanity checks.

Prevents obviously wrong orders:
- Notional value exceeds max order size
- Quantity exceeds N× average trade size
- Entry price deviates > 2% from last known price
"""

from __future__ import annotations

from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

from .config import RiskConfig
from .state import RiskState

log = get_logger("fat_finger")

_PRICE_DEVIATION_PCT = 0.02  # 2% max deviation from last known


class FatFingerGuard:
    """Sanity checks on order values and prices."""

    def __init__(self, config: RiskConfig, state: RiskState):
        self._cfg = config
        self._state = state
        # Restore running-average state from RiskState (which loaded it from
        # the risk_state SQLite table). Before this restore was added, every
        # engine restart lost the average and the first small post-restart
        # signal locked in avg≈31, rejecting every subsequent normal-sized
        # signal as "10× avg" forever.
        self._avg_trade_size: float = float(state.fat_finger_avg_trade_size)
        self._trade_count: int = int(state.fat_finger_trade_count)
        self._last_prices: dict[str, float] = {}

    def check(self, signal: Signal, *,
             resolved_max_value: float | None = None) -> str | None:
        """Check if order looks like a fat finger.

        Args:
            resolved_max_value: Mode-resolved fat finger max value.

        Returns rejection reason if suspicious, None if OK.
        """
        if signal.action in (SignalAction.CLOSE, SignalAction.HOLD):
            return None

        entry = signal.entry_price
        if entry is None or entry <= 0:
            return None  # Can't check without price

        max_value = resolved_max_value or self._cfg.fat_finger_max_value

        # Check notional value (estimate using risk_pct * equity / risk_per_unit)
        equity = self._state.current_equity
        risk_pct = signal.risk_pct or 0.01
        if signal.stop_loss and signal.stop_loss != entry:
            risk_per_unit = abs(entry - signal.stop_loss)
            if risk_per_unit > 0:
                est_quantity = (equity * risk_pct) / risk_per_unit
                est_notional = est_quantity * entry
                if est_notional > max_value:
                    return (f"FAT_FINGER_NOTIONAL: estimated ${est_notional:,.0f} > "
                            f"max ${max_value:,.0f}")

        # Check quantity vs average
        if self._avg_trade_size > 0 and self._cfg.fat_finger_max_qty_mult > 0:
            if signal.stop_loss and signal.stop_loss != entry:
                risk_per_unit = abs(entry - signal.stop_loss)
                if risk_per_unit > 0:
                    est_quantity = (equity * risk_pct) / risk_per_unit
                    if est_quantity > self._avg_trade_size * self._cfg.fat_finger_max_qty_mult:
                        return (f"FAT_FINGER_QTY: {est_quantity:.2f} > "
                                f"{self._cfg.fat_finger_max_qty_mult}x avg "
                                f"({self._avg_trade_size:.2f})")

        # Check price deviation from last known
        last = self._last_prices.get(signal.symbol)
        if last is not None and last > 0:
            deviation = abs(entry - last) / last
            if deviation > _PRICE_DEVIATION_PCT:
                return (f"FAT_FINGER_PRICE: {signal.symbol} price {entry:.4f} deviates "
                        f"{deviation:.2%} from last {last:.4f}")

        return None

    def update_price(self, symbol: str, price: float) -> None:
        """Update last known price for a symbol."""
        if price > 0:
            self._last_prices[symbol] = price

    def update_avg_trade_size(self, quantity: float) -> None:
        """Update running average trade size (Welford's method).

        Also mirrors the updated values into RiskState so they persist across
        process restarts. The owning RiskManager.update_fill() calls
        state.persist() immediately after this method, making the durability
        guarantee: every fill's contribution to the running average is
        committed to disk before the method returns to the caller.
        """
        self._trade_count += 1
        self._avg_trade_size += (quantity - self._avg_trade_size) / self._trade_count
        # Mirror into RiskState so the next persist() picks it up.
        self._state.fat_finger_avg_trade_size = self._avg_trade_size
        self._state.fat_finger_trade_count = self._trade_count
