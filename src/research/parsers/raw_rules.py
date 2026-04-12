"""Raw rules parser — structured YAML/JSON rules directly to IR.

This is the internal format for defining strategies manually. No AI needed.

Usage:
    parser = RawRulesParser()
    ir = parser.parse(open("my_strategy.yaml").read())
"""

from __future__ import annotations

from src.research.parsers.base import BaseParser, ParserRegistry
from src.research.strategy_ir import StrategyIR


class RawRulesParser(BaseParser):
    """Parses structured YAML/JSON strategy rules into IR.

    The input format IS the IR format — this parser just validates and loads.
    """

    @property
    def name(self) -> str:
        return "Raw Rules"

    @property
    def supported_formats(self) -> list[str]:
        return ["raw_rules", "yaml", "yml"]

    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse YAML source directly into StrategyIR."""
        ir = StrategyIR.from_yaml(source)

        # Apply metadata overrides
        if metadata:
            if "name" in metadata:
                ir.name = metadata["name"]
            if "markets" in metadata:
                ir.markets = metadata["markets"]
            if "timeframe" in metadata:
                ir.timeframe = metadata["timeframe"]

        ir.source_format = "raw_rules"
        return ir


# Auto-register
ParserRegistry.register(RawRulesParser())
