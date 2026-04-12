"""Strategy Intermediate Representation (IR) — the universal format.

Every parser converts TO this IR, one code generator converts FROM it to Python.
This is the linchpin of multi-format ingestion.

The IR is a YAML-serializable schema that captures any strategy's logic:
    - Parameters with types and ranges
    - Indicators with configurable periods
    - Entry/exit conditions
    - Stop loss / take profit rules
"""

from __future__ import annotations

import enum
from typing import Any

import msgspec
import yaml


class ConditionType(str, enum.Enum):
    CROSSOVER = "crossover"          # fast crosses above slow
    CROSSUNDER = "crossunder"        # fast crosses below slow
    THRESHOLD_ABOVE = "threshold_above"  # value > threshold
    THRESHOLD_BELOW = "threshold_below"  # value < threshold
    AND = "and"                      # logical AND of sub-conditions
    OR = "or"                        # logical OR of sub-conditions


class StopType(str, enum.Enum):
    FIXED_PCT = "fixed_pct"          # fixed percentage stop
    ATR_MULT = "atr_mult"            # ATR multiplier stop
    TRAILING_PCT = "trailing_pct"    # trailing stop percentage
    NONE = "none"


class TakeProfitType(str, enum.Enum):
    FIXED_PCT = "fixed_pct"
    RISK_MULTIPLE = "risk_multiple"  # R-multiple of stop loss
    ATR_MULT = "atr_mult"
    NONE = "none"


class ParameterDef(msgspec.Struct):
    """A strategy parameter with type, default, and optimization range."""
    name: str
    type: str = "float"              # int, float, bool
    default: float | int | bool = 0
    min: float | int | None = None
    max: float | int | None = None
    description: str = ""


class IndicatorDef(msgspec.Struct):
    """An indicator requirement."""
    name: str                        # e.g., "sma", "ema", "rsi", "bbands", "atr", "adx", "macd"
    period: int | str = 14           # int or "{param_name}" for parameterized
    column: str = ""                 # output column name (auto-generated if empty)
    source: str = "close"            # input column


class ConditionDef(msgspec.Struct):
    """A trading condition (entry or exit trigger)."""
    type: str = "crossover"          # ConditionType value
    fast: str = ""                   # column or indicator name (for crossover/crossunder)
    slow: str = ""                   # column or indicator name
    column: str = ""                 # column name (for threshold conditions)
    threshold: float = 0.0           # threshold value
    sub_conditions: list[dict] = msgspec.field(default_factory=list)  # for AND/OR composite


class StopDef(msgspec.Struct):
    """Stop loss definition."""
    type: str = "none"               # StopType value
    value: float = 0.0               # percentage or multiplier
    period: int = 14                 # ATR period (if ATR-based)


class TakeProfitDef(msgspec.Struct):
    """Take profit definition."""
    type: str = "none"               # TakeProfitType value
    value: float = 0.0               # percentage, multiplier, or R-multiple


