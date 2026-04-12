"""Webhook parser — TV alerts JSON, TradersPost, PineConnector, Telegram signals.

Parses JSON-format trading signals into Strategy IR.
These signals are typically from TradingView alerts or copy-trade services.

Supported formats:
    {"action": "buy", "ticker": "BTCUSDT", "price": 50000}
    {"action": "sell", "contracts": 1, "ticker": "BTCUSDT"}
    TradingView alert: {{strategy.order.action}} {{ticker}} {{close}}
"""

from __future__ import annotations

import json
import re

from src.research.parsers.base import BaseParser, ParserRegistry
from src.research.strategy_ir import (
    ConditionDef,
    IndicatorDef,
    StrategyIR,
)


class WebhookParser(BaseParser):
    """Parses webhook/alert JSON signals into a simple strategy IR."""

    @property
    def name(self) -> str:
        return "Webhook / Alert Signals"

    @property
    def supported_formats(self) -> list[str]:
        return ["webhook", "tv_alert", "traderspost", "pineconnector"]

    def parse(self, source: str, metadata: dict | None = None) -> StrategyIR:
        """Parse a JSON webhook signal into IR.

        The resulting IR is a simple signal-following strategy.
        """
        source = source.strip()

        # Try to parse as JSON
        try:
            data = json.loads(source)
        except json.JSONDecodeError:
            # Try extracting from TradingView alert format
            data = self._parse_tv_alert_text(source)

        action = str(data.get("action", data.get("order", ""))).lower()
        ticker = str(data.get("ticker", data.get("symbol", metadata.get("symbol", "BTCUSDT") if metadata else "BTCUSDT")))
        timeframe = str(data.get("timeframe", data.get("interval", metadata.get("timeframe", "1h") if metadata else "1h")))

        name = metadata.get("name", f"webhook_{ticker}") if metadata else f"webhook_{ticker}"

        # Map action to entry condition
        entry_long = None
        entry_short = None
        if action in ("buy", "long", "enter_long"):
            entry_long = ConditionDef(type="threshold_above", column="close", threshold=0)
        elif action in ("sell", "short", "enter_short"):
            entry_short = ConditionDef(type="threshold_below", column="close", threshold=float("inf"))

        # Webhooks are typically one-shot signals, use a simple exit
        exit_long = ConditionDef(type="threshold_below", column="close", threshold=0) if entry_long else None
        exit_short = ConditionDef(type="threshold_above", column="close", threshold=float("inf")) if entry_short else None

        ir = StrategyIR(
            name=name,
            source_format="webhook",
            markets=[ticker.upper()],
            timeframe=timeframe,
            description=f"Webhook signal strategy — action: {action}",
            indicators=[IndicatorDef(name="sma", period=20)],
            entry_long=entry_long,
            exit_long=exit_long,
            entry_short=entry_short,
            exit_short=exit_short,
        )

        return ir

    def _parse_tv_alert_text(self, text: str) -> dict:
        """Try to parse TradingView alert text format."""
        data: dict = {}

        # Common patterns
        action_match = re.search(r"(buy|sell|long|short|close)", text, re.IGNORECASE)
        if action_match:
            data["action"] = action_match.group(1).lower()

        ticker_match = re.search(r"([A-Z]{3,10}USDT?|[A-Z]{3,10}/[A-Z]{3,5})", text)
        if ticker_match:
            data["ticker"] = ticker_match.group(1).replace("/", "")

        price_match = re.search(r"(\d+\.?\d*)", text)
        if price_match:
            data["price"] = float(price_match.group(1))

        return data


# Auto-register
ParserRegistry.register(WebhookParser())
