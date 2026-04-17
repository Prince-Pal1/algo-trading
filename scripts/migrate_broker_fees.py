#!/usr/bin/env python3
"""Migrate config/broker_fees.toml flat registry → config/brokers/<id>.toml files.

Reads the 21 flat [profiles.<name>] entries in broker_fees.toml and groups
them by (broker, platform), writing one config/brokers/<id>.toml per broker.

Also seeds the new illiquid + volatile scenarios for each broker-instrument
combo by multiplying the 'normal' scenario's spread/slip appropriately.

Idempotent: running twice produces the same output (we overwrite fully).

The flat broker_fees.toml remains unchanged and keeps working via
src.backtest.fee_profiles. This script's output adds a broker-first view
for the new FeeManager.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "config" / "broker_fees.toml"
DEST_DIR = REPO_ROOT / "config" / "brokers"


# Broker metadata we add during migration (not in the flat TOML).
# Source of truth: manually researched / docs/setup_icmarkets.md / memory.
BROKER_METADATA: dict[str, dict] = {
    "ic_markets_ctrader": {
        "name": "IC Markets",
        "platform": "ctrader",
        "regulator": "FSA Seychelles",
        "country": "Seychelles",
        "api_type": "ctrader_open_api",
        "account_types": ["Raw Spread", "Standard", "cTrader Raw"],
        "min_deposit_usd": 200.0,
        "base_currencies": ["USD", "EUR", "GBP", "AUD", "SGD"],
        "leverage_max": {
            "xauusd_metals": 500.0,
            "fx_majors": 500.0,
            "fx_minors": 500.0,
            "crypto": 200.0,
            "indices": 500.0,
        },
        "notes": """
IC Markets cTrader Raw Spread. Primary deployment broker for the gold
stack (G.3 Day 1 demo-clock currently running). Verified live against
demo.ctraderapi.com 2026-04-17: 351 symbols, XAUUSD at 5-pip spread,
$3/$100k commission. KYC required for sandbox→production token access.
Primary live cost fixture = weekly London/NY overlap spread sampler.
""".strip(),
    },
    "ic_markets_mt4": {
        "name": "IC Markets",
        "platform": "mt4",
        "regulator": "FSA Seychelles",
        "country": "Seychelles",
        "api_type": "mt4_bridge",
        "account_types": ["Raw Spread", "Standard"],
        "min_deposit_usd": 200.0,
        "base_currencies": ["USD", "EUR", "GBP", "AUD"],
        "leverage_max": {
            "xauusd_metals": 500.0,
            "fx_majors": 500.0,
            "fx_minors": 500.0,
        },
        "notes": """
IC Markets MT4 Raw Spread. NOT deployable on macOS (MT5 PyPI is
Windows-only C++ per CLAUDE.md; MT4 may work via Wine or VPS).
Kept in the registry for backtest comparison only — MT4 commission
is fixed $3.50/lot/side vs cTrader's volume-based $3/$100k, making
MT4 ~3.86× cheaper on gold. Research-reference profile.
""".strip(),
    },
    "pine_zero_cost": {
        "name": "(research baseline)",
        "platform": "(none)",
        "regulator": "(n/a)",
        "country": "(n/a)",
        "api_type": "(none)",
        "account_types": [],
        "min_deposit_usd": 0.0,
        "base_currencies": [],
        "leverage_max": {},
        "notes": """
Zero-cost research baseline — matches TradingView strategy tester
defaults for Pine Script port validation (Stage 0 of the strategy
dev process). NOT for production backtests. After port equivalence,
switch to a real broker profile for Phase 2 viability testing.
""".strip(),
    },
}


# Symbol aliases + contract spec per instrument_class.
INSTRUMENT_METADATA: dict[str, dict] = {
    "xauusd_metals": {
        "symbol_aliases": ["XAUUSD", "GOLD"],
        "contract_size": 100.0,   # 100 oz per standard lot
        "min_lot": 0.01,
        "max_lot": 100.0,
        "lot_step": 0.01,
    },
    "fx_majors": {
        "symbol_aliases": [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "AUDUSD", "USDCAD", "NZDUSD",
        ],
        "contract_size": 100_000.0,  # 100k base currency per standard lot
        "min_lot": 0.01,
        "max_lot": 100.0,
        "lot_step": 0.01,
    },
    "any": {
        "symbol_aliases": [],
        "contract_size": 100.0,  # caller should override
        "min_lot": 0.01,
        "max_lot": 100.0,
        "lot_step": 0.01,
    },
}


def _normalize_instrument_class(ic: str) -> str:
    """Map legacy instrument_class strings (e.g. '(any)') to TOML-valid keys."""
    if ic in ("(any)", "(none)", ""):
        return "any"
    return ic


def _infer_broker_id(broker: str, platform: str) -> str:
    """Derive canonical broker_id slug from the flat TOML's broker+platform tags."""
    if broker == "(none)" or platform == "(none)":
        return "pine_zero_cost"
    b = broker.lower().replace(" ", "_").replace("(", "").replace(")", "")
    p = platform.lower().replace(" ", "_")
    return f"{b}__{p}" if False else f"{b}_{p}"


