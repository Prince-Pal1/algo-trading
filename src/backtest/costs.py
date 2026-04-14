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

    Defaults are calibrated to IC Markets Raw cTrader XAUUSD using
    PUBLISHED official broker numbers + peer ECN measurements (2026-04-14
    research pass, task #105):

    base_spread_pips: 0.30 pips
        Conservative blend between IC Markets Global Raw (0.09 avg) and
        IC Markets EU Raw (0.63 avg per cdn.icmarkets.eu spec sheet).
        Aligned with peer ECN brokers Pepperstone (0.20-0.30) and
        Exness (0.0-0.20). Sources:
          - https://www.icmarkets.com/global/en/trading-pricing/spreads
          - https://cdn.icmarkets.eu/uploads/Commodity-Specification-Sheet.pdf
          - https://www.bestbrokers.com/reviews/ic-markets/spreads-fees-and-commissions/

    normal_slip_pips: 0.30 pips
        Conservative — gold is slightly less liquid than EURUSD where
        databasemart latency study measured cTrader London slippage at
        0.00001 (1 micro-pip). Bumped 30% above the spread baseline as
        gold has wider tick increments. Sources:
          - https://www.databasemart.com/blog/customer-stories-115
          - https://www.myfxbook.com/press-release/-99-slippage-free-advantage/36625
            (Exness XAUUSD 99% slippage-free, peer ECN comparable)

    atr_vol_mult: 0.0 (DISABLED)
        ⚠️ HISTORICAL BUG: previous default was 0.5, which scaled slip
        as half of bar ATR. With XAUUSD M5 median ATR ~$7, this produced
        35.6 pips of slip per fill — ~90× too high for ECN brokers.
        ECN slippage is bounded by order book depth (typically <1 pip),
        not by bar volatility. The ATR scaling was a port from a
        market-maker model and never made sense for raw cTrader fills.
        Removed entirely. See ARCHITECTURE.md gotcha 2026-04-14.

    news_spread_mult: 5.0
        Halved from previous 10× because real news widening on gold is
        3-10× the normal spread, not always 10×. Source: multiple FXNX,
        Vantage Markets gold news trading guides documenting
        widening behavior on NFP/CPI/FOMC.

    news_slip_mult: 5.0
        Same reasoning as news_spread_mult. During NFP/CPI/FOMC the
        order book thins out and slippage can spike to 1-5 pips for a
        few minutes. 5× normal_slip_pips (0.30) = 1.5 pips peak slip
        during news, matches anecdotal trader reports.

    Numbers are PER SIDE (buy leg or sell leg), not per round trip.
    A round-trip trade pays spread twice (open + close).

    Pip convention for XAUUSD on IC Markets:
        - 2-decimal quote (e.g. 4500.05)
        - 1 pip = $0.10 price units
        - 1 lot = 100 oz → 1 pip move = $10 P&L per lot
        - Verified against IC Markets official spreads page +
          getknowtrading.com pip calculator + multiple peer broker docs.
    """
    base_spread_pips: float = 0.30
    normal_slip_pips: float = 0.30
    atr_vol_mult: float = 0.0
    news_windows: tuple[NewsWindow, ...] = field(default_factory=tuple)
    news_spread_mult: float = 5.0
    news_slip_mult: float = 5.0
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
    """Commission schedule supporting BOTH per-lot fixed and volume-based pricing.

    IC Markets uses TWO different commission structures depending on platform
    (verified 2026-04-14 task #105 against the official spreads page):

    1. **MT4/MT5 Raw Spread** — FIXED per-lot commission
       Use: per_lot_per_side_usd=3.50, per_100k_notional_usd=None
       For XAUUSD: $3.50/side regardless of gold price → $7 round-trip per lot.
       Also same rate for FX pairs.

    2. **cTrader / TradingView Raw Spread** — VOLUME-BASED commission
       Use: per_lot_per_side_usd=None, per_100k_notional_usd=3.0
       Commission = ($3 × notional_usd / $100,000) per side.
       For XAUUSD at $4500/oz × 100 oz = $450,000 notional:
         → $13.50/side per lot → $27 round-trip per lot.
       For FX where 1 lot = $100k notional, this works out to $3/side
       (matches MT4 nearly exactly).

    **For gold on cTrader, commission is ~3.86× higher than MT4.** This
    is a real economic difference, not a calibration choice. Per CLAUDE.md
    we deploy on cTrader, so the ICMarketsMetalFeeModel default uses the
    cTrader schedule.

    Sources:
      - https://www.icmarkets.com/global/en/trading-pricing/spreads
      - https://www.icmarkets.eu/en/trading-pricing/trading-costs
      - https://www.bestbrokers.com/reviews/ic-markets/spreads-fees-and-commissions/

    For volume-based schedules, commission_usd() requires a reference_price
    argument so the function can compute notional from quantity × price.
    For per-lot schedules, reference_price is ignored.

    For sub-lot trades (0.01, 0.05 lots), commission scales linearly.
    No minimum commission on ECN accounts (min_commission_usd=0).
    """
    per_lot_per_side_usd: float | None = None  # MT4 fixed (e.g. $3.50)
    per_100k_notional_usd: float | None = 3.0  # cTrader volume-based (default)
    contract_size: float = 100.0  # XAUUSD 1 lot = 100 oz
    min_commission_usd: float = 0.0


# Factory functions for the two IC Markets schedules
def make_ic_markets_mt4_xauusd_schedule() -> CommissionSchedule:
    """IC Markets MT4 Raw Spread schedule for XAUUSD: $3.50/lot/side fixed."""
    return CommissionSchedule(
        per_lot_per_side_usd=3.50,
        per_100k_notional_usd=None,
        contract_size=100.0,
    )


def make_ic_markets_ctrader_xauusd_schedule() -> CommissionSchedule:
    """IC Markets cTrader Raw Spread schedule: $3 per $100k notional, volume-based.

    For gold at typical ~$4000-$5000/oz price, this works out to
    ~$12-15 per side per lot — significantly higher than MT4's $3.50.
    """
    return CommissionSchedule(
        per_lot_per_side_usd=None,
        per_100k_notional_usd=3.0,
        contract_size=100.0,
    )


def commission_usd(
    *,
    quantity_units: float,
    schedule: CommissionSchedule,
    reference_price: float | None = None,
) -> float:
    """Round-trip commission for a position of `quantity_units`.

    Dispatches on schedule type:
      - per_lot_per_side_usd set → MT4-style fixed per-lot pricing
        (reference_price ignored)
      - per_100k_notional_usd set → cTrader-style volume-based pricing
        (reference_price REQUIRED)

    Args:
        quantity_units: position size in instrument units (oz for XAUUSD).
        schedule: CommissionSchedule (must set exactly one pricing field).
        reference_price: current price used to compute notional value.
            Required for volume-based schedules; ignored for per-lot.

    Returns:
        USD commission for the full round-trip (entry + exit).
    """
    if quantity_units <= 0:
        return 0.0

    if schedule.per_100k_notional_usd is not None:
        # Volume-based (cTrader)
        if reference_price is None or reference_price <= 0:
            raise ValueError(
                "reference_price required for volume-based commission schedules. "
                "Pass current fill_price to commission_usd()."
            )
        notional_usd = quantity_units * reference_price
        per_leg_usd = (notional_usd / 100_000.0) * schedule.per_100k_notional_usd
        per_leg_usd = max(schedule.min_commission_usd, per_leg_usd)
    elif schedule.per_lot_per_side_usd is not None:
        # Fixed per-lot (MT4)
        lots = quantity_units / schedule.contract_size
        per_leg_usd = max(schedule.min_commission_usd, lots * schedule.per_lot_per_side_usd)
    else:
        raise ValueError(
            "CommissionSchedule must set either per_lot_per_side_usd "
            "(MT4 style) or per_100k_notional_usd (cTrader style)."
        )

    return 2.0 * per_leg_usd


# ── Broker-specific fee model ────────────────────────────────────────────


@dataclass
class ICMarketsMetalFeeModel:
    """Convenience wrapper bundling spread + commission for IC Markets XAUUSD.

    Defaults to the **cTrader Raw Spread** schedule because per CLAUDE.md
    we deploy on IC Markets cTrader. For MT4 backtests, construct with
    `commission_schedule=make_ic_markets_mt4_xauusd_schedule()`.

    Both schedules use the same SpreadSlippageConfig defaults (the
    research-calibrated 0.30 spread + 0.30 slip + 0.0 ATR mult).

    Usage:
        model = ICMarketsMetalFeeModel()  # cTrader by default
        fill = model.fill_price(side="BUY", reference_price=2400.50,
                                 atr=3.0, ts_ms=1738681300000)
        cost = model.commission_usd(quantity_units=20.833,
                                     reference_price=2400.50)  # required for cTrader
        rt_spread = model.round_trip_spread_cost_usd(
            quantity_units=20.833, in_news=False,
        )

        # MT4 alternative (cheaper for gold):
        from src.backtest.costs import make_ic_markets_mt4_xauusd_schedule
        model_mt4 = ICMarketsMetalFeeModel(
            commission_schedule=make_ic_markets_mt4_xauusd_schedule()
        )
        cost_mt4 = model_mt4.commission_usd(quantity_units=20.833)  # no price needed
    """
    spread_config: SpreadSlippageConfig = field(default_factory=SpreadSlippageConfig)
    commission_schedule: CommissionSchedule = field(
        default_factory=make_ic_markets_ctrader_xauusd_schedule
    )

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

    def commission_usd(
        self,
        *,
        quantity_units: float,
        reference_price: float | None = None,
    ) -> float:
        return commission_usd(
            quantity_units=quantity_units,
            schedule=self.commission_schedule,
            reference_price=reference_price,
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

    def commission_usd(
        self,
        *,
        quantity_units: float,
        reference_price: float | None = None,
    ) -> float:
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
