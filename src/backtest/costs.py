"""Backtest cost models — Phase G.2a.2 of the gold trading plan.

Replaces the naive `commission_pct=0.04` flat model in the existing
BacktestEngine with a broker-accurate cost pipeline for leveraged gold
scalping. The flat 0.04% model is wrong for two independent reasons at
high leverage on M5 XAUUSD:

1. Commission is per-lot-per-side at real ECN brokers, not a % of notional.
   IC Markets cTrader raw: $3/side per standard lot ($6 round-trip). At
   $50,000 notional (5% × 1000× on $1000 account), that's $6 round-trip
   which is 12% of the $50 margin — nothing like 0.04%.

2. Spread + slippage at M5 scalping speed is volatility-dependent and
   widens massively during news events (NFP, FOMC, CPI). The flat
   model doesn't capture this — backtests then overstate alpha by
   exactly the slippage gap.

This module ships three cost components:
- `SpreadSlippageConfig` — spread + slippage parameters (base + ATR + news)
- `CommissionSchedule` — per-lot-per-side + min commission + contract size
- `NewsWindow` — explicit start/end UTC ms for news-event widening
- `ICMarketsMetalFeeModel` — convenience wrapper for XAUUSD on IC Markets

Usage:
    from src.backtest.costs import ICMarketsMetalFeeModel, NewsWindow

    model = ICMarketsMetalFeeModel(
        news_windows=[NewsWindow(start_ms=1738681200000, end_ms=1738684800000,
                                  label="NFP 2025-02-07")],
    )
    fill = model.fill_price(
        side="BUY",
        reference_price=2400.50,
        atr=3.0,
        ts_ms=1738681300000,
    )
    commission = model.commission_usd(quantity_units=20.833)  # 1 lot
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


# ── News windows ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewsWindow:
    """A specific news event's spread/slippage widening window.

    The window is a closed interval [start_ms, end_ms] in unix ms. During
    the window, spread is multiplied by news_spread_mult and slippage by
    news_slip_mult. Outside the window, normal spread/slippage applies.

    Typical windows for XAUUSD:
    - NFP: 12:25-12:40 UTC Friday (first Friday of month)
    - FOMC: 17:55-18:15 UTC Wednesday (8×/year) + 18:25-19:00 Powell press
    - CPI: 12:25-12:40 UTC (monthly)
    - ECB: 11:40-12:00 UTC (Thursday, 8×/year) + 12:25-13:00 Lagarde press
    """
    start_ms: int
    end_ms: int
    label: str = ""

    def contains(self, ts_ms: int) -> bool:
        return self.start_ms <= ts_ms <= self.end_ms


# ── Spread + slippage ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpreadSlippageConfig:
    """Per-side spread + slippage model in pips.

    The broker quotes a two-sided market with bid and ask. A BUY order
    fills at ask = mid + spread/2 + slippage. A SELL order fills at
    bid = mid - spread/2 - slippage. `fill_price()` computes this given
    a reference price and ATR context.

    Defaults are calibrated to IC Markets cTrader raw XAUUSD:
    - base_spread_pips: 0.13 pips = $1.30 per standard lot spread cost
      (matches Brokerchooser-verified IC Markets typical)
    - normal_slip_pips: 0.2 pips queue slippage — what the order book
      moves between signal emission and fill
    - atr_vol_mult: slippage widens with volatility. During low-ATR
      quiet periods slip ~= normal; during high-ATR fast moves slip can
      be 2-3× that
    - news_spread_mult: 10× widening during news windows (empirically
      IC Markets raw goes from 0.13 → 1.0-2.0 pips during NFP)
    - news_slip_mult: 8× slip widening during news

    Numbers are PER SIDE (buy leg or sell leg), not per round trip.
    A round-trip trade pays spread twice (open + close).

    Pip convention: for XAUUSD, 1 pip = $0.10 move. At contract_size=100
    (1 lot = 100 oz), 1 pip = $10 per lot = $10/100oz = $0.10/oz. All
    the per-pip numbers in this config are in that convention.
    """
    base_spread_pips: float = 0.13
    normal_slip_pips: float = 0.2
    atr_vol_mult: float = 0.5
    news_windows: tuple[NewsWindow, ...] = field(default_factory=tuple)
    news_spread_mult: float = 10.0
    news_slip_mult: float = 8.0
    pip_size: float = 0.10  # price units per pip for XAUUSD (2-decimal quote)


def fill_price(
    *,
    side: str,
    reference_price: float,
    atr: float,
    ts_ms: int,
    config: SpreadSlippageConfig,
) -> float:
    """Compute the realistic fill price for an order at the given reference.

    Formula:
        half_spread = base_spread_pips / 2
        slip        = normal_slip_pips + atr_vol_mult × (atr / pip_size)
        in a news window:
            half_spread *= news_spread_mult
            slip        *= news_slip_mult
        total_adverse_pips = half_spread + slip
        BUY  fills at  reference + total_adverse_pips × pip_size
        SELL fills at  reference − total_adverse_pips × pip_size

    Args:
        side: "BUY" or "SELL". BUY pays the ask (fills higher than ref).
            SELL pays the bid (fills lower than ref).
        reference_price: the mid-market reference (e.g., bar_open).
        atr: recent ATR in price units. Used for volatility-scaled slippage.
        ts_ms: current timestamp for news-window lookup.
        config: the SpreadSlippageConfig.

    Returns:
        The fill price with spread + slippage applied, always adverse
        to the order direction.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    if reference_price <= 0:
        raise ValueError(f"reference_price must be > 0, got {reference_price}")

    in_news = any(w.contains(ts_ms) for w in config.news_windows)

    half_spread_pips = config.base_spread_pips / 2.0
    slip_pips = config.normal_slip_pips
    if atr > 0 and config.pip_size > 0:
        slip_pips += config.atr_vol_mult * (atr / config.pip_size)

    if in_news:
        half_spread_pips *= config.news_spread_mult
        slip_pips *= config.news_slip_mult

    adverse_price = (half_spread_pips + slip_pips) * config.pip_size

    if side == "BUY":
        return reference_price + adverse_price
    return reference_price - adverse_price


