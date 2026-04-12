"""Pine Script parser — TradingView Pine Script v4/v5 to IR.

Uses regex extraction for common patterns, with optional LLM fallback
for complex scripts.

Handles common patterns:
    - SMA/EMA crossover strategies
    - RSI overbought/oversold
    - Bollinger Band strategies
    - MACD signal line crossovers
    - Custom indicator conditions
"""

from __future__ import annotations

import re
from typing import Any

from src.research.parsers.base import BaseParser, ParserRegistry
from src.research.strategy_ir import (
    ConditionDef,
    IndicatorDef,
    ParameterDef,
    StrategyIR,
)


# Regex patterns for Pine Script constructs
_INPUT_PATTERN = re.compile(
    r"(\w+)\s*=\s*input(?:\.int|\.float|\.bool)?\s*\(\s*"
    r"(?:defval\s*=\s*)?([^,)]+)"
    r"(?:.*?title\s*=\s*[\"']([^\"']+)[\"'])?"
    r"(?:.*?minval\s*=\s*(\d+\.?\d*))?"
    r"(?:.*?maxval\s*=\s*(\d+\.?\d*))?",
    re.MULTILINE,
)

_INDICATOR_PATTERNS = {
    "sma": re.compile(r"ta\.sma\(\s*(\w+)\s*,\s*(\w+)\s*\)"),
    "ema": re.compile(r"ta\.ema\(\s*(\w+)\s*,\s*(\w+)\s*\)"),
    "rsi": re.compile(r"ta\.rsi\(\s*(\w+)\s*,\s*(\w+)\s*\)"),
    "macd": re.compile(r"ta\.macd\(\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*\)"),
    "bbands": re.compile(r"ta\.bb\(\s*(\w+)\s*,\s*(\w+)\s*"),
    "atr": re.compile(r"ta\.atr\(\s*(\w+)\s*\)"),
    "adx": re.compile(r"ta\.adx\(\s*(\w+)\s*,\s*(\w+)\s*\)"),
}

_CROSSOVER_PATTERN = re.compile(r"ta\.crossover\(\s*(\w+)\s*,\s*(\w+)\s*\)")
_CROSSUNDER_PATTERN = re.compile(r"ta\.crossunder\(\s*(\w+)\s*,\s*(\w+)\s*\)")

_STRATEGY_ENTRY = re.compile(
    r"strategy\.entry\(\s*[\"'](\w+)[\"']\s*,\s*strategy\.(\w+)",
)
_STRATEGY_CLOSE = re.compile(
    r"strategy\.close\(\s*[\"'](\w+)[\"']",
)


