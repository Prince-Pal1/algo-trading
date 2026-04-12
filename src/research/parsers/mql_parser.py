"""MQL4/MQL5 parser — MetaTrader Expert Advisors to IR.

Uses regex extraction for common MQL patterns. For complex EAs,
falls back to LLM translation (requires ANTHROPIC_API_KEY).

Handles:
    - iMA(), iRSI(), iBands(), iATR() indicator calls
    - OrderSend() buy/sell logic
    - Simple condition patterns
"""

from __future__ import annotations

import os
import re

from src.research.parsers.base import BaseParser, ParserRegistry
from src.research.strategy_ir import (
    ConditionDef,
    IndicatorDef,
    ParameterDef,
    StrategyIR,
)


_INDICATOR_CALLS = {
    "sma": re.compile(r"iMA\([^,]+,\s*\d+,\s*(\d+),\s*\d+,\s*MODE_SMA"),
    "ema": re.compile(r"iMA\([^,]+,\s*\d+,\s*(\d+),\s*\d+,\s*MODE_EMA"),
    "rsi": re.compile(r"iRSI\([^,]+,\s*\d+,\s*(\d+)"),
    "bbands": re.compile(r"iBands\([^,]+,\s*\d+,\s*(\d+)"),
    "atr": re.compile(r"iATR\([^,]+,\s*\d+,\s*(\d+)"),
}

_MQL5_INDICATOR_CALLS = {
    "sma": re.compile(r"iMA\([^,]+,\s*\w+,\s*(\d+),\s*\d+,\s*MODE_SMA"),
    "ema": re.compile(r"iMA\([^,]+,\s*\w+,\s*(\d+),\s*\d+,\s*MODE_EMA"),
    "rsi": re.compile(r"iRSI\([^,]+,\s*\w+,\s*(\d+)"),
}

_INPUT_PATTERN = re.compile(
    r"(?:input|extern)\s+(\w+)\s+(\w+)\s*=\s*([^;]+);",
)

_ORDER_SEND = re.compile(r"OrderSend\([^,]+,\s*(OP_BUY|OP_SELL)")


class MQLParser(BaseParser):
    """Parses MQL4/MQL5 Expert Advisors into Strategy IR."""

    @property
    def name(self) -> str:
        return "MQL4/MQL5 (MetaTrader)"

    @property
    def supported_formats(self) -> list[str]:
        return ["mql4", "mql5", "mql"]

    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse MQL source into IR."""
        metadata = metadata or {}

        # Detect MQL version
        is_mql5 = "OnInit" in source and "#include" in source
        fmt = "mql5" if is_mql5 else "mql4"

        # Try LLM first for complex EAs
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key and len(source) > 500:
            try:
                return self._parse_with_llm(source, metadata, api_key, fmt)
            except Exception:
                pass

        # Regex-based extraction
        return self._parse_with_regex(source, metadata, fmt)

    def _parse_with_regex(self, source: str, metadata: dict, fmt: str) -> StrategyIR:
        """Regex-based MQL parsing."""
        # Extract inputs
        params = []
        for match in _INPUT_PATTERN.finditer(source):
            ptype = match.group(1).lower()
            pname = match.group(2)
            pdefault = match.group(3).strip()

            if ptype in ("int", "long"):
                try:
                    params.append(ParameterDef(name=pname, type="int", default=int(pdefault)))
                except ValueError:
                    pass
            elif ptype in ("double", "float"):
                try:
                    params.append(ParameterDef(name=pname, type="float", default=float(pdefault)))
                except ValueError:
                    pass

        # Extract indicators
        indicators = []
        call_patterns = _MQL5_INDICATOR_CALLS if fmt == "mql5" else _INDICATOR_CALLS

        for ind_type, pattern in call_patterns.items():
            for match in pattern.finditer(source):
                period = int(match.group(1))
                if ind_type == "bbands":
                    indicators.append(IndicatorDef(name="bbands", period=period))
                else:
                    col = f"{ind_type.upper()}_{period}"
                    indicators.append(IndicatorDef(name=ind_type, period=period, column=col))

        # Deduplicate
        seen = set()
        unique_inds = []
        for ind in indicators:
            key = (ind.name, ind.period)
            if key not in seen:
                seen.add(key)
                unique_inds.append(ind)
        indicators = unique_inds

        # Extract entry conditions from OrderSend calls
        entry_long = None
        exit_long = None

        buy_matches = list(re.finditer(r"OP_BUY", source))
        sell_matches = list(re.finditer(r"OP_SELL", source))

        # Try to infer conditions from context
        if indicators and len(indicators) >= 2:
            # Assume crossover between first two indicators
            col1 = indicators[0].column or f"{indicators[0].name.upper()}_{indicators[0].period}"
            col2 = indicators[1].column or f"{indicators[1].name.upper()}_{indicators[1].period}"
            entry_long = ConditionDef(type="crossover", fast=col1, slow=col2)
            exit_long = ConditionDef(type="crossunder", fast=col1, slow=col2)
        elif indicators:
            col = indicators[0].column or f"{indicators[0].name.upper()}_{indicators[0].period}"
            if indicators[0].name == "rsi":
                entry_long = ConditionDef(type="threshold_below", column=col, threshold=30)
                exit_long = ConditionDef(type="threshold_above", column=col, threshold=70)

        # Default indicators if none found
        if not indicators:
            indicators = [
                IndicatorDef(name="sma", period=10, column="SMA_10"),
                IndicatorDef(name="sma", period=20, column="SMA_20"),
            ]
            entry_long = ConditionDef(type="crossover", fast="SMA_10", slow="SMA_20")
            exit_long = ConditionDef(type="crossunder", fast="SMA_10", slow="SMA_20")

        return StrategyIR(
            name=metadata.get("name", f"mql_strategy"),
            source_format=fmt,
            markets=metadata.get("markets", ["BTCUSDT"]),
            timeframe=metadata.get("timeframe", "1h"),
            description=f"Converted from {fmt.upper()} Expert Advisor",
            parameters=params,
            indicators=indicators,
            entry_long=entry_long,
            exit_long=exit_long,
        )

    def _parse_with_llm(self, source: str, metadata: dict, api_key: str, fmt: str) -> StrategyIR:
        """Use Claude to parse complex MQL code."""
        import httpx

        prompt = f"""Convert this {fmt.upper()} Expert Advisor to a YAML strategy definition.

```{fmt}
{source[:3000]}
```

Output YAML with this structure (no extra text):
```yaml
name: "strategy name"
indicators:
  - name: sma/ema/rsi/bbands/atr
    period: 14
entry_long:
  type: crossover/threshold_above/threshold_below
  fast: "COLUMN"
  slow: "COLUMN"
  column: "COLUMN"
  threshold: 30
exit_long:
  type: crossunder/threshold_above/threshold_below
  fast: "COLUMN"
  slow: "COLUMN"
```
Column names: SMA_10, EMA_21, RSI_14, BBU_20, BBL_20, ATR_14, MACD."""

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

        yaml_match = re.search(r"```yaml\s*\n(.*?)\n```", content, re.DOTALL)
        yaml_str = yaml_match.group(1) if yaml_match else content

        ir = StrategyIR.from_yaml(yaml_str)
        ir.source_format = fmt
        if metadata.get("name"):
            ir.name = metadata["name"]
        if metadata.get("markets"):
            ir.markets = metadata["markets"]
        return ir


# Auto-register
ParserRegistry.register(MQLParser())