def round_trip_spread_cost_usd(
    *,
    quantity_units: float,
    config: SpreadSlippageConfig,
    contract_size: float,
    in_news: bool = False,
) -> float:
    """Compute the round-trip spread + slippage cost in USD for a position.

    This is the PER-ROUND-TRIP cost: open leg pays spread + slip, close
    leg also pays. Used for post-hoc attribution (split equity into
    alpha vs cost) and for pre-trade expected-cost estimation.

    Args:
        quantity_units: position size in instrument units (oz for XAUUSD).
        config: SpreadSlippageConfig.
        contract_size: instrument units per 1 standard lot (100 for XAUUSD).
        in_news: whether to apply news-event multipliers.

    Returns:
        USD cost for the full round-trip (both legs).
    """
    half_spread_pips = config.base_spread_pips / 2.0
    slip_pips = config.normal_slip_pips  # atr contribution omitted in static estimate
    if in_news:
        half_spread_pips *= config.news_spread_mult
        slip_pips *= config.news_slip_mult

    # Two legs (open + close), each pays (half_spread + slip) in adverse
    # price movement. Cost per leg in USD = adverse_pips × pip_size × quantity.
    adverse_per_leg_usd = (half_spread_pips + slip_pips) * config.pip_size * quantity_units
    return 2.0 * adverse_per_leg_usd


# ── Commission ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommissionSchedule:
    """Per-lot-per-side commission schedule.

    IC Markets cTrader raw XAUUSD:
        per_lot_per_side_usd = $3.00
        contract_size        = 100 (oz per lot)
        min_commission_usd   = 0.0

    Per round-trip (open + close), a 1-lot XAUUSD trade pays $6 commission.
    Contrast with the old flat 0.04% model which, on $50,000 notional,
    would be $20/side or $40/round-trip. The flat model was 6-7× too
    expensive at high leverage, masking real alpha.

    For sub-lot trades (0.01, 0.05 lots), commission scales linearly with
    the quantity_units / contract_size ratio. There's usually no minimum
    per-trade commission on ECN accounts, but the field exists for brokers
    that do impose one.
    """
    per_lot_per_side_usd: float = 3.0
    contract_size: float = 100.0  # default XAUUSD 1 lot = 100 oz
    min_commission_usd: float = 0.0


def commission_usd(
    *,
    quantity_units: float,
    schedule: CommissionSchedule,
) -> float:
    """Round-trip commission for a position of `quantity_units`.

    Formula:
        lots = quantity_units / contract_size
        per_leg = max(min_commission_usd, lots × per_lot_per_side_usd)
        round_trip = 2 × per_leg

    Args:
        quantity_units: position size in instrument units.
        schedule: CommissionSchedule.

    Returns:
        USD commission for the full round-trip.
    """
    if quantity_units <= 0:
        return 0.0
    lots = quantity_units / schedule.contract_size
    per_leg = max(schedule.min_commission_usd, lots * schedule.per_lot_per_side_usd)
    return 2.0 * per_leg


