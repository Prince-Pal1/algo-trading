"""Instrument metadata registry — Phase G.0b of the gold trading plan.

Describes the physical properties of each tradeable symbol: tick size,
contract size, pip value, lot sizing, market hours, asset class. This
metadata is what separates "200 units of XAU" from "0.33 BTC" — both
are raw quantities from the risk math, but they mean completely
different things in dollar terms depending on the broker's unit system.

Backward compat: the existing risk/executor/strategy code treats
quantity as "notional units in the same scale as entry_price"
(e.g., 0.33 BTC or 200 XAU). This module ADDS metadata on top of that
without changing the math — callers can query the registry for tick
size, pip value, market hours, etc. The actual broker-unit translation
(e.g., "lots" vs "units") happens at the broker executor layer in G.4.

Usage:
    from src.utils.instruments import get_instrument
    inst = get_instrument("XAUUSD")
    if inst.is_24_5 and is_weekend(ts):
        log.info("market closed — skipping signal")
"""

from __future__ import annotations

import msgspec


class Instrument(msgspec.Struct, frozen=True):
    """Physical properties of a tradeable instrument.

    - `tick_size`: smallest price increment (e.g., 0.01 for XAUUSD 2-digit,
      0.001 for 3-digit, 0.01 for BTCUSDT spot)
    - `contract_size`: units of underlying per "1 standard lot". 1.0 for
      fractional crypto (quantity IS the underlying amount). 100.0 for
      XAUUSD (1 standard lot = 100 troy oz).
    - `pip_value_per_std_lot_usd`: USD value of 1 pip movement on a standard
      lot, assuming USD-denominated account. Computed or looked up; used by
      leverage/margin math in G.0c.
    - `min_lot` / `max_lot`: broker-enforced lot size bounds. `min_lot=0.001`
      for BTCUSDT (Binance), `0.01` for XAUUSD on most forex brokers.
    - `asset_class`: "crypto" | "metal" | "forex" | "index" | "equity"
    - `quote_currency`: "USD" | "USDT" | "EUR" etc. — for risk calculations
      when the account currency differs from the quote currency.
    - `is_24_7`: True for crypto (Binance, etc.). False for metals/forex/
      indices that close on weekends.
    - `is_24_5`: True for forex/metals that trade 24h Mon-Fri but close
      Fri evening until Sun evening. False for crypto.
    - `weekend_closed_hours_utc`: (open_utc_hour, close_utc_hour) — typically
      forex closes Friday ~22:00 UTC and reopens Sunday ~22:00 UTC. For
      watchdog + weekend-aware heartbeat (Blocker 4 in the gold plan).
    - `default_leverage_cap`: conservative upper bound on effective leverage
      for THIS instrument, used by G.0c risk gates. Override-able via config.
    """
    symbol: str
    asset_class: str
    quote_currency: str
    tick_size: float
    contract_size: float
    pip_value_per_std_lot_usd: float
    min_lot: float
    max_lot: float
    is_24_7: bool
    is_24_5: bool
    default_leverage_cap: float
    # Friday close → Sunday open, in UTC. Only meaningful if is_24_5.
    # Default (22, 22) = Fri 22:00 UTC close, Sun 22:00 UTC reopen.
    weekend_close_utc_hour: int = 22
    weekend_open_utc_hour: int = 22


# ═══════════════════════════════════════════════════════════════════════
# Registry — add entries here as new markets come online
# ═══════════════════════════════════════════════════════════════════════

# Crypto defaults (preserving existing behavior)
# contract_size=1.0 means "quantity IS the underlying amount" — the
# existing risk math works unchanged.
_CRYPTO_DEFAULT = dict(
    asset_class="crypto",
    quote_currency="USDT",
    tick_size=0.01,
    contract_size=1.0,
    pip_value_per_std_lot_usd=0.01,  # 1 tick = $0.01 per unit
    min_lot=0.001,
    max_lot=1_000.0,
    is_24_7=True,
    is_24_5=False,
    default_leverage_cap=1.0,  # spot crypto, no leverage in current system
)

