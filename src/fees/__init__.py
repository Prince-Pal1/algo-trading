"""Fee management subsystem.

Provides a unified, broker-aware, scenario-aware cost model that every
part of the system (strategies, backtest engine, risk manager, M3S,
dashboard) queries on demand.

Public API:

    # Broker registry + active pointer
    from src.fees import get_broker, list_brokers, get_active_broker

    # Strategy / engine / risk consumers
    from src.fees import FeeManager

    fees = FeeManager.resolve(symbol="XAUUSD", style="swing")          # live-mode
    fees = FeeManager.resolve(symbol="XAUUSD", style="swing",
                              scenario="news_active")                   # forced scenario
    cost = FeeManager.project_cost(symbol="XAUUSD", qty_lots=1.0,
                                   style="swing", hold_hours=48)        # pre-trade

The two-axis model:
    Style (strategy declares): scalping / intraday / swing / position / arbitrage
    Scenario (system detects): normal / news_active / illiquid / volatile

Each (broker, instrument_class, scenario) pair has a FeeProfile; the
style modulates how much of the total cost is spread vs swap vs commission.

See docs/FEE_SYSTEM.md for the full architecture.
"""

from src.fees.broker import (
    Broker,
    BrokerInstrumentProfile,
    get_broker,
    list_brokers,
    load_all_brokers,
    reload_brokers,
)
from src.fees.active import (
    get_active_broker,
    get_active_broker_id,
    get_active_broker_for_instrument_class,
    set_active_broker,
)
from src.fees.helpers import (
    cost_for_signal,
    cost_for_strategy_signal,
    explain_for_signal,
)
from src.fees.manager import CostProjection, FeeManager
from src.fees.scenario import ScenarioContext, ScenarioName, detect, detect_simple

__all__ = [
    "Broker",
    "BrokerInstrumentProfile",
    "CostProjection",
    "FeeManager",
    "ScenarioContext",
    "ScenarioName",
    "cost_for_signal",
    "cost_for_strategy_signal",
    "explain_for_signal",
    "detect",
    "detect_simple",
    "get_broker",
    "list_brokers",
    "load_all_brokers",
    "reload_brokers",
    "get_active_broker",
    "get_active_broker_id",
    "get_active_broker_for_instrument_class",
    "set_active_broker",
]