# ── Broker-specific fee model ────────────────────────────────────────────


@dataclass
class ICMarketsMetalFeeModel:
    """Convenience wrapper bundling spread + commission for IC Markets XAUUSD.

    Construct with optional news calendar override. Use the wrapper methods
    directly in the BacktestEngine to avoid threading multiple config
    objects through every call site.

    Usage:
        model = ICMarketsMetalFeeModel()  # defaults calibrated for XAUUSD
        fill = model.fill_price(side="BUY", reference_price=2400.50,
                                 atr=3.0, ts_ms=1738681300000)
        cost = model.commission_usd(quantity_units=20.833)  # 1 lot
        rt_spread = model.round_trip_spread_cost_usd(
            quantity_units=20.833, in_news=False,
        )
    """
    spread_config: SpreadSlippageConfig = field(default_factory=SpreadSlippageConfig)
    commission_schedule: CommissionSchedule = field(default_factory=CommissionSchedule)

    def fill_price(
        self,
        *,
        side: str,
        reference_price: float,
        atr: float,
        ts_ms: int,
    ) -> float:
        return fill_price(
            side=side,
            reference_price=reference_price,
            atr=atr,
            ts_ms=ts_ms,
            config=self.spread_config,
        )

    def commission_usd(self, *, quantity_units: float) -> float:
        return commission_usd(
            quantity_units=quantity_units,
            schedule=self.commission_schedule,
        )

    def round_trip_spread_cost_usd(
        self,
        *,
        quantity_units: float,
        in_news: bool = False,
    ) -> float:
        return round_trip_spread_cost_usd(
            quantity_units=quantity_units,
            config=self.spread_config,
            contract_size=self.commission_schedule.contract_size,
            in_news=in_news,
        )

    def is_in_news_window(self, ts_ms: int) -> bool:
        """True if the given timestamp falls inside any configured news window."""
        return any(w.contains(ts_ms) for w in self.spread_config.news_windows)


@dataclass
class ZeroCostFeeModel:
    """Drop-in fee model that charges zero commission, zero spread, zero slippage.

    Used for Pine-faithful backtests — TradingView's default strategy tester
    uses `commission_value=0, slippage=0` unless the script sets otherwise.
    Matching this lets us reproduce the optimistic TV numbers so we can
    compare them apples-to-apples against realistic costs.

    Interface identical to `ICMarketsMetalFeeModel`:
      - `fill_price(side, reference_price, atr, ts_ms)` → returns reference_price
      - `commission_usd(quantity_units)` → returns 0.0

    Do NOT use for live backtests intended to predict real P&L — this is
    strictly for reproducing Pine Script parity.
    """

    def fill_price(
        self,
        *,
        side: str,
        reference_price: float,
        atr: float,
        ts_ms: int,
    ) -> float:
        # No spread, no slippage — fill at the exact reference price
        return reference_price

    def commission_usd(self, *, quantity_units: float) -> float:
        return 0.0

    def round_trip_spread_cost_usd(
        self,
        *,
        quantity_units: float,
        in_news: bool = False,
    ) -> float:
        return 0.0

    def is_in_news_window(self, ts_ms: int) -> bool:
        return False


# ── News calendar loader ─────────────────────────────────────────────────


def load_news_calendar_csv(path: str) -> list[NewsWindow]:
    """Load news windows from a CSV file.

    Expected CSV format:
        label,start_iso,end_iso
        NFP 2025-02-07,2025-02-07T12:25:00Z,2025-02-07T12:40:00Z
        FOMC 2025-03-19,2025-03-19T17:55:00Z,2025-03-19T18:15:00Z
        ...

    Returns an empty list if the file doesn't exist — lets the backtest
    run without a news calendar and just not apply widening.
    """
    from datetime import datetime, timezone
    from pathlib import Path
    import csv

    p = Path(path)
    if not p.exists():
        return []

    windows: list[NewsWindow] = []
    with p.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row.get("label", "").strip()
            start_iso = row.get("start_iso", "").strip()
            end_iso = row.get("end_iso", "").strip()
            if not (label and start_iso and end_iso):
                continue
            try:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            # Ensure UTC
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            windows.append(NewsWindow(
                start_ms=int(start_dt.timestamp() * 1000),
                end_ms=int(end_dt.timestamp() * 1000),
                label=label,
            ))
    return windows