def _derive_illiquid(normal_spread: dict) -> dict:
    """Illiquid scenario: Asian session + weekend approach.

    Empirically: liquidity halves → spread roughly 3-4× wider. Pick 3.5×
    as a central estimate. News multipliers stay the same.
    """
    out = dict(normal_spread)
    out["base_spread_pips"] = round(float(normal_spread["base_spread_pips"]) * 3.5, 3)
    out["normal_slip_pips"] = round(float(normal_spread["normal_slip_pips"]) * 3.5, 3)
    return out


def _derive_volatile(normal_spread: dict) -> dict:
    """Volatile scenario: high-ATR regime (current ATR > 1.5× 30-bar rolling).

    Empirically: spread widens but not as much as illiquid (liquidity
    is still present, just prices move). Pick 2× as central estimate.
    """
    out = dict(normal_spread)
    out["base_spread_pips"] = round(float(normal_spread["base_spread_pips"]) * 2.0, 3)
    out["normal_slip_pips"] = round(float(normal_spread["normal_slip_pips"]) * 2.0, 3)
    return out


def _emit_scenario_section(
    broker_id: str,
    instrument_class: str,
    scenario_name: str,
    scenario_data: dict,
    lines: list[str],
) -> None:
    header = f"instruments.{instrument_class}.scenarios.{scenario_name}"
    lines.append(f"[{header}]")
    if "description" in scenario_data:
        desc = scenario_data["description"].replace('"', '\\"')
        lines.append(f'description = "{desc}"')
    if "sources" in scenario_data and scenario_data["sources"]:
        lines.append("sources = [")
        for s in scenario_data["sources"]:
            s_esc = s.replace('"', '\\"')
            lines.append(f'    "{s_esc}",')
        lines.append("]")
    if "notes" in scenario_data and scenario_data["notes"]:
        notes_esc = scenario_data["notes"].replace('"""', '\\"\\"\\"')
        lines.append('notes = """')
        lines.append(notes_esc.strip())
        lines.append('"""')
    lines.append("")
    lines.append(f"[{header}.spread]")
    for k, v in scenario_data["spread"].items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        else:
            v_esc = str(v).replace('"', '\\"')
            lines.append(f'{k} = "{v_esc}"')
    lines.append("")
    lines.append(f"[{header}.commission]")
    for k, v in scenario_data["commission"].items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        else:
            v_esc = str(v).replace('"', '\\"')
            lines.append(f'{k} = "{v_esc}"')
    lines.append("")


