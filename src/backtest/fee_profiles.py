"""Broker fee profile registry — research-calibrated cost models.

Loads named profiles from `config/broker_fees.toml` and exposes them as
`FeeProfile` dataclasses. Each profile bundles a `SpreadSlippageConfig`,
a `CommissionSchedule`, the broker/platform/scenario tags, and the URL
sources used to calibrate the parameters.

Use this BEFORE every backtest. The strategy dev process (Stage 0 Pine
port validation + Phase 2 realistic backtest) requires explicit profile
selection so cost assumptions are visible and traceable.

Public API:

    list_profiles()                 → list[str] of all profile names
    list_profiles(broker="...")     → filter by broker
    list_profiles(scenario="...")   → filter by scenario tag
    get_profile(name)               → FeeProfile (raises if not found)
    make_fee_model(name)            → ICMarketsMetalFeeModel ready for engine
    select_profile(broker, platform, instrument, scenario)  → name lookup

Example:

    from src.backtest.fee_profiles import make_fee_model
    from src.backtest.leveraged_engine import LeveragedBacktestEngine

    engine = LeveragedBacktestEngine(
        fee_model=make_fee_model("ic_markets_ctrader_xauusd_normal"),
        ...
    )

Or for stress testing:

    engine_stress = LeveragedBacktestEngine(
        fee_model=make_fee_model("ic_markets_ctrader_xauusd_stress"),
        ...
    )

LEVERAGE INTERACTION (important):
Broker costs are LEVERAGE-INDEPENDENT in dollar terms. A 1-lot trade
costs the same $94 (cTrader gold) at 1x or 1000x leverage. But as a
percentage of MARGIN, costs amplify dramatically: 0.021% at 1x →
20.9% at 1000x. See docs/BROKER_FEES.md for the full explanation +
worked examples.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Python 3.11+ has tomllib stdlib
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from src.backtest.costs import (
    CommissionSchedule,
    ICMarketsMetalFeeModel,
    NewsWindow,
    SpreadSlippageConfig,
    ZeroCostFeeModel,
    load_news_calendar_csv,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "broker_fees.toml"
DEFAULT_NEWS_CALENDAR_PATH = REPO_ROOT / "config" / "news_calendar.csv"


@dataclass(frozen=True)
class FeeProfile:
    """A named broker × platform × instrument × scenario fee configuration.

    Bundles the SpreadSlippageConfig + CommissionSchedule + traceability
    metadata (sources cited, scenario tag, calibration notes). The
    `make_fee_model()` method returns a ready-to-use `ICMarketsMetalFeeModel`
    or `ZeroCostFeeModel` for direct insertion into a backtest engine.

    Fields:
        name: unique identifier (e.g. "ic_markets_ctrader_xauusd_normal")
        broker: "IC Markets", "(none)", etc.
        platform: "ctrader", "mt4", "(none)", etc.
        instrument_class: "xauusd_metals", "fx_majors", etc.
        scenario: "normal", "news_active", "stress", "pine_faithful"
        description: 1-line human summary
        sources: tuple of URLs used to calibrate this profile
        notes: free-form rationale (what's the calibration logic, what's
               special about this profile)
        spread_config: SpreadSlippageConfig instance
        commission_schedule: CommissionSchedule instance
    """
    name: str
    broker: str
    platform: str
    instrument_class: str
    scenario: str
    description: str
    sources: tuple[str, ...]
    notes: str
    spread_config: SpreadSlippageConfig
    commission_schedule: CommissionSchedule

    def make_fee_model(self) -> ICMarketsMetalFeeModel | ZeroCostFeeModel:
        """Return a ready-to-use fee model for the LeveragedBacktestEngine."""
        if self.scenario == "pine_faithful":
            return ZeroCostFeeModel()
        return ICMarketsMetalFeeModel(
            spread_config=self.spread_config,
            commission_schedule=self.commission_schedule,
        )


# ── Loader ──────────────────────────────────────────────────────────────


def _build_spread_config(
    raw: dict[str, Any],
    *,
    news_calendar_path: Path = DEFAULT_NEWS_CALENDAR_PATH,
) -> SpreadSlippageConfig:
    """Construct SpreadSlippageConfig from a TOML profile's [spread] table."""
    news_windows: tuple[NewsWindow, ...] = ()
    if raw.get("load_news_calendar", False):
        loaded = load_news_calendar_csv(str(news_calendar_path))
        news_windows = tuple(loaded)

    return SpreadSlippageConfig(
        base_spread_pips=float(raw["base_spread_pips"]),
        normal_slip_pips=float(raw["normal_slip_pips"]),
        atr_vol_mult=float(raw["atr_vol_mult"]),
        news_windows=news_windows,
        news_spread_mult=float(raw["news_spread_mult"]),
        news_slip_mult=float(raw["news_slip_mult"]),
        pip_size=float(raw["pip_size"]),
    )


