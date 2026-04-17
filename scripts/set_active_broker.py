#!/usr/bin/env python3
"""CLI to switch the active broker in config/active_broker.toml.

Usage:
    # Set default broker for all instrument classes
    python3 scripts/set_active_broker.py ic_markets_mt4

    # Set per-instrument-class override
    python3 scripts/set_active_broker.py ic_markets_mt4 --instrument-class xauusd_metals

    # List available brokers
    python3 scripts/set_active_broker.py --list

    # Show current setting
    python3 scripts/set_active_broker.py --show
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fees.active import (  # noqa: E402
    get_active_broker_id,
    set_active_broker,
)
from src.fees.broker import list_brokers  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("broker_id", nargs="?", help="Broker ID to activate")
    p.add_argument("--instrument-class", default=None, help="Per-class override (e.g. 'xauusd_metals')")
    p.add_argument("--list", action="store_true", help="List available brokers and exit")
    p.add_argument("--show", action="store_true", help="Show current active broker and exit")
    args = p.parse_args()

    if args.list:
        brokers = list_brokers()
        print("Available brokers (from config/brokers/*.toml):")
        for b in brokers:
            from src.fees.broker import get_broker
            meta = get_broker(b)
            print(f"  {b}  ({meta.name} / {meta.platform}, {meta.regulator or '(no regulator)'})")
        return 0

    if args.show:
        print(f"Active broker (default): {get_active_broker_id()}")
        return 0

    if not args.broker_id:
        p.print_help()
        print("\nError: pass a broker_id, or use --list / --show", file=sys.stderr)
        return 1

    try:
        set_active_broker(args.broker_id, instrument_class=args.instrument_class)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Hint: run with --list to see available brokers.", file=sys.stderr)
        return 1

    target = f"[{args.instrument_class} override]" if args.instrument_class else "[default]"
    print(f"✓ Active broker {target} set to: {args.broker_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
