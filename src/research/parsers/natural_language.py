"""Natural language parser — plain text strategy descriptions to IR.

Uses Claude API to convert English descriptions into structured Strategy IR.
Requires ANTHROPIC_API_KEY environment variable.

Fallback: If no API key, uses a rule-based pattern matcher for common strategies.

Usage:
    parser = NaturalLanguageParser()
    ir = parser.parse("Buy when RSI < 30 and price touches lower Bollinger Band")
"""

from __future__ import annotations

import json
import os
import re

from src.research.parsers.base import BaseParser, ParserRegistry
from src.research.strategy_ir import (
    ConditionDef,
    IndicatorDef,
    ParameterDef,
    StopDef,
    StrategyIR,
    TakeProfitDef,
)


# Common strategy patterns for rule-based fallback
_PATTERN_MAP = {
    # Crossover patterns
    r"(?:when|if)\s+(\w+)\s*(?:crosses?\s+above|crossover)\s+(\w+)": "crossover",
    r"(?:when|if)\s+(\w+)\s*(?:crosses?\s+below|crossunder)\s+(\w+)": "crossunder",
    # RSI patterns
    r"rsi\s*(?:<|below|under)\s*(\d+)": "rsi_oversold",
    r"rsi\s*(?:>|above|over)\s*(\d+)": "rsi_overbought",
    # BB patterns
    r"(?:lower|bottom)\s+(?:bollinger|bb)\s+band": "bb_lower",
    r"(?:upper|top)\s+(?:bollinger|bb)\s+band": "bb_upper",
    # SMA/EMA patterns
    r"(\d+)\s*(?:ema|EMA)\s*(?:crosses?\s+above|crossover)\s+(\d+)\s*(?:ema|EMA)": "ema_crossover",
    r"(\d+)\s*(?:sma|SMA)\s*(?:crosses?\s+above|crossover)\s+(\d+)\s*(?:sma|SMA)": "sma_crossover",
}


class NaturalLanguageParser(BaseParser):
    """Parses natural language strategy descriptions into IR."""

    @property
    def name(self) -> str:
        return "Natural Language"

    @property
    def supported_formats(self) -> list[str]:
        return ["natural_language", "natural", "text", "english"]

    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse a natural language strategy description.

        Tries LLM first (if API key available), falls back to pattern matching.
        """
        metadata = metadata or {}
        api_key = os.environ.get("ANTHROPIC_API_KEY")

        if api_key:
            try:
                return self._parse_with_llm(source, metadata, api_key)
            except Exception:
                pass

        # Fallback: rule-based pattern matching
        return self._parse_with_rules(source, metadata)

    def _parse_with_llm(self, source: str, metadata: dict, api_key: str) -> StrategyIR:
        """Use Claude API to convert text to IR."""
        import httpx

        prompt = f"""Convert this trading strategy description into a structured YAML format.

Strategy description: "{source}"

Output a YAML document with this exact structure (no extra text, just YAML):
```yaml
name: "strategy name"
timeframe: "1h"
indicators:
  - name: sma/ema/rsi/bbands/atr/adx/macd
    period: 14
entry_long:
  type: crossover/crossunder/threshold_above/threshold_below/and
  fast: "COLUMN_NAME"  # for crossover/crossunder
  slow: "COLUMN_NAME"
  column: "COLUMN_NAME"  # for threshold
  threshold: 30.0
  sub_conditions: []  # for and/or type
exit_long:
  type: crossover/crossunder/threshold_above/threshold_below
  fast: "COLUMN_NAME"
  slow: "COLUMN_NAME"
stop_loss:
  type: none/fixed_pct/atr_mult
  value: 2.0
```