class StrategyIR(msgspec.Struct):
    """The universal intermediate representation for any trading strategy."""
    name: str
    version: str = "1.0"
    source_format: str = "raw_rules"
    markets: list[str] = msgspec.field(default_factory=lambda: ["BTCUSDT"])
    timeframe: str = "1h"
    description: str = ""

    # Parameters
    parameters: list[ParameterDef] = msgspec.field(default_factory=list)

    # Indicators required
    indicators: list[IndicatorDef] = msgspec.field(default_factory=list)

    # Entry conditions
    entry_long: ConditionDef | None = None
    entry_short: ConditionDef | None = None

    # Exit conditions
    exit_long: ConditionDef | None = None
    exit_short: ConditionDef | None = None

    # Risk management
    stop_loss: StopDef = msgspec.field(default_factory=lambda: StopDef(type="none"))
    take_profit: TakeProfitDef = msgspec.field(default_factory=lambda: TakeProfitDef(type="none"))

    # Filters
    filters: list[ConditionDef] = msgspec.field(default_factory=list)

    # Metadata
    cooldown_bars: int = 0
    max_hold_bars: int = 0

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        d = _struct_to_dict(self)
        return yaml.dump(d, default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> StrategyIR:
        """Deserialize from YAML string."""
        d = yaml.safe_load(yaml_str)
        return _dict_to_ir(d)

    @classmethod
    def from_file(cls, path: str) -> StrategyIR:
        """Load from a YAML file."""
        with open(path) as f:
            return cls.from_yaml(f.read())

    def to_file(self, path: str) -> None:
        """Save to a YAML file."""
        with open(path, "w") as f:
            f.write(self.to_yaml())

    def get_indicator_names(self) -> list[str]:
        """Get feature engine indicator names (e.g., ['sma_10', 'rsi_14'])."""
        names = []
        for ind in self.indicators:
            period = ind.period
            if isinstance(period, str) and period.startswith("{"):
                # Resolve parameterized period from defaults
                param_name = period.strip("{}")
                for p in self.parameters:
                    if p.name == param_name:
                        period = int(p.default)
                        break
                else:
                    period = 14  # fallback
            name = ind.name.lower()
            if name == "macd":
                names.append("macd")
            else:
                names.append(f"{name}_{period}")
        return names

    def validate(self) -> list[str]:
        """Validate the IR and return a list of warnings."""
        warnings = []
        if not self.name:
            warnings.append("Strategy name is empty")
        if not self.indicators:
            warnings.append("No indicators defined")
        if self.entry_long is None and self.entry_short is None:
            warnings.append("No entry conditions defined")
        if self.entry_long is not None and self.exit_long is None:
            warnings.append("Long entry defined but no long exit — will rely on stops only")
        if self.entry_short is not None and self.exit_short is None:
            warnings.append("Short entry defined but no short exit — will rely on stops only")
        return warnings


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _struct_to_dict(obj: Any) -> Any:
    """Recursively convert msgspec Struct to dict."""
    if isinstance(obj, msgspec.Struct):
        d = {}
        for field_name in obj.__struct_fields__:
            val = getattr(obj, field_name)
            d[field_name] = _struct_to_dict(val)
        return d
    elif isinstance(obj, list):
        return [_struct_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _struct_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, enum.Enum):
        return obj.value
    return obj


def _dict_to_ir(d: dict) -> StrategyIR:
    """Convert a YAML-loaded dict back into a StrategyIR."""
    params = [ParameterDef(**p) for p in d.get("parameters", [])]
    indicators = [IndicatorDef(**i) for i in d.get("indicators", [])]
    filters = [ConditionDef(**f) for f in d.get("filters", [])]

    entry_long = ConditionDef(**d["entry_long"]) if d.get("entry_long") else None
    entry_short = ConditionDef(**d["entry_short"]) if d.get("entry_short") else None
    exit_long = ConditionDef(**d["exit_long"]) if d.get("exit_long") else None
    exit_short = ConditionDef(**d["exit_short"]) if d.get("exit_short") else None

    sl = StopDef(**d["stop_loss"]) if d.get("stop_loss") else StopDef(type="none")
    tp = TakeProfitDef(**d["take_profit"]) if d.get("take_profit") else TakeProfitDef(type="none")

    return StrategyIR(
        name=d.get("name", ""),
        version=d.get("version", "1.0"),
        source_format=d.get("source_format", "raw_rules"),
        markets=d.get("markets", ["BTCUSDT"]),
        timeframe=d.get("timeframe", "1h"),
        description=d.get("description", ""),
        parameters=params,
        indicators=indicators,
        entry_long=entry_long,
        entry_short=entry_short,
        exit_long=exit_long,
        exit_short=exit_short,
        stop_loss=sl,
        take_profit=tp,
        filters=filters,
        cooldown_bars=d.get("cooldown_bars", 0),
        max_hold_bars=d.get("max_hold_bars", 0),
    )
