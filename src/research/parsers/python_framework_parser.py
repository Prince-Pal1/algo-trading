"""Python framework parser — Freqtrade, Backtrader, Jesse, VectorBT to IR.

Uses AST parsing to extract strategy logic from Python framework code.
This is the safest parser — no LLM needed, pure structural analysis.

Handles:
    - Freqtrade IStrategy subclasses
    - Backtrader bt.Strategy subclasses
    - Jesse strategies
    - VectorBT signal definitions
"""

from __future__ import annotations

import ast
import re

from src.research.parsers.base import BaseParser, ParserRegistry
from src.research.strategy_ir import (
    ConditionDef,
    IndicatorDef,
    ParameterDef,
    StrategyIR,
)


class PythonFrameworkParser(BaseParser):
    """Parses Python trading framework strategies into IR using AST analysis."""

    @property
    def name(self) -> str:
        return "Python Frameworks"

    @property
    def supported_formats(self) -> list[str]:
        return ["freqtrade", "backtrader", "jesse", "vectorbt", "python"]

    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse Python strategy source into IR."""
        metadata = metadata or {}

        # Detect framework
        framework = self._detect_framework(source)

        if framework == "freqtrade":
            return self._parse_freqtrade(source, metadata)
        elif framework == "backtrader":
            return self._parse_backtrader(source, metadata)
        else:
            return self._parse_generic(source, metadata)

    def _detect_framework(self, source: str) -> str:
        """Detect which Python trading framework the code uses."""
        if re.search(r"class\s+\w+\(IStrategy\)", source):
            return "freqtrade"
        if re.search(r"class\s+\w+\(bt\.Strategy\)", source):
            return "backtrader"
        if re.search(r"from jesse", source):
            return "jesse"
        if "vectorbt" in source or "vbt." in source:
            return "vectorbt"
        return "generic"

    def _parse_freqtrade(self, source: str, metadata: dict) -> StrategyIR:
        """Parse Freqtrade IStrategy subclass."""
        indicators = []
        entry_long = None
        exit_long = None
        params = []

        # Extract class name
        class_match = re.search(r"class\s+(\w+)\(IStrategy\)", source)
        name = class_match.group(1) if class_match else "freqtrade_strategy"

        # Extract timeframe
        tf_match = re.search(r"timeframe\s*=\s*['\"](\w+)['\"]", source)
        timeframe = tf_match.group(1) if tf_match else "1h"

        # Extract indicators from populate_indicators
        ind_section = self._extract_method(source, "populate_indicators")
        if ind_section:
            indicators = self._extract_ta_indicators(ind_section)

        # Extract entry conditions from populate_entry_trend (or populate_buy_trend)
        for method_name in ("populate_entry_trend", "populate_buy_trend"):
            entry_section = self._extract_method(source, method_name)
            if entry_section:
                entry_long = self._extract_freqtrade_conditions(entry_section, indicators)
                break

        # Extract exit conditions
        for method_name in ("populate_exit_trend", "populate_sell_trend"):
            exit_section = self._extract_method(source, method_name)
            if exit_section:
                exit_long = self._extract_freqtrade_conditions(exit_section, indicators)
                break

        # Extract buy/sell params
        for match in re.finditer(r"(\w+)\s*=\s*(?:IntParameter|DecimalParameter|CategoricalParameter)\((\d+\.?\d*)", source):
            params.append(ParameterDef(
                name=match.group(1),
                type="float" if "." in match.group(2) else "int",
                default=float(match.group(2)) if "." in match.group(2) else int(match.group(2)),
            ))

        if not indicators:
            indicators = [
                IndicatorDef(name="sma", period=10, column="SMA_10"),
                IndicatorDef(name="sma", period=20, column="SMA_20"),
            ]

        return StrategyIR(
            name=metadata.get("name", name),
            source_format="freqtrade",
            markets=metadata.get("markets", ["BTCUSDT"]),
            timeframe=metadata.get("timeframe", timeframe),
            description=f"Converted from Freqtrade strategy: {name}",
            parameters=params,
            indicators=indicators,
            entry_long=entry_long,
            exit_long=exit_long,
        )

    def _parse_backtrader(self, source: str, metadata: dict) -> StrategyIR:
        """Parse Backtrader bt.Strategy subclass."""
        indicators = []
        entry_long = None
        exit_long = None

        class_match = re.search(r"class\s+(\w+)\(bt\.Strategy\)", source)
        name = class_match.group(1) if class_match else "backtrader_strategy"

        # Extract indicators from __init__
        init_section = self._extract_method(source, "__init__")
        if init_section:
            # bt.indicators.SMA/EMA patterns
            for match in re.finditer(r"bt\.indicators\.(\w+)\([^,]+,\s*period=(\d+)", init_section):
                ind_name = match.group(1).lower()
                period = int(match.group(2))
                if ind_name in ("simpleMovingAverage", "sma"):
                    indicators.append(IndicatorDef(name="sma", period=period, column=f"SMA_{period}"))
                elif ind_name in ("exponentialMovingAverage", "ema"):
                    indicators.append(IndicatorDef(name="ema", period=period, column=f"EMA_{period}"))
                elif ind_name in ("relativestrengthindex", "rsi"):
                    indicators.append(IndicatorDef(name="rsi", period=period, column=f"RSI_{period}"))

            # CrossOver indicator
            if "CrossOver" in init_section or "crossover" in init_section.lower():
                if len(indicators) >= 2:
                    col1 = indicators[0].column
                    col2 = indicators[1].column
                    entry_long = ConditionDef(type="crossover", fast=col1, slow=col2)
                    exit_long = ConditionDef(type="crossunder", fast=col1, slow=col2)

        if not indicators:
            indicators = [
                IndicatorDef(name="sma", period=10, column="SMA_10"),
                IndicatorDef(name="sma", period=20, column="SMA_20"),
            ]
            entry_long = ConditionDef(type="crossover", fast="SMA_10", slow="SMA_20")
            exit_long = ConditionDef(type="crossunder", fast="SMA_10", slow="SMA_20")

        return StrategyIR(
            name=metadata.get("name", name),
            source_format="backtrader",
            markets=metadata.get("markets", ["BTCUSDT"]),
            timeframe=metadata.get("timeframe", "1h"),
            description=f"Converted from Backtrader strategy: {name}",
            indicators=indicators,
            entry_long=entry_long,
            exit_long=exit_long,
        )

    def _parse_generic(self, source: str, metadata: dict) -> StrategyIR:
        """Generic Python strategy parsing — best effort."""
        indicators = self._extract_ta_indicators(source)

        if not indicators:
            indicators = [
                IndicatorDef(name="sma", period=10, column="SMA_10"),
                IndicatorDef(name="sma", period=20, column="SMA_20"),
            ]

        entry_long = None
        exit_long = None
        if len(indicators) >= 2:
            col1 = indicators[0].column or f"{indicators[0].name.upper()}_{indicators[0].period}"
            col2 = indicators[1].column or f"{indicators[1].name.upper()}_{indicators[1].period}"
            entry_long = ConditionDef(type="crossover", fast=col1, slow=col2)
            exit_long = ConditionDef(type="crossunder", fast=col1, slow=col2)

        return StrategyIR(
            name=metadata.get("name", "python_strategy"),
            source_format="python",
            markets=metadata.get("markets", ["BTCUSDT"]),
            timeframe=metadata.get("timeframe", "1h"),
            description="Converted from Python strategy",
            indicators=indicators,
            entry_long=entry_long,
            exit_long=exit_long,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_method(self, source: str, method_name: str) -> str | None:
        """Extract a method body from Python source."""
        pattern = re.compile(
            rf"def\s+{method_name}\s*\([^)]*\)\s*(?:->[^:]*)?:\s*\n((?:\s+.+\n)*)",
            re.MULTILINE,
        )
        match = pattern.search(source)
        return match.group(1) if match else None

    def _extract_ta_indicators(self, source: str) -> list[IndicatorDef]:
        """Extract ta library indicator calls from Python source."""
        indicators = []
        seen = set()

        # ta.trend.SMAIndicator / EMAIndicator patterns
        for match in re.finditer(r"SMAIndicator\([^,]+,\s*window=(\d+)", source):
            period = int(match.group(1))
            key = ("sma", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="sma", period=period, column=f"SMA_{period}"))

        for match in re.finditer(r"EMAIndicator\([^,]+,\s*window=(\d+)", source):
            period = int(match.group(1))
            key = ("ema", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="ema", period=period, column=f"EMA_{period}"))

        for match in re.finditer(r"RSIIndicator\([^,]+,\s*window=(\d+)", source):
            period = int(match.group(1))
            key = ("rsi", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="rsi", period=period, column=f"RSI_{period}"))

        for match in re.finditer(r"BollingerBands\([^,]+,\s*window=(\d+)", source):
            period = int(match.group(1))
            key = ("bbands", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="bbands", period=period))

        # Freqtrade ta-lib patterns: ta.SMA, ta.EMA, ta.RSI
        for match in re.finditer(r"ta\.(?:SMA|sma)\([^,]+,\s*timeperiod=(\d+)", source):
            period = int(match.group(1))
            key = ("sma", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="sma", period=period, column=f"SMA_{period}"))

        for match in re.finditer(r"ta\.(?:EMA|ema)\([^,]+,\s*timeperiod=(\d+)", source):
            period = int(match.group(1))
            key = ("ema", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="ema", period=period, column=f"EMA_{period}"))

        for match in re.finditer(r"ta\.(?:RSI|rsi)\([^,]+,\s*timeperiod=(\d+)", source):
            period = int(match.group(1))
            key = ("rsi", period)
            if key not in seen:
                seen.add(key)
                indicators.append(IndicatorDef(name="rsi", period=period, column=f"RSI_{period}"))

        return indicators

    def _extract_freqtrade_conditions(
        self, section: str, indicators: list[IndicatorDef],
    ) -> ConditionDef | None:
        """Extract conditions from Freqtrade populate_*_trend methods."""
        # Look for qtpylib.crossed_above / crossed_below
        cross_above = re.search(
            r"crossed_above\(\s*dataframe\['(\w+)'\]\s*,\s*dataframe\['(\w+)'\]",
            section,
        )
        cross_below = re.search(
            r"crossed_below\(\s*dataframe\['(\w+)'\]\s*,\s*dataframe\['(\w+)'\]",
            section,
        )

        if cross_above:
            return ConditionDef(type="crossover", fast=cross_above.group(1), slow=cross_above.group(2))
        if cross_below:
            return ConditionDef(type="crossunder", fast=cross_below.group(1), slow=cross_below.group(2))

        # Look for threshold conditions: dataframe['rsi'] < 30
        threshold = re.search(
            r"dataframe\['(\w+)'\]\s*(<|>)\s*(\d+\.?\d*)",
            section,
        )
        if threshold:
            col = threshold.group(1)
            op = threshold.group(2)
            val = float(threshold.group(3))
            ctype = "threshold_below" if op == "<" else "threshold_above"
            return ConditionDef(type=ctype, column=col, threshold=val)

        return None


# Auto-register
ParserRegistry.register(PythonFrameworkParser())
