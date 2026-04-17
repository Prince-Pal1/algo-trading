"""Active-broker pointer: single source of truth for 'which broker
is currently in use', with optional per-instrument-class overrides.

Stored at config/active_broker.toml. Shape:

    [active]
    default = "ic_markets_ctrader"

    [active.overrides]
    # optional per-instrument-class routing
    # crypto = "binance_futures"
    # us_equities = "alpaca"

    [special]
    research_baseline = "pine_zero_cost"   # always-available research profile

Any component that needs "who's the current broker" calls
`get_active_broker()` (or `get_active_broker_for_instrument_class(ic)` if
they want to honor per-class overrides).

Writes go through `set_active_broker()` which edits the TOML in place
so both the dashboard and CLI can change the active broker without
touching the file by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from src.fees.broker import Broker, get_broker, list_brokers


REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_CONFIG_PATH = REPO_ROOT / "config" / "active_broker.toml"


def _read() -> dict:
    if not ACTIVE_CONFIG_PATH.exists():
        # First-run default: prefer ic_markets_ctrader if available, else first broker
        brokers = list_brokers()
        default_id = "ic_markets_ctrader" if "ic_markets_ctrader" in brokers else (brokers[0] if brokers else "")
        return {
            "active": {"default": default_id, "overrides": {}},
            "special": {"research_baseline": "pine_zero_cost"},
        }
    with open(ACTIVE_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def get_active_broker_id() -> str:
    data = _read()
    return data.get("active", {}).get("default", "")


def get_active_broker() -> Broker:
    """Return the currently active Broker instance (the default)."""
    bid = get_active_broker_id()
    if not bid:
        raise RuntimeError(
            "No active broker set. Write config/active_broker.toml with "
            "[active]\\ndefault = \"<broker_id>\""
        )
    return get_broker(bid)


def get_active_broker_for_instrument_class(instrument_class: str) -> Broker:
    """Honor per-instrument-class overrides.

    If the active_broker TOML has an override entry for this instrument_class,
    return that broker; otherwise fall back to the default.

    This lets you have e.g. IC Markets for gold/FX and a different broker
    for crypto — same config file, one lookup.
    """
    data = _read()
    overrides = data.get("active", {}).get("overrides", {}) or {}
    bid = overrides.get(instrument_class) or data.get("active", {}).get("default", "")
    if not bid:
        raise RuntimeError(
            f"No active broker resolved for instrument_class={instrument_class!r}"
        )
    return get_broker(bid)


def set_active_broker(
    broker_id: str,
    *,
    instrument_class: str | None = None,
) -> None:
    """Update the active broker.

    If instrument_class is None → updates [active].default
    If instrument_class is set  → updates [active.overrides.<class>]

    Validates that broker_id exists in the registry before writing.
    """
    # Validate first
    get_broker(broker_id)

    # Read current file content (preserve layout)
    if ACTIVE_CONFIG_PATH.exists():
        text = ACTIVE_CONFIG_PATH.read_text()
        data = tomllib.loads(text)
    else:
        data = _read()
        text = ""

    # Mutate dict
    data.setdefault("active", {})
    if instrument_class is None:
        data["active"]["default"] = broker_id
    else:
        data["active"].setdefault("overrides", {})
        data["active"]["overrides"][instrument_class] = broker_id
    data.setdefault("special", {}).setdefault("research_baseline", "pine_zero_cost")

    # Write back (simple TOML re-serializer so we don't drag a new dependency in)
    out = _serialize(data)
    ACTIVE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_CONFIG_PATH.write_text(out)


def _serialize(data: dict) -> str:
    """Minimal TOML serializer for the active_broker.toml shape.

    We control this file's shape, so a hand-rolled emitter is simpler
    than adding a tomli-w dependency. Produces stable, diff-friendly
    output (sorted keys within each section).
    """
    lines: list[str] = []
    active = data.get("active", {})
    lines.append("[active]")
    lines.append(f'default = "{active.get("default", "")}"')
    lines.append("")
    overrides = active.get("overrides", {}) or {}
    if overrides:
        lines.append("[active.overrides]")
        for k in sorted(overrides.keys()):
            lines.append(f'{k} = "{overrides[k]}"')
        lines.append("")
    else:
        lines.append("[active.overrides]")
        lines.append("# e.g. crypto = \"binance_futures\"")
        lines.append("")
    special = data.get("special", {})
    lines.append("[special]")
    for k in sorted(special.keys()):
        lines.append(f'{k} = "{special[k]}"')
    lines.append("")
    return "\n".join(lines)
