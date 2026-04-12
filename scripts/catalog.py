"""Strategy Research Catalog CLI.

Usage:
    python -m scripts.catalog list
    python -m scripts.catalog list --status researched
    python -m scripts.catalog list --category rl
    python -m scripts.catalog list --min-sharpe 2.0
    python -m scripts.catalog list --max-effort 7
    python -m scripts.catalog list --market ETHUSDT
    python -m scripts.catalog show wf_ema
    python -m scripts.catalog validations wf_ema
    python -m scripts.catalog summary
"""

from __future__ import annotations

import argparse
import sys

from src.research.catalog import StrategyCatalog


def cmd_list(catalog: StrategyCatalog, args: argparse.Namespace) -> None:
    entries = catalog.filter(
        status=args.status,
        category=args.category,
        min_sharpe=args.min_sharpe,
        max_effort_days=args.max_effort,
        market=args.market,
    )
    print(catalog.summary_table(entries))


def cmd_show(catalog: StrategyCatalog, args: argparse.Namespace) -> None:
    print(catalog.detail_view(args.strategy_id))


def cmd_validations(catalog: StrategyCatalog, args: argparse.Namespace) -> None:
    print(catalog.validations_table(args.strategy_id))


def cmd_summary(catalog: StrategyCatalog, _args: argparse.Namespace) -> None:
    print(catalog.category_summary())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="catalog",
        description="Strategy Research Catalog — list, filter, and inspect strategies.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List strategies (with optional filters)")
    p_list.add_argument("--status", type=str, default=None, help="Filter by status")
    p_list.add_argument("--category", type=str, default=None, help="Filter by category")
    p_list.add_argument("--min-sharpe", type=float, default=None, help="Minimum Sharpe ratio")
    p_list.add_argument("--max-effort", type=int, default=None, help="Maximum effort in days")
    p_list.add_argument("--market", type=str, default=None, help="Filter by market symbol")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="Show full detail for a strategy")
    p_show.add_argument("strategy_id", type=str, help="Strategy ID (e.g., wf_ema)")
    p_show.set_defaults(func=cmd_show)

    # validations
    p_val = sub.add_parser("validations", help="Show validation history for a strategy")
    p_val.add_argument("strategy_id", type=str, help="Strategy ID")
    p_val.set_defaults(func=cmd_validations)

    # summary
    p_sum = sub.add_parser("summary", help="High-level catalog stats")
    p_sum.set_defaults(func=cmd_summary)

    args = parser.parse_args()

    catalog = StrategyCatalog()
    catalog.load()
    args.func(catalog, args)


if __name__ == "__main__":
    main()