# Metals / forex
# contract_size=100 means 1 standard lot = 100 troy oz. Tick 0.01 and
# pip=0.10 is the 2-decimal broker convention (most common).
_XAUUSD = dict(
    asset_class="metal",
    quote_currency="USD",
    tick_size=0.01,
    contract_size=100.0,  # 1 standard lot = 100 troy ounces
    pip_value_per_std_lot_usd=10.0,  # 1 pip = $0.10 price move × 100 oz = $10
    min_lot=0.01,
    max_lot=100.0,
    is_24_7=False,
    is_24_5=True,
    default_leverage_cap=500.0,  # IC Markets offers 1:1000 but practical cap is 500
)

_REGISTRY: dict[str, Instrument] = {
    # Crypto spot
    "BTCUSDT":  Instrument(symbol="BTCUSDT",  **_CRYPTO_DEFAULT),
    "ETHUSDT":  Instrument(symbol="ETHUSDT",  **_CRYPTO_DEFAULT),
    "BNBUSDT":  Instrument(symbol="BNBUSDT",  **_CRYPTO_DEFAULT),
    "SOLUSDT":  Instrument(symbol="SOLUSDT",  **_CRYPTO_DEFAULT),
    "XRPUSDT":  Instrument(symbol="XRPUSDT",  **_CRYPTO_DEFAULT),
    "ADAUSDT":  Instrument(symbol="ADAUSDT",  **_CRYPTO_DEFAULT),
    "DOGEUSDT": Instrument(symbol="DOGEUSDT", **_CRYPTO_DEFAULT),
    "AVAXUSDT": Instrument(symbol="AVAXUSDT", **_CRYPTO_DEFAULT),
    "DOTUSDT":  Instrument(symbol="DOTUSDT",  **_CRYPTO_DEFAULT),
    "NEARUSDT": Instrument(symbol="NEARUSDT", **_CRYPTO_DEFAULT),
    "APTUSDT":  Instrument(symbol="APTUSDT",  **_CRYPTO_DEFAULT),
    "MATICUSDT": Instrument(symbol="MATICUSDT", **_CRYPTO_DEFAULT),
    # Synthetic carry symbols — same shape as crypto but no real trading
    "BTCUSDT-CARRY": Instrument(symbol="BTCUSDT-CARRY", **_CRYPTO_DEFAULT),

    # Metals
    "XAUUSD": Instrument(symbol="XAUUSD", **_XAUUSD),
}


def get_instrument(symbol: str) -> Instrument:
    """Return the Instrument metadata for `symbol`.

    Falls back to a conservative crypto-default Instrument if the symbol
    is not in the registry. Log a warning at call time if this matters.
    """
    if symbol in _REGISTRY:
        return _REGISTRY[symbol]
    # Safe fallback — treats unknown symbols as fractional-unit crypto
    return Instrument(symbol=symbol, **_CRYPTO_DEFAULT)


def register_instrument(inst: Instrument) -> None:
    """Add or override an entry in the registry (for tests + dynamic setup)."""
    _REGISTRY[inst.symbol] = inst


def list_instruments() -> list[Instrument]:
    """Return all registered instruments, sorted by symbol."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def is_market_open(symbol: str, ts_ms: int) -> bool:
    """Return True if the market for `symbol` is open at `ts_ms`.

    Crypto (is_24_7) is always open. Forex/metals (is_24_5) is open from
    Sunday 22:00 UTC through Friday 22:00 UTC; closed otherwise.

    Used by the watchdog/heartbeat to avoid killing the engine during
    legitimate market-closed windows (Blocker 4 in the gold plan).
    """
    inst = get_instrument(symbol)
    if inst.is_24_7:
        return True
    if not inst.is_24_5:
        # Unknown market hours — assume open as a safe default
        return True

    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    weekday = dt.weekday()  # 0=Mon, 6=Sun
    hour = dt.hour

    # Saturday is fully closed
    if weekday == 5:
        return False
    # Friday after weekend_close_utc_hour is closed
    if weekday == 4 and hour >= inst.weekend_close_utc_hour:
        return False
    # Sunday before weekend_open_utc_hour is closed
    if weekday == 6 and hour < inst.weekend_open_utc_hour:
        return False
    return True