def _emit_broker_toml(
    broker_id: str,
    profiles: list[tuple[str, dict]],
) -> str:
    """Produce the full per-broker TOML string."""
    meta = BROKER_METADATA[broker_id]
    lines: list[str] = []
    lines.append(f"# Broker: {meta['name']} / platform: {meta['platform']}")
    lines.append("#")
    lines.append("# Generated by scripts/migrate_broker_fees.py from config/broker_fees.toml.")
    lines.append("# The flat broker_fees.toml is still authoritative for the legacy")
    lines.append("# src.backtest.fee_profiles registry; this file is the broker-first view")
    lines.append("# used by src.fees.FeeManager.")
    lines.append("#")
    lines.append("# Scenarios derived during migration: illiquid (normal × 3.5) and")
    lines.append("# volatile (normal × 2.0) — refined over time from the weekly spread")
    lines.append("# sampler + fee_calibration/*.json empirical overrides.")
    lines.append("")
    lines.append("[broker]")
    lines.append(f'id = "{broker_id}"')
    lines.append(f'name = "{meta["name"]}"')
    lines.append(f'platform = "{meta["platform"]}"')
    lines.append(f'regulator = "{meta["regulator"]}"')
    lines.append(f'country = "{meta["country"]}"')
    lines.append(f'api_type = "{meta["api_type"]}"')
    acct_types = ", ".join(f'"{t}"' for t in meta["account_types"])
    lines.append(f"account_types = [{acct_types}]")
    lines.append(f'min_deposit_usd = {meta["min_deposit_usd"]}')
    base_ccys = ", ".join(f'"{c}"' for c in meta["base_currencies"])
    lines.append(f"base_currencies = [{base_ccys}]")
    notes_escaped = meta["notes"].replace('"""', '\\"\\"\\"')
    lines.append('notes = """')
    lines.append(notes_escaped)
    lines.append('"""')
    lines.append("")
    if meta["leverage_max"]:
        lines.append("[leverage_max]")
        for ic, lev in meta["leverage_max"].items():
            lines.append(f"{ic} = {lev}")
        lines.append("")

    # Group profiles by instrument_class (normalized)
    by_ic: dict[str, dict[str, dict]] = {}
    for profile_name, raw in profiles:
        ic = _normalize_instrument_class(raw.get("instrument_class", "(any)"))
        scenario = raw.get("scenario", "normal")
        by_ic.setdefault(ic, {})[scenario] = raw

    for ic in sorted(by_ic.keys()):
        imeta = INSTRUMENT_METADATA.get(ic, INSTRUMENT_METADATA["any"])
        lines.append(f"[instruments.{ic}]")
        aliases = ", ".join(f'"{s}"' for s in imeta["symbol_aliases"])
        lines.append(f"symbol_aliases = [{aliases}]")
        lines.append(f"contract_size = {imeta['contract_size']}")
        lines.append(f"min_lot = {imeta['min_lot']}")
        lines.append(f"max_lot = {imeta['max_lot']}")
        lines.append(f"lot_step = {imeta['lot_step']}")
        lines.append("")

        scenarios = by_ic[ic]

        # Derive illiquid + volatile from normal if we have a normal scenario
        # and they aren't already defined.
        if "normal" in scenarios:
            normal = scenarios["normal"]
            if "illiquid" not in scenarios:
                scenarios["illiquid"] = {
                    "description": f"{ic} under illiquid conditions (Asian session, weekend approach, holiday) — spread × 3.5 vs normal.",
                    "sources": [],
                    "notes": "Derived during migration from the 'normal' profile. Refine via the weekly sampler's illiquid-bucket captures.",
                    "spread": _derive_illiquid(normal["spread"]),
                    "commission": dict(normal["commission"]),
                }
            if "volatile" not in scenarios:
                scenarios["volatile"] = {
                    "description": f"{ic} under volatile-regime conditions (current ATR > 1.5× 30-bar rolling) — spread × 2 vs normal.",
                    "sources": [],
                    "notes": "Derived during migration from the 'normal' profile. Refine via live observation during high-vol windows.",
                    "spread": _derive_volatile(normal["spread"]),
                    "commission": dict(normal["commission"]),
                }

        # Emit each scenario
        for scenario_name in sorted(scenarios.keys()):
            _emit_scenario_section(
                broker_id=broker_id,
                instrument_class=ic,
                scenario_name=scenario_name,
                scenario_data=scenarios[scenario_name],
                lines=lines,
            )

    return "\n".join(lines) + "\n"


def main() -> int:
    if not SOURCE.exists():
        print(f"ERROR: {SOURCE} not found", file=sys.stderr)
        return 1

    with open(SOURCE, "rb") as f:
        flat = tomllib.load(f)

    profiles = flat.get("profiles", {})
    by_broker_id: dict[str, list[tuple[str, dict]]] = {}
    for name, raw in profiles.items():
        bid = _infer_broker_id(raw.get("broker", ""), raw.get("platform", ""))
        by_broker_id.setdefault(bid, []).append((name, raw))

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for bid, prof_list in by_broker_id.items():
        if bid not in BROKER_METADATA:
            print(f"WARN: no metadata for broker_id={bid!r}, skipping", file=sys.stderr)
            continue
        out = _emit_broker_toml(bid, prof_list)
        path = DEST_DIR / f"{bid}.toml"
        path.write_text(out)
        written.append(path)
        print(f"  ✓ {path.relative_to(REPO_ROOT)}  ({len(prof_list)} source profile(s))")

    print(f"\nWrote {len(written)} per-broker TOML(s) to {DEST_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
