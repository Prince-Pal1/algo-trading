"""Base parser ABC and format auto-detection registry.

Every parser converts a source format into StrategyIR.
The ParserRegistry handles format detection and parser lookup.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from src.research.strategy_ir import StrategyIR


class BaseParser(ABC):
    """Abstract base for all strategy format parsers."""

    @abstractmethod
    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse source code/text into Strategy IR.

        Args:
            source: The strategy source code or description.
            metadata: Optional metadata (name, markets, timeframe overrides).

        Returns:
            StrategyIR instance.
        """
        ...

    def validate(self, ir: StrategyIR) -> list[str]:
        """Validate the generated IR and return warnings."""
        return ir.validate()

    @property
    @abstractmethod
    def supported_formats(self) -> list[str]:
        """List of format identifiers this parser handles."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable parser name."""
        ...


class ParserRegistry:
    """Registry of all available parsers with auto-detection."""

    _parsers: dict[str, BaseParser] = {}

    @classmethod
    def register(cls, parser: BaseParser) -> None:
        """Register a parser for its supported formats."""
        for fmt in parser.supported_formats:
            cls._parsers[fmt] = parser

    @classmethod
    def get(cls, format_id: str) -> BaseParser | None:
        """Get parser for a specific format."""
        return cls._parsers.get(format_id)

    @classmethod
    def detect_format(cls, source: str, file_path: str | None = None) -> str | None:
        """Auto-detect the format of a strategy source.

        Checks file extension first, then content patterns.
        """
        # File extension detection
        if file_path:
            ext = Path(file_path).suffix.lower()
            ext_map = {
                ".pine": "pine_script",
                ".mq4": "mql4",
                ".mq5": "mql5",
                ".py": "python",
                ".yaml": "raw_rules",
                ".yml": "raw_rules",
                ".json": "webhook",
            }
            if ext in ext_map:
                return ext_map[ext]

        # Content pattern detection
        if re.search(r"//@version=\d", source):
            return "pine_script"
        if re.search(r"#property\s+(copyright|link|version)", source):
            if "MQL5" in source or "OnInit" in source:
                return "mql5"
            return "mql4"
        if re.search(r"class\s+\w+\(IStrategy\)", source):
            return "freqtrade"
        if re.search(r"class\s+\w+\(bt\.Strategy\)", source):
            return "backtrader"
        if "action" in source and "ticker" in source:
            try:
                import json
                json.loads(source)
                return "webhook"
            except (json.JSONDecodeError, ValueError):
                pass
        if re.search(r"^(name|indicators|entry_long):", source, re.MULTILINE):
            return "raw_rules"

        return None

    @classmethod
    def list_formats(cls) -> list[dict[str, str]]:
        """List all registered formats and their parsers."""
        seen = set()
        result = []
        for fmt, parser in cls._parsers.items():
            if parser.name not in seen:
                seen.add(parser.name)
                result.append({
                    "parser": parser.name,
                    "formats": parser.supported_formats,
                })
        return result

    @classmethod
    def all_formats(cls) -> list[str]:
        """List all supported format IDs."""
        return sorted(cls._parsers.keys())
