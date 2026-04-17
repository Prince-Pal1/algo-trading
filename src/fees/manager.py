"""FeeManager — the unified entry point for cost queries.

Strategies, backtest engine, RiskManager, and M3S all call into this
one API. They don't need to know which broker is active, which profile
to pick, or how to detect the current scenario — the manager handles
all of that.

Key API:

    # Resolve the current FeeModel for (symbol, strategy-style, [forced scenario])
    fees = FeeManager.resolve(symbol="XAUUSD", style="swing")

    # Pre-trade cost projection ($/position)
    cost = FeeManager.project_cost(
        symbol="XAUUSD",
        qty_lots=1.0,
        style="swing",
        hold_hours=48.0,
        mid_price=4865.0,
    )
    # → CostProjection(spread=X, commission=Y, swap=Z, total=X+Y+Z, ...)

    # Introspection (for dashboard / logs / debugging)
    info = FeeManager.explain(symbol="XAUUSD", style="swing")
    # → {'broker_id': ..., 'profile_name': ..., 'scenario': 'normal', ...}

The style (scalping/intraday/swing/position/arbitrage) is declared by the
strategy; the scenario (normal/news_active/illiquid/volatile) is detected
automatically from (timestamp, symbol, optional ATR context).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from src.backtest.costs import ICMarketsMetalFeeModel, ZeroCostFeeModel
from src.backtest.fee_profiles import FeeProfile
from src.fees.active import get_active_broker_for_instrument_class
from src.fees.broker import Broker, get_broker
from src.fees.scenario import ScenarioContext, ScenarioName, detect_simple


StyleName = Literal["scalping", "intraday", "swing", "position", "arbitrage"]

# Default hold-time (hours) per style — used when caller doesn't pass an explicit
# hold_hours to project_cost(). Calibrated to typical strategy behavior.
_DEFAULT_HOLD_HOURS: dict[str, float] = {
    "scalping":  0.25,    # ~15 min
    "intraday":  6.0,     # most of one session
    "swing":     48.0,    # 2 days
    "position":  480.0,   # 20 days (~3 weeks)
    "arbitrage": 2.0,     # typical pair-trade hold
}

# Whether a given style accrues swap costs. Scalping and intraday close
# before the daily rollover (22:00 UTC for most brokers); swing/position
# incur one swap per night held.
_STYLE_INCURS_SWAP: dict[str, bool] = {
    "scalping":  False,
    "intraday":  False,
    "swing":     True,
    "position":  True,
    "arbitrage": False,  # arbitrage pair-trades close same-day typically
}


@dataclass(frozen=True)
class CostProjection:
    """Decomposition of projected round-trip cost for one position.

    All figures in USD, assuming one open + one close fill.

    spread:     cost paid to cross the bid/ask (entry + exit)
    commission: per-side × 2
    swap:       per-night × nights_held (long side = usually negative/cost,
                short side = broker-dependent; we conservatively assume
                the long-side rate for unknown direction)
    total:      spread + commission + swap
    notes:      any flags (e.g. "swap not modelled for this style",
                "using fallback profile")
    """
    spread: float
    commission: float
    swap: float
    total: float
    scenario: str
    profile_name: str
    broker_id: str
    notes: tuple[str, ...] = ()

    def as_pct_of_notional(self, notional_usd: float) -> float:
        if notional_usd <= 0:
            return 0.0
        return self.total / notional_usd


class _FeeManager:
    """Stateless helpers namespaced under a class so callers can
    `from src.fees import FeeManager` and use `FeeManager.resolve(...)`.

    Not a singleton — methods are classmethods/staticmethods and the
    registry state is module-level in broker.py + scenario.py. Callers
    should NOT instantiate this class.
    """

    @staticmethod
    def _broker_and_instrument_class(
        symbol: str,
        *,
        broker_id: str | None = None,
    ) -> tuple[Broker, str]:
        """Find the broker and instrument_class for a given symbol.

        If broker_id is passed, use that broker explicitly. Otherwise,
        consult the active-broker config (honoring per-class overrides
        once we know the class — this requires a two-step resolution
        when the overrides depend on instrument_class).
        """
        # First, try the default active broker to determine the instrument class
        # (the instrument_class is typically the same across brokers for one symbol).
        if broker_id:
            broker = get_broker(broker_id)
            # When the caller explicitly picks a broker, allow the 'any'
            # wildcard so the pine research baseline can accept any symbol.
            ic = broker.instrument_class_for_symbol(symbol, allow_wildcard=True)
            if ic is None:
                raise ValueError(
                    f"Symbol {symbol!r} not supported by broker {broker_id!r}"
                )
            return broker, ic

        # Figure out the class from whichever broker first recognizes the symbol.
        # Check the default active broker first, then others.
        from src.fees.active import get_active_broker
        default_broker = get_active_broker()
        ic = default_broker.instrument_class_for_symbol(symbol)
        if ic is None:
            from src.fees.broker import list_brokers as _list
            for bid in _list():
                b = get_broker(bid)
                maybe = b.instrument_class_for_symbol(symbol)
                if maybe is not None:
                    ic = maybe
                    default_broker = b
                    break
        if ic is None:
            raise ValueError(
                f"Symbol {symbol!r} not recognized by any broker in the registry"
            )

        # Now apply per-class override (falls through to default if no override)
        broker = get_active_broker_for_instrument_class(ic)
        return broker, ic

    @staticmethod
    def resolve(
        symbol: str,
        style: str = "intraday",  # default style if strategy doesn't declare one
        *,
        scenario: ScenarioName | None = None,
        timestamp_ms: int | None = None,
        bar_atr: float | None = None,
        rolling_atr: float | None = None,
        broker_id: str | None = None,
    ) -> ICMarketsMetalFeeModel | ZeroCostFeeModel:
        """Return the FeeModel for the current context.

        - If `scenario` is passed, use it directly (explicit override).
        - Else if `timestamp_ms` is passed, auto-detect scenario from it.
        - Else default to 'normal'.

        `style` currently does not influence FeeModel selection (the model
        is a cost function, not a strategy-specific config). But the style
        is retained in the API for future extensibility (e.g. if a strategy
        wants a tighter-latency slip model for scalping vs a wider one for
        swing).
        """
        broker, ic = _FeeManager._broker_and_instrument_class(symbol, broker_id=broker_id)

        if scenario is None:
            if timestamp_ms is not None:
                scenario = detect_simple(
                    timestamp_ms=timestamp_ms,
                    instrument_class=ic,
                    bar_atr=bar_atr,
                    rolling_atr=rolling_atr,
                )
            else:
                scenario = "normal"

        profile = broker.resolve_profile(instrument_class=ic, scenario=scenario)
        if profile is None:
            # Last-resort fallback: pine_zero_cost (research baseline)
            # — makes the call succeed but flags it so caller sees the
            # degenerate profile was used.
            zero = get_broker("pine_zero_cost")
            profile = zero.resolve_profile(instrument_class="any", scenario="pine_faithful")
            if profile is None:
                raise RuntimeError(
                    f"No profile available for {symbol}/{ic}/{scenario} and no pine fallback"
                )

        return profile.make_fee_model()

    @staticmethod
    def resolve_profile(
        symbol: str,
        *,
        scenario: ScenarioName = "normal",
        timestamp_ms: int | None = None,
        bar_atr: float | None = None,
        rolling_atr: float | None = None,
        broker_id: str | None = None,
    ) -> FeeProfile:
        """Return the FeeProfile (not the model) — useful for inspection."""
        broker, ic = _FeeManager._broker_and_instrument_class(symbol, broker_id=broker_id)
        if timestamp_ms is not None:
            scenario = detect_simple(
                timestamp_ms=timestamp_ms,
                instrument_class=ic,
                bar_atr=bar_atr,
                rolling_atr=rolling_atr,
            )
        profile = broker.resolve_profile(instrument_class=ic, scenario=scenario)
        if profile is None:
            raise KeyError(
                f"No profile for {symbol}/{ic}/{scenario} on broker {broker.id}"
            )
        return profile

    @staticmethod
    def project_cost(
        symbol: str,
        qty_lots: float,
        *,
        style: str = "intraday",
        hold_hours: float | None = None,
        mid_price: float | None = None,
        scenario: ScenarioName | None = None,
        timestamp_ms: int | None = None,
        bar_atr: float | None = None,
        rolling_atr: float | None = None,
        broker_id: str | None = None,
        side: Literal["long", "short"] = "long",
    ) -> CostProjection:
        """Pre-trade cost projection — used by RiskManager/M3S for sizing.

        Computes: spread cost (round-trip) + commission (round-trip) +
        swap (per-night × nights_held for styles that incur it).

        Args:
            symbol: e.g. "XAUUSD"
            qty_lots: position size in standard lots
            style: strategy style (scalping/intraday/swing/position/arbitrage)
            hold_hours: explicit hold time. Defaults to style's typical hold.
            mid_price: reference mid — required for volume-based commission
                and for converting spread pips to dollars. If None, raises.
            scenario: forced scenario override
            timestamp_ms, bar_atr, rolling_atr: for scenario auto-detection
            broker_id: use a specific broker (defaults to active)
            side: "long" uses swapLong rate (usually a cost); "short" uses
                swapShort (sometimes a credit)
        """
        broker, ic = _FeeManager._broker_and_instrument_class(symbol, broker_id=broker_id)
        instrument = broker.resolve_instrument(instrument_class=ic)
        if instrument is None:
            raise ValueError(f"{symbol} not supported by broker {broker.id}")

        if scenario is None and timestamp_ms is not None:
            scenario = detect_simple(
                timestamp_ms=timestamp_ms,
                instrument_class=ic,
                bar_atr=bar_atr,
                rolling_atr=rolling_atr,
            )
        if scenario is None:
            scenario = "normal"

        profile = broker.resolve_profile(instrument_class=ic, scenario=scenario)
        if profile is None:
            raise KeyError(f"No profile for {symbol}/{ic}/{scenario} on {broker.id}")

        if mid_price is None:
            raise ValueError(
                "project_cost requires mid_price (needed for commission + spread-to-USD conversion)"
            )

        contract_size = instrument.contract_size
        notional_per_lot = mid_price * contract_size
        notional_total = notional_per_lot * qty_lots

        # Spread cost per side (pip × pip_size × contract × qty_lots)
        pip_size = profile.spread_config.pip_size
        spread_pips = profile.spread_config.base_spread_pips + profile.spread_config.normal_slip_pips
        # For 'volatile' scenario, news-like multiplier may apply; but the profile's
        # base_spread_pips already reflects the scenario (migration bumps it × 2 or × 3.5).
        # So we don't double-apply here.
        spread_per_lot_per_side = spread_pips * pip_size * contract_size
        spread_round_trip = spread_per_lot_per_side * qty_lots * 2  # entry + exit

        # Commission per side (dispatched by schedule type)
        comm = profile.commission_schedule
        if comm.per_100k_notional_usd is not None:
            comm_per_side_total = (notional_total / 100_000) * comm.per_100k_notional_usd
        elif comm.per_lot_per_side_usd is not None:
            comm_per_side_total = qty_lots * comm.per_lot_per_side_usd
        else:
            comm_per_side_total = 0.0
        comm_per_side_total = max(comm_per_side_total, comm.min_commission_usd)
        commission_round_trip = comm_per_side_total * 2

        # Swap (only for styles that hold overnight)
        swap_total = 0.0
        notes: list[str] = []
        if _STYLE_INCURS_SWAP.get(style, False):
            nights = max(0, int((hold_hours or _DEFAULT_HOLD_HOURS[style]) // 24))
            # We don't currently store swap rates in FeeProfile (they're in the
            # live cTrader symbol metadata, not in broker_fees.toml). For now we
            # approximate: XAUUSD long ≈ -58.14 pips/night (observed live,
            # IC Markets cTrader), short ≈ +49.42 pips/night. For other
            # instrument_classes we skip until swap data is ingested.
            if ic == "xauusd_metals":
                swap_pips_per_night = -58.14 if side == "long" else 49.42
                swap_per_lot_per_night = swap_pips_per_night * pip_size * contract_size
                swap_total = swap_per_lot_per_night * qty_lots * nights
            else:
                notes.append(f"swap not modelled for {ic}")
        else:
            notes.append(f"style={style!r} does not incur swap")

        total = spread_round_trip + commission_round_trip + swap_total

        return CostProjection(
            spread=spread_round_trip,
            commission=commission_round_trip,
            swap=swap_total,
            total=total,
            scenario=scenario,
            profile_name=profile.name,
            broker_id=broker.id,
            notes=tuple(notes),
        )

    @staticmethod
    def explain(
        symbol: str,
        *,
        style: str = "intraday",
        timestamp_ms: int | None = None,
        scenario: ScenarioName | None = None,
        broker_id: str | None = None,
    ) -> dict:
        """Return a dict summary of which broker/profile/scenario will be
        chosen for this (symbol, style, timestamp). For dashboard/logs."""
        broker, ic = _FeeManager._broker_and_instrument_class(symbol, broker_id=broker_id)
        if scenario is None and timestamp_ms is not None:
            scenario = detect_simple(
                timestamp_ms=timestamp_ms,
                instrument_class=ic,
            )
        if scenario is None:
            scenario = "normal"
        profile = broker.resolve_profile(instrument_class=ic, scenario=scenario)
        return {
            "symbol": symbol,
            "style": style,
            "broker_id": broker.id,
            "broker_name": broker.name,
            "platform": broker.platform,
            "instrument_class": ic,
            "scenario": scenario,
            "profile_name": profile.name if profile else None,
            "base_spread_pips": profile.spread_config.base_spread_pips if profile else None,
            "pip_size": profile.spread_config.pip_size if profile else None,
            "max_leverage": broker.max_leverage_for(ic),
            "incurs_swap": _STYLE_INCURS_SWAP.get(style, False),
            "default_hold_hours": _DEFAULT_HOLD_HOURS.get(style),
        }


# Module-level alias so callers import FeeManager (not _FeeManager)
FeeManager = _FeeManager