Column names follow the pattern: SMA_10, EMA_21, RSI_14, BBU_20, BBM_20, BBL_20, ATR_14, ADX_14, MACD.
Only include fields that are relevant. Use "and" type with sub_conditions for multiple conditions."""

        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["content"][0]["text"]

        # Extract YAML from response
        yaml_match = re.search(r"```yaml\s*\n(.*?)\n```", content, re.DOTALL)
        if yaml_match:
            yaml_str = yaml_match.group(1)
        else:
            yaml_str = content

        ir = StrategyIR.from_yaml(yaml_str)
        ir.source_format = "natural_language"
        if metadata.get("name"):
            ir.name = metadata["name"]
        if metadata.get("markets"):
            ir.markets = metadata["markets"]
        if metadata.get("timeframe"):
            ir.timeframe = metadata["timeframe"]
        return ir

    def _parse_with_rules(self, source: str, metadata: dict) -> StrategyIR:
        """Rule-based pattern matching fallback."""
        source_lower = source.lower()
        indicators: list[IndicatorDef] = []
        entry_long: ConditionDef | None = None
        exit_long: ConditionDef | None = None
        params: list[ParameterDef] = []

        # Check for EMA crossover pattern
        ema_match = re.search(r"(\d+)\s*ema\s*(?:cross(?:es|over)?)\s*(?:above)?\s*(\d+)\s*ema", source_lower)
        sma_match = re.search(r"(\d+)\s*sma\s*(?:cross(?:es|over)?)\s*(?:above)?\s*(\d+)\s*sma", source_lower)

        if ema_match:
            fast, slow = int(ema_match.group(1)), int(ema_match.group(2))
            indicators.append(IndicatorDef(name="ema", period=fast, column=f"EMA_{fast}"))
            indicators.append(IndicatorDef(name="ema", period=slow, column=f"EMA_{slow}"))
            entry_long = ConditionDef(type="crossover", fast=f"EMA_{fast}", slow=f"EMA_{slow}")
            exit_long = ConditionDef(type="crossunder", fast=f"EMA_{fast}", slow=f"EMA_{slow}")

        elif sma_match:
            fast, slow = int(sma_match.group(1)), int(sma_match.group(2))
            indicators.append(IndicatorDef(name="sma", period=fast, column=f"SMA_{fast}"))
            indicators.append(IndicatorDef(name="sma", period=slow, column=f"SMA_{slow}"))
            entry_long = ConditionDef(type="crossover", fast=f"SMA_{fast}", slow=f"SMA_{slow}")
            exit_long = ConditionDef(type="crossunder", fast=f"SMA_{fast}", slow=f"SMA_{slow}")

        # RSI conditions
        rsi_buy = re.search(r"rsi\s*(?:<|below|under)\s*(\d+)", source_lower)
        rsi_sell = re.search(r"rsi\s*(?:>|above|over)\s*(\d+)", source_lower)

        if rsi_buy and not entry_long:
            period = 14
            rsi_period_match = re.search(r"rsi\s*\(?(\d+)\)?", source_lower)
            if rsi_period_match:
                period = int(rsi_period_match.group(1))
            indicators.append(IndicatorDef(name="rsi", period=period, column=f"RSI_{period}"))
            threshold = int(rsi_buy.group(1))
            entry_long = ConditionDef(type="threshold_below", column=f"RSI_{period}", threshold=threshold)
            if rsi_sell:
                sell_threshold = int(rsi_sell.group(1))
                exit_long = ConditionDef(type="threshold_above", column=f"RSI_{period}", threshold=sell_threshold)
            else:
                exit_long = ConditionDef(type="threshold_above", column=f"RSI_{period}", threshold=70)

        # Bollinger Band reference
        if "bollinger" in source_lower or "bb" in source_lower:
            bb_period = 20
            bb_match = re.search(r"bb\s*\(?(\d+)\)?", source_lower)
            if bb_match:
                bb_period = int(bb_match.group(1))
            if not any(i.name == "bbands" for i in indicators):
                indicators.append(IndicatorDef(name="bbands", period=bb_period))

            if "lower" in source_lower and not entry_long:
                entry_long = ConditionDef(
                    type="threshold_below", column=f"BBL_{bb_period}", threshold=0,
                )

        # Default if nothing matched
        if not indicators:
            indicators = [
                IndicatorDef(name="sma", period=10, column="SMA_10"),
                IndicatorDef(name="sma", period=20, column="SMA_20"),
            ]
            entry_long = ConditionDef(type="crossover", fast="SMA_10", slow="SMA_20")
            exit_long = ConditionDef(type="crossunder", fast="SMA_10", slow="SMA_20")

        return StrategyIR(
            name=metadata.get("name", "nl_strategy"),
            source_format="natural_language",
            markets=metadata.get("markets", ["BTCUSDT"]),
            timeframe=metadata.get("timeframe", "1h"),
            description=source,
            parameters=params,
            indicators=indicators,
            entry_long=entry_long,
            exit_long=exit_long,
        )


# Auto-register
ParserRegistry.register(NaturalLanguageParser())