def _build_commission_schedule(raw: dict[str, Any]) -> CommissionSchedule:
    """Construct CommissionSchedule from a TOML profile's [commission] table.

    Dispatches on `type`:
      - "fixed_per_lot": MT4-style (`per_lot_per_side_usd`)
      - "volume_based":  cTrader-style (`per_100k_notional_usd`)
    """
    schedule_type = raw.get("type", "fixed_per_lot")
    if schedule_type == "fixed_per_lot":
        return CommissionSchedule(
            per_lot_per_side_usd=float(raw["per_lot_per_side_usd"]),
            per_100k_notional_usd=None,
            contract_size=float(raw["contract_size"]),
            min_commission_usd=float(raw.get("min_commission_usd", 0.0)),
        )
    elif schedule_type == "volume_based":
        return CommissionSchedule(
            per_lot_per_side_usd=None,
            per_100k_notional_usd=float(raw["per_100k_notional_usd"]),
            contract_size=float(raw["contract_size"]),
            min_commission_usd=float(raw.get("min_commission_usd", 0.0)),
        )
    else:
        raise ValueError(
            f"Unknown commission schedule type: {schedule_type!r}. "
            f"Expected 'fixed_per_lot' or 'volume_based'."
        )


def _build_profile(name: str, raw: dict[str, Any]) -> FeeProfile:
    """Construct a FeeProfile from a TOML profile table."""
    return FeeProfile(
        name=name,
        broker=raw.get("broker", ""),
        platform=raw.get("platform", ""),
        instrument_class=raw.get("instrument_class", ""),
        scenario=raw.get("scenario", "normal"),
        description=raw.get("description", ""),
        sources=tuple(raw.get("sources", [])),
        notes=raw.get("notes", ""),
        spread_config=_build_spread_config(raw["spread"]),
        commission_schedule=_build_commission_schedule(raw["commission"]),
    )


def _load_registry(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, FeeProfile]:
    """Read TOML → dict[profile_name → FeeProfile]."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"Broker fees config not found at {config_path}. "
            f"Expected at config/broker_fees.toml."
        )
    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    profiles_section = data.get("profiles", {})
    return {
        name: _build_profile(name, raw)
        for name, raw in profiles_section.items()
    }


# Module-level cache so we read the TOML once per process. Tests can
# call `_reload_registry()` to force re-read.
_REGISTRY_CACHE: dict[str, FeeProfile] | None = None


def _registry() -> dict[str, FeeProfile]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _load_registry()
    return _REGISTRY_CACHE


def _reload_registry(config_path: Path | None = None) -> None:
    """Force re-read of the TOML config (test helper)."""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = _load_registry(config_path or DEFAULT_CONFIG_PATH)


# ── Public API ──────────────────────────────────────────────────────────


def list_profiles(
    *,
    broker: str | None = None,
    platform: str | None = None,
    instrument_class: str | None = None,
    scenario: str | None = None,
) -> list[str]:
    """Return profile names matching the given filters.

    All filters are optional; if none provided, returns ALL profile names.
    Filters are exact-match on the FeeProfile's metadata fields.
    """
    out = []
    for name, profile in _registry().items():
        if broker is not None and profile.broker != broker:
            continue
        if platform is not None and profile.platform != platform:
            continue
        if instrument_class is not None and profile.instrument_class != instrument_class:
            continue
        if scenario is not None and profile.scenario != scenario:
            continue
        out.append(name)
    return sorted(out)


def get_profile(name: str) -> FeeProfile:
    """Return the FeeProfile by name. Raises KeyError if not found."""
    reg = _registry()
    if name not in reg:
        available = ", ".join(sorted(reg.keys()))
        raise KeyError(
            f"Unknown fee profile: {name!r}. Available: {available}"
        )
    return reg[name]


def make_fee_model(name: str) -> ICMarketsMetalFeeModel | ZeroCostFeeModel:
    """Construct a fee model from a named profile (1-line shortcut).

    Equivalent to `get_profile(name).make_fee_model()`.
    """
    return get_profile(name).make_fee_model()


def select_profile(
    *,
    broker: str = "IC Markets",
    platform: str = "ctrader",
    instrument_class: str = "xauusd_metals",
    scenario: str = "normal",
) -> str:
    """Look up a profile by its full (broker, platform, instrument, scenario) key.

    Returns the profile name. Raises ValueError if no profile matches OR
    if multiple match (the registry should be unique per tuple).
    """
    matches = list_profiles(
        broker=broker,
        platform=platform,
        instrument_class=instrument_class,
        scenario=scenario,
    )
    if not matches:
        raise ValueError(
            f"No fee profile matches: broker={broker!r}, platform={platform!r}, "
            f"instrument_class={instrument_class!r}, scenario={scenario!r}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple profiles match: {matches}. Registry should be unique."
        )
    return matches[0]


# ── Cost analysis helpers (leverage interaction) ────────────────────────


def cost_as_pct_of_margin(
    fee_usd: float,
    notional_usd: float,
    leverage: float,
) -> float:
    """Compute the cost-to-margin ratio for a position.

    Broker costs are LEVERAGE-INDEPENDENT in dollar terms but become a
    much larger fraction of margin at high leverage. Use this helper to
    sanity-check whether a strategy's costs are sustainable at the
    intended leverage level.

    Example:
        # SwiftAlmaStrategy 1-lot trade on cTrader, $94 cost
        # at $4500 gold → $450,000 notional
        for lev in (1, 10, 100, 500, 1000):
            pct = cost_as_pct_of_margin(94.0, 450_000, lev)
            print(f"{lev:>5}x leverage → {pct:.3f}% of margin")
        # 1x: 0.021%, 100x: 2.09%, 1000x: 20.9%

    Args:
        fee_usd: total round-trip cost in USD (commission + spread + slip)
        notional_usd: position notional value (quantity × price)
        leverage: leverage multiplier (1.0 = no leverage)

    Returns:
        Cost as a percentage of required margin.
    """
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
    if notional_usd <= 0:
        return 0.0
    margin = notional_usd / leverage
    return (fee_usd / margin) * 100.0
