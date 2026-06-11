"""Fat finger guard — order sanity checks.

Prevents obviously wrong orders:
- Notional value exceeds max order size
- Quantity exceeds N× average trade size (per-(strategy, symbol))
- Entry price deviates > 2% from last known price
"""

from __future__ import annotations

from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

from .config import RiskConfig
from .state import RiskState

log = get_logger("fat_finger")

_PRICE_DEVIATION_PCT = 0.02  # 2% max deviation from last known
# Skip the qty check until we've seen at least this many fills for a given
# (strategy, symbol) pair. Stops a single tiny first fill from locking the
# average at a low value and rejecting every subsequent normal-sized signal.
_QTY_CHECK_WARMUP = 5


def _pair_key(strategy: str, symbol: str) -> str:
    return f"{strategy}/{symbol}"


class FatFingerGuard:
    """Sanity checks on order values and prices."""

    def __init__(self, config: RiskConfig, state: RiskState):
        self._cfg = config
        self._state = state
        self._last_prices: dict[str, float] = {}

    def _get_pair(self, strategy: str, symbol: str) -> tuple[float, int]:
        return self._state.fat_finger_avg_pairs.get(_pair_key(strategy, symbol), (0.0, 0))

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

        # Check quantity vs per-(strategy, symbol) average
        avg, count = self._get_pair(signal.strategy_name, signal.symbol)
        if (
            count >= _QTY_CHECK_WARMUP
            and avg > 0
            and self._cfg.fat_finger_max_qty_mult > 0
            and signal.stop_loss
            and signal.stop_loss != entry
        ):
            risk_per_unit = abs(entry - signal.stop_loss)
            if risk_per_unit > 0:
                est_quantity = (equity * risk_pct) / risk_per_unit
                if est_quantity > avg * self._cfg.fat_finger_max_qty_mult:
                    return (f"FAT_FINGER_QTY: {est_quantity:.2f} > "
                            f"{self._cfg.fat_finger_max_qty_mult}x avg "
                            f"({avg:.2f}) for {signal.strategy_name}/{signal.symbol}")

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

    def update_avg_trade_size(self, strategy: str, symbol: str, quantity: float) -> None:
        """Update running average trade size for a (strategy, symbol) pair.

        Welford's method, applied independently per pair. The owning
        RiskManager.update_fill() calls state.persist() immediately after,
        so every fill's contribution is committed to disk before the
        method returns.
        """
        key = _pair_key(strategy, symbol)
        avg, count = self._state.fat_finger_avg_pairs.get(key, (0.0, 0))
        count += 1
        avg += (quantity - avg) / count
        self._state.fat_finger_avg_pairs[key] = (avg, count)
