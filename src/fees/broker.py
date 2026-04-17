"""Broker registry — per-broker fee profile groupings.

Reads `config/brokers/<broker_id>.toml` files into `Broker` dataclasses.
Each broker groups FeeProfile instances by (instrument_class, scenario),
exposing `.resolve_profile(...)` for the FeeManager.

The existing flat `config/broker_fees.toml` registry (loaded by
`src.backtest.fee_profiles`) is preserved for backwards compatibility;
this module adds a broker-first view on top. See `scripts/migrate_broker_fees.py`
for the conversion.

Design notes:
- One Broker = one (broker-name × platform) combo. IC Markets has two:
  `ic_markets_ctrader` and `ic_markets_mt4`.
- Profiles are keyed by (instrument_class, scenario) within a broker.
  Scenario values: normal, news_active, illiquid, volatile, stress,
  pine_faithful. Not every broker defines every scenario; missing
  scenarios fall back to normal.
- Operational metadata (regulator, min deposit, API type) lives on
  the Broker for dashboard display + multi-broker comparison reports.
- Leverage caps are per instrument_class since brokers cap gold at
  500x while FX can go to 1000x, etc.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from src.backtest.fee_profiles import FeeProfile, _build_commission_schedule, _build_spread_config


REPO_ROOT = Path(__file__).resolve().parents[2]
BROKERS_DIR = REPO_ROOT / "config" / "brokers"


@dataclass(frozen=True)
class BrokerInstrumentProfile:
    """All the scenario profiles for one (broker × instrument_class).

    Example: IC Markets cTrader offers XAUUSD under scenarios
    {normal, news_active, illiquid, volatile}. This dataclass holds all
    four, keyed by scenario.
    """
    instrument_class: str  # "xauusd_metals", "fx_majors", etc.
    symbol_aliases: tuple[str, ...]  # which symbol names map here
    contract_size: float  # units per standard lot (100 oz for gold, 100k for FX)
    min_lot: float  # smallest tradeable lot (e.g. 0.01)
    max_lot: float  # largest single-order lot (e.g. 100)
    lot_step: float  # lot increment (e.g. 0.01)
    profiles_by_scenario: dict[str, FeeProfile] = field(default_factory=dict)

    def resolve(self, scenario: str, *, fallback: str = "normal") -> FeeProfile | None:
        """Return the profile for the requested scenario.

        If the exact scenario is not defined, falls back to `fallback`
        (default 'normal'). Returns None if neither exists.
        """
        if scenario in self.profiles_by_scenario:
            return self.profiles_by_scenario[scenario]
        return self.profiles_by_scenario.get(fallback)

    def scenarios_available(self) -> list[str]:
        return sorted(self.profiles_by_scenario.keys())


@dataclass(frozen=True)
class Broker:
    """One broker × platform combination.

    Fields:
        id: canonical slug, e.g. 'ic_markets_ctrader'. Used as the key
            in config/active_broker.toml and as the TOML filename.
        name: human-readable broker name, e.g. 'IC Markets'.
        platform: platform slug, e.g. 'ctrader' | 'mt4' | 'binance' | 'alpaca' | '(none)'.
        regulator: e.g. 'FSA Seychelles', 'ASIC', 'CySEC'.
        country: legal entity country, e.g. 'Seychelles'.
        api_type: e.g. 'ctrader_open_api', 'mt4_bridge', 'rest', 'websocket'.
        account_types: available account types, e.g. ['Raw Spread', 'Standard'].
        min_deposit_usd: minimum live account deposit.
        base_currencies: currencies you can hold the account in.
        leverage_max: {instrument_class: max_leverage}.
        instrument_profiles: {instrument_class: BrokerInstrumentProfile}.
        notes: free-form operational notes (quirks, known gotchas).
    """
    id: str
    name: str
    platform: str
    regulator: str
    country: str
    api_type: str
    account_types: tuple[str, ...]
    min_deposit_usd: float
    base_currencies: tuple[str, ...]
    leverage_max: dict[str, float] = field(default_factory=dict)
    instrument_profiles: dict[str, BrokerInstrumentProfile] = field(default_factory=dict)
    notes: str = ""

    def resolve_profile(
        self,
        *,
        instrument_class: str,
        scenario: str = "normal",
    ) -> FeeProfile | None:
        """Find the FeeProfile for (instrument_class, scenario).

        Falls back to scenario='normal' if the exact scenario isn't defined
        for this broker's handling of the instrument class. Returns None
        if the broker doesn't offer the instrument class at all.
        """
        ip = self.instrument_profiles.get(instrument_class)
        if ip is None:
            return None
        return ip.resolve(scenario)

    def resolve_instrument(
        self, *, instrument_class: str
    ) -> BrokerInstrumentProfile | None:
        return self.instrument_profiles.get(instrument_class)

    def instrument_classes(self) -> list[str]:
        return sorted(self.instrument_profiles.keys())

    def symbols(self) -> list[str]:
        out: set[str] = set()
        for ip in self.instrument_profiles.values():
            out.update(ip.symbol_aliases)
        return sorted(out)

    def supports_symbol(self, symbol: str) -> bool:
        symbol_upper = symbol.upper()
        return any(
            symbol_upper in [s.upper() for s in ip.symbol_aliases]
            for ip in self.instrument_profiles.values()
        )

    def instrument_class_for_symbol(
        self, symbol: str, *, allow_wildcard: bool = False
    ) -> str | None:
        """Return the instrument_class this broker uses for `symbol`.

        If `allow_wildcard=True` and no alias matches, a broker that has
        only an 'any' instrument_class (no declared aliases) will match
        any symbol. Used for the pine_zero_cost research baseline —
        callers that explicitly request broker_id=pine_zero_cost set this
        flag; symbol→broker auto-routing does NOT, so unknown symbols still
        raise rather than silently falling to pine.
        """
        symbol_upper = symbol.upper()
        for ic, ip in self.instrument_profiles.items():
            if symbol_upper in [s.upper() for s in ip.symbol_aliases]:
                return ic
        if allow_wildcard:
            any_ip = self.instrument_profiles.get("any")
            if any_ip is not None and not any_ip.symbol_aliases:
                return "any"
        return None

    def max_leverage_for(self, instrument_class: str) -> float:
        return self.leverage_max.get(instrument_class, 1.0)


# ── TOML loader ──────────────────────────────────────────────────────────


def _build_instrument_profile(
    instrument_class: str,
    raw: dict[str, Any],
    broker_id: str,
    platform: str,
    broker_name: str,
) -> BrokerInstrumentProfile:
    """Parse one [instruments.<class>] section + its child scenarios."""
    symbol_aliases = tuple(raw.get("symbol_aliases", []))
    contract_size = float(raw.get("contract_size", 1.0))
    min_lot = float(raw.get("min_lot", 0.01))
    max_lot = float(raw.get("max_lot", 100.0))
    lot_step = float(raw.get("lot_step", 0.01))

    scenarios_raw = raw.get("scenarios", {})
    profiles_by_scenario: dict[str, FeeProfile] = {}
    for scenario_name, scenario_raw in scenarios_raw.items():
        # Build FeeProfile using the same helpers fee_profiles.py uses
        # so both registries parse identically.
        spread_cfg = _build_spread_config(scenario_raw["spread"])
        comm_sched = _build_commission_schedule(scenario_raw["commission"])
        profile_name = f"{broker_id}__{instrument_class}__{scenario_name}"
        profile = FeeProfile(
            name=profile_name,
            broker=broker_name,
            platform=platform,
            instrument_class=instrument_class,
            scenario=scenario_name,
            description=scenario_raw.get("description", ""),
            sources=tuple(scenario_raw.get("sources", [])),
            notes=scenario_raw.get("notes", ""),
            spread_config=spread_cfg,
            commission_schedule=comm_sched,
        )
        profiles_by_scenario[scenario_name] = profile

    return BrokerInstrumentProfile(
        instrument_class=instrument_class,
        symbol_aliases=symbol_aliases,
        contract_size=contract_size,
        min_lot=min_lot,
        max_lot=max_lot,
        lot_step=lot_step,
        profiles_by_scenario=profiles_by_scenario,
    )


def _load_broker_from_toml(path: Path) -> Broker:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    br = data["broker"]
    broker_id = br["id"]
    leverage_max = {k: float(v) for k, v in data.get("leverage_max", {}).items()}

    instruments_raw = data.get("instruments", {})
    instrument_profiles: dict[str, BrokerInstrumentProfile] = {}
    for ic, raw in instruments_raw.items():
        instrument_profiles[ic] = _build_instrument_profile(
            instrument_class=ic,
            raw=raw,
            broker_id=broker_id,
            platform=br["platform"],
            broker_name=br["name"],
        )

    return Broker(
        id=broker_id,
        name=br["name"],
        platform=br["platform"],
        regulator=br.get("regulator", ""),
        country=br.get("country", ""),
        api_type=br.get("api_type", ""),
        account_types=tuple(br.get("account_types", [])),
        min_deposit_usd=float(br.get("min_deposit_usd", 0.0)),
        base_currencies=tuple(br.get("base_currencies", [])),
        leverage_max=leverage_max,
        instrument_profiles=instrument_profiles,
        notes=br.get("notes", ""),
    )


# ── Module-level registry ───────────────────────────────────────────────

_REGISTRY_CACHE: dict[str, Broker] | None = None


def load_all_brokers(
    brokers_dir: Path | None = None,
) -> dict[str, Broker]:
    """Load every config/brokers/<id>.toml into a {id: Broker} mapping."""
    directory = brokers_dir or BROKERS_DIR
    if not directory.exists():
        return {}
    out: dict[str, Broker] = {}
    for path in sorted(directory.glob("*.toml")):
        broker = _load_broker_from_toml(path)
        out[broker.id] = broker
    return out


def _registry() -> dict[str, Broker]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = load_all_brokers()
    return _REGISTRY_CACHE


def reload_brokers(brokers_dir: Path | None = None) -> None:
    """Force re-read of the broker TOMLs (test helper)."""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = load_all_brokers(brokers_dir)


def get_broker(broker_id: str) -> Broker:
    reg = _registry()
    if broker_id not in reg:
        available = ", ".join(sorted(reg.keys())) or "(none)"
        raise KeyError(
            f"Unknown broker: {broker_id!r}. Available: {available}. "
            f"Expected a TOML at config/brokers/{broker_id}.toml."
        )
    return reg[broker_id]


def list_brokers() -> list[str]:
    return sorted(_registry().keys())
