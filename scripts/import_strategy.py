"""Multi-format strategy import CLI.

Usage:
    python -m scripts.import_strategy --format raw --file rules.yaml --name "My SMA"
    python -m scripts.import_strategy --format natural --describe "Buy when 9 EMA crosses 21 EMA"
    python -m scripts.import_strategy --format pine --file strategy.pine
    python -m scripts.import_strategy --format webhook --json '{"action":"buy","ticker":"BTCUSDT"}'
    python -m scripts.import_strategy --format mql4 --file expert.mq4
    python -m scripts.import_strategy --format freqtrade --file strategy.py
    python -m scripts.import_strategy --list-formats
    python -m scripts.import_strategy --list          # list imported strategies
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Import all parsers to trigger auto-registration
from src.research.parsers.base import ParserRegistry
import src.research.parsers.raw_rules          # noqa: F401
import src.research.parsers.natural_language   # noqa: F401
import src.research.parsers.pine_parser        # noqa: F401
import src.research.parsers.webhook_parser     # noqa: F401
import src.research.parsers.mql_parser         # noqa: F401
import src.research.parsers.python_framework_parser  # noqa: F401

from src.research.strategy_ir import StrategyIR
from src.research.codegen import IRCodeGenerator


_IMPORTED_DIR = Path("strategies/imported")


def cmd_import(args: argparse.Namespace) -> None:
    """Import a strategy from any supported format."""
    source: str
    file_path: str | None = None

    if args.describe:
        # Natural language mode
        source = args.describe
        fmt = "natural_language"
    elif args.json_str:
        # Webhook JSON mode
        source = args.json_str
        fmt = args.format or "webhook"
    elif args.file:
        # File mode
        file_path = args.file
        path = Path(file_path)
        if not path.exists():
            print(f"  ERROR: File not found: {file_path}")
            sys.exit(1)
        source = path.read_text()
        fmt = args.format
    else:
        print("  ERROR: Provide --file, --describe, or --json")
        sys.exit(1)

    # Auto-detect format if not specified
    if not fmt:
        fmt = ParserRegistry.detect_format(source, file_path)
        if fmt:
            print(f"  Auto-detected format: {fmt}")
        else:
            print("  ERROR: Could not auto-detect format. Use --format to specify.")
            sys.exit(1)

    # Get parser
    parser = ParserRegistry.get(fmt)
    if parser is None:
        print(f"  ERROR: No parser for format '{fmt}'")
        print(f"  Available: {', '.join(ParserRegistry.all_formats())}")
        sys.exit(1)

    # Build metadata
    metadata = {}
    if args.name:
        metadata["name"] = args.name
    if args.symbol:
        metadata["markets"] = [args.symbol]
    if args.tf:
        metadata["timeframe"] = args.tf

    # Parse
    print(f"  Parsing with {parser.name}...")
    ir = parser.parse(source, metadata=metadata)

    # Validate
    warnings = parser.validate(ir)
    if warnings:
        print(f"  Warnings:")
        for w in warnings:
            print(f"    - {w}")

    # Create output directory
    strategy_id = ir.name.lower().replace(" ", "_").replace("-", "_")
    out_dir = _IMPORTED_DIR / strategy_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save original source
    if file_path:
        ext = Path(file_path).suffix or ".txt"
        (out_dir / f"original{ext}").write_text(source)
    else:
        (out_dir / "original.txt").write_text(source)

    # Save IR as YAML
    ir_path = out_dir / "strategy.yaml"
    ir.to_file(str(ir_path))
    print(f"  IR saved: {ir_path}")

    # Generate Python code
    gen = IRCodeGenerator()
    py_path = gen.generate_to_file(ir, str(out_dir / "strategy.py"))
    print(f"  Python saved: {py_path}")

    # Print summary
    print(f"\n  Strategy imported successfully!")
    print(f"  Name:       {ir.name}")
    print(f"  Format:     {ir.source_format}")
    print(f"  Markets:    {', '.join(ir.markets)}")
    print(f"  Timeframe:  {ir.timeframe}")
    print(f"  Indicators: {ir.get_indicator_names()}")
    print(f"  Directory:  {out_dir}")

    # Show how to backtest
    print(f"\n  To backtest, add to STRATEGY_REGISTRY in scripts/backtest.py:")
    indicator_names = ir.get_indicator_names()
    print(f'    "{strategy_id}": {{')
    print(f'        "factory": lambda tf: load_imported_strategy("{strategy_id}", tf),')
    print(f'        "indicators": {indicator_names},')
    print(f"    }},")
    print()


def cmd_list_formats(args: argparse.Namespace) -> None:
    """List all supported formats."""
    print("\n  Supported Strategy Formats:\n")
    for entry in ParserRegistry.list_formats():
        print(f"  {entry['parser']:<30} Formats: {', '.join(entry['formats'])}")
    print()


def cmd_list_imported(args: argparse.Namespace) -> None:
    """List all imported strategies."""
    if not _IMPORTED_DIR.exists():
        print("  No imported strategies found.")
        return

    dirs = sorted(d for d in _IMPORTED_DIR.iterdir() if d.is_dir())
    if not dirs:
        print("  No imported strategies found.")
        return

    print(f"\n  Imported Strategies ({len(dirs)}):\n")
    print(f"  {'ID':<25} {'Format':<20} {'Markets':<15} {'Indicators'}")
    print(f"  {'─'*25} {'─'*20} {'─'*15} {'─'*30}")

    for d in dirs:
        ir_file = d / "strategy.yaml"
        if ir_file.exists():
            try:
                ir = StrategyIR.from_file(str(ir_file))
                print(
                    f"  {d.name:<25} {ir.source_format:<20} "
                    f"{','.join(ir.markets):<15} {ir.get_indicator_names()}"
                )
            except Exception as e:
                print(f"  {d.name:<25} ERROR: {e}")
        else:
            print(f"  {d.name:<25} (no IR file)")
    print()


def load_imported_strategy(strategy_id: str, timeframe: str):
    """Load an imported strategy by ID. Used by backtest CLI."""
    ir_path = _IMPORTED_DIR / strategy_id / "strategy.yaml"
    if not ir_path.exists():
        raise FileNotFoundError(f"No imported strategy: {strategy_id}")
    ir = StrategyIR.from_file(str(ir_path))
    gen = IRCodeGenerator()
    cls = gen.generate_and_load(ir)
    return cls(timeframe=timeframe)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="import_strategy",
        description="Import strategies from any format into the backtest system",
    )
    sub = parser.add_subparsers(dest="command")

    # import (default)
    p_import = sub.add_parser("import", help="Import a strategy")
    p_import.add_argument("--format", "-f", type=str, default=None,
                          help="Source format (pine, mql4, freqtrade, raw, natural, webhook)")
    p_import.add_argument("--file", type=str, default=None, help="Path to strategy file")
    p_import.add_argument("--describe", type=str, default=None,
                          help="Natural language strategy description")
    p_import.add_argument("--json", dest="json_str", type=str, default=None,
                          help="Webhook JSON string")
    p_import.add_argument("--name", type=str, default=None, help="Strategy name override")
    p_import.add_argument("--symbol", type=str, default="BTCUSDT", help="Symbol (default: BTCUSDT)")
    p_import.add_argument("--tf", type=str, default="1h", help="Timeframe (default: 1h)")

    # list-formats
    sub.add_parser("formats", help="List all supported formats")

    # list imported
    sub.add_parser("list", help="List imported strategies")

    args = parser.parse_args()

    if args.command == "import":
        cmd_import(args)
    elif args.command == "formats":
        cmd_list_formats(args)
    elif args.command == "list":
        cmd_list_imported(args)
    else:
        # Default: show help if no --describe or --file given on bare call
        if hasattr(args, "describe") and args.describe:
            cmd_import(args)
        elif hasattr(args, "file") and args.file:
            cmd_import(args)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