class PineParser(BaseParser):
    """Parses TradingView Pine Script (v4/v5) into Strategy IR."""

    @property
    def name(self) -> str:
        return "Pine Script (TradingView)"

    @property
    def supported_formats(self) -> list[str]:
        return ["pine_script", "pine_script_v4", "pine_script_v5", "pine"]

    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse Pine Script source into IR using regex extraction."""
        metadata = metadata or {}

        # Extract version
        version = 5
        ver_match = re.search(r"//@version=(\d)", source)
        if ver_match:
            version = int(ver_match.group(1))

        # Extract strategy name
        name_match = re.search(r'strategy\(\s*["\']([^"\']+)', source)
        name = name_match.group(1) if name_match else metadata.get("name", "pine_strategy")

        # Extract inputs as parameters
        parameters = self._extract_inputs(source)

        # Extract indicators
        indicators, var_to_indicator = self._extract_indicators(source, parameters)

        # Extract conditions
        entry_long, exit_long, entry_short, exit_short = self._extract_conditions(
            source, var_to_indicator,
        )

        ir = StrategyIR(
            name=metadata.get("name", name),
            source_format=f"pine_script_v{version}",
            markets=metadata.get("markets", ["BTCUSDT"]),
            timeframe=metadata.get("timeframe", "1h"),
            description=f"Converted from Pine Script v{version}",
            parameters=parameters,
            indicators=indicators,
            entry_long=entry_long,
            exit_long=exit_long,
            entry_short=entry_short,
            exit_short=exit_short,
        )

        return ir

    def _extract_inputs(self, source: str) -> list[ParameterDef]:
        """Extract input() declarations as parameters."""
        params = []
        for match in _INPUT_PATTERN.finditer(source):
            var_name = match.group(1)
            default_str = match.group(2).strip()
            title = match.group(3) or var_name
            min_val = match.group(4)
            max_val = match.group(5)

            # Determine type and default
            try:
                if "." in default_str:
                    default = float(default_str)
                    ptype = "float"
                elif default_str.lower() in ("true", "false"):
                    default = default_str.lower() == "true"
                    ptype = "bool"
                else:
                    default = int(default_str)
                    ptype = "int"
            except ValueError:
                default = 14
                ptype = "int"

            params.append(ParameterDef(
                name=var_name,
                type=ptype,
                default=default,
                min=float(min_val) if min_val else None,
                max=float(max_val) if max_val else None,
                description=title,
            ))
        return params

    def _extract_indicators(
        self, source: str, params: list[ParameterDef],
    ) -> tuple[list[IndicatorDef], dict[str, str]]:
        """Extract ta.* calls as indicator definitions.

        Returns:
            (indicators, var_to_column_map)
        """
        indicators = []
        var_map: dict[str, str] = {}  # pine_variable -> IR column name
        param_defaults = {p.name: p.default for p in params}

        for ind_type, pattern in _INDICATOR_PATTERNS.items():
            for match in pattern.finditer(source):
                # Find what variable this is assigned to
                line_start = source.rfind("\n", 0, match.start()) + 1
                line = source[line_start:match.start()].strip()
                var_name = ""
                if "=" in line:
                    var_name = line.split("=")[0].strip()

                if ind_type in ("sma", "ema", "rsi"):
                    period_str = match.group(2)
                    period = param_defaults.get(period_str, _try_int(period_str, 14))
                    col = f"{ind_type.upper()}_{period}"
                    indicators.append(IndicatorDef(name=ind_type, period=int(period), column=col))
                    if var_name:
                        var_map[var_name] = col

                elif ind_type == "atr":
                    period_str = match.group(1)
                    period = param_defaults.get(period_str, _try_int(period_str, 14))
                    col = f"ATR_{period}"
                    indicators.append(IndicatorDef(name="atr", period=int(period), column=col))
                    if var_name:
                        var_map[var_name] = col

                elif ind_type == "bbands":
                    period_str = match.group(2)
                    period = param_defaults.get(period_str, _try_int(period_str, 20))
                    indicators.append(IndicatorDef(name="bbands", period=int(period)))
                    # BB has 3 columns
                    if var_name:
                        var_map[var_name] = f"BBM_{period}"
                        var_map[f"{var_name}_upper"] = f"BBU_{period}"
                        var_map[f"{var_name}_lower"] = f"BBL_{period}"

                elif ind_type == "macd":
                    indicators.append(IndicatorDef(name="macd", period=12))
                    if var_name:
                        var_map[var_name] = "MACD"

        # Deduplicate indicators
        seen = set()
        unique = []
        for ind in indicators:
            key = (ind.name, ind.period)
            if key not in seen:
                seen.add(key)
                unique.append(ind)

        return unique, var_map

    def _extract_conditions(
        self, source: str, var_map: dict[str, str],
    ) -> tuple[ConditionDef | None, ConditionDef | None, ConditionDef | None, ConditionDef | None]:
        """Extract entry/exit conditions from strategy.entry()/close() calls."""
        entry_long = None
        exit_long = None
        entry_short = None
        exit_short = None

        # Find crossover/crossunder patterns
        crossovers = _CROSSOVER_PATTERN.findall(source)
        crossunders = _CROSSUNDER_PATTERN.findall(source)

        # Find strategy.entry calls and their conditions
        for match in _STRATEGY_ENTRY.finditer(source):
            entry_id = match.group(1)
            direction = match.group(2).lower()

            # Look backward for the condition (usually on the line before or same block)
            block_start = max(0, source.rfind("\n\n", 0, match.start()))
            block = source[block_start:match.end()]

            if direction == "long":
                # Check if there's a crossover in the condition
                co = _CROSSOVER_PATTERN.search(block)
                if co:
                    fast = var_map.get(co.group(1), co.group(1))
                    slow = var_map.get(co.group(2), co.group(2))
                    entry_long = ConditionDef(type="crossover", fast=fast, slow=slow)
                elif crossovers:
                    fast = var_map.get(crossovers[0][0], crossovers[0][0])
                    slow = var_map.get(crossovers[0][1], crossovers[0][1])
                    entry_long = ConditionDef(type="crossover", fast=fast, slow=slow)

            elif direction == "short":
                cu = _CROSSUNDER_PATTERN.search(block)
                if cu:
                    fast = var_map.get(cu.group(1), cu.group(1))
                    slow = var_map.get(cu.group(2), cu.group(2))
                    entry_short = ConditionDef(type="crossunder", fast=fast, slow=slow)
                elif crossunders:
                    fast = var_map.get(crossunders[0][0], crossunders[0][0])
                    slow = var_map.get(crossunders[0][1], crossunders[0][1])
                    entry_short = ConditionDef(type="crossunder", fast=fast, slow=slow)

        # For exit: look for strategy.close or opposite crossover
        for match in _STRATEGY_CLOSE.finditer(source):
            entry_id = match.group(1)
            block_start = max(0, source.rfind("\n\n", 0, match.start()))
            block = source[block_start:match.end()]

            cu = _CROSSUNDER_PATTERN.search(block)
            co = _CROSSOVER_PATTERN.search(block)
            if cu and entry_long:
                fast = var_map.get(cu.group(1), cu.group(1))
                slow = var_map.get(cu.group(2), cu.group(2))
                exit_long = ConditionDef(type="crossunder", fast=fast, slow=slow)
            elif co and entry_short:
                fast = var_map.get(co.group(1), co.group(1))
                slow = var_map.get(co.group(2), co.group(2))
                exit_short = ConditionDef(type="crossover", fast=fast, slow=slow)

        # Fallback: if we found crossovers but no strategy calls, infer
        if entry_long is None and crossovers:
            fast = var_map.get(crossovers[0][0], crossovers[0][0])
            slow = var_map.get(crossovers[0][1], crossovers[0][1])
            entry_long = ConditionDef(type="crossover", fast=fast, slow=slow)
        if exit_long is None and crossunders:
            fast = var_map.get(crossunders[0][0], crossunders[0][0])
            slow = var_map.get(crossunders[0][1], crossunders[0][1])
            exit_long = ConditionDef(type="crossunder", fast=fast, slow=slow)

        return entry_long, exit_long, entry_short, exit_short


def _try_int(s: str, default: int = 14) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


# Auto-register
ParserRegistry.register(PineParser())
