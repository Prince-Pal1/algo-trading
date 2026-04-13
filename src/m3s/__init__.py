"""M3S — Master Money Management System.

Capital allocator + compounder + mode manager + regime detector. Sits in
front of the RiskClient in src/main.py: can only *shrink* a signal's
risk_pct and *shift* weights between strategies — never bypass the risk
server, never upscale.

Phase 3b-2, sub-phase 0.1: skeleton + state layer only. Nothing is wired
into main.py yet. Later sub-phases add portfolio tracker, compounder,
allocator, hooks, scheduler, meta-backtest, regime detector, and the
evaluation/Purged-CV layer.

See docs/planning/m3s_plan_v1.md § v1.1 ADDENDUM for the authoritative
spec and sub-phase breakdown.
"""

from __future__ import annotations

from src.m3s.allocator import (
    Allocator,
    build_return_matrix,
    cov_to_corr,
    ledoit_wolf_shrinkage,
)
from src.m3s.compounder import (
    Compounder,
    CompoundTickReason,
    CompoundTickResult,
    TradeCloseEvent,
)
from src.m3s.conviction import ConvictionScore, ConvictionScorer
from src.m3s.edge_decay import (
    EdgeDecayAlert,
    EdgeDecayFlag,
    EdgeDecayMonitor,
    EdgeDecayState,
)
from src.m3s.evaluation import (
    PurgedFold,
    StrategyEvaluation,
    bayesian_fractional_kelly,
    evaluate_strategy,
    purged_kfold_splits,
)
from src.m3s.hooks import M3S, SignalDecision
from src.m3s.meta_backtest import (
    MetaBacktestResult,
    MetaTrade,
    compare_baselines,
    run_fixed_weight_baseline,
    run_m3s_simulation,
)
from src.m3s.modes import (
    MODE_PRESETS,
    CustomModeValidationError,
    M3SMode,
    ModeConfig,
    load_mode_from_dict,
)
from src.m3s.portfolio import PortfolioTracker
from src.m3s.regime import (
    AutoModeSwitcher,
    ModeTransition,
    Regime,
    RegimeClassification,
    RegimeClassifier,
    RegimeInputs,
)
from src.m3s.scheduler import (
    M3SScheduler,
    load_state,
    make_scheduler,
    save_state,
)
from src.m3s.state import M3SStore
from src.m3s.types import (
    AllocationDecision,
    CompoundState,
    M3SEventType,
    PortfolioSnapshot,
    StrategySnapshot,
)

__all__ = [
    # Types
    "PortfolioSnapshot",
    "StrategySnapshot",
    "AllocationDecision",
    "CompoundState",
    "M3SEventType",
    # Modes
    "M3SMode",
    "ModeConfig",
    "MODE_PRESETS",
    "load_mode_from_dict",
    "CustomModeValidationError",
    # Portfolio tracker
    "PortfolioTracker",
    # Edge-decay monitor
    "EdgeDecayMonitor",
    "EdgeDecayState",
    "EdgeDecayAlert",
    "EdgeDecayFlag",
    # Compounder
    "Compounder",
    "CompoundTickReason",
    "CompoundTickResult",
    "TradeCloseEvent",
    # Allocator
    "Allocator",
    "ledoit_wolf_shrinkage",
    "cov_to_corr",
    "build_return_matrix",
    # Conviction scorer (Tier 1 #3)
    "ConvictionScorer",
    "ConvictionScore",
    # Hooks facade
    "M3S",
    "SignalDecision",
    # Scheduler + persistence
    "M3SScheduler",
    "make_scheduler",
    "save_state",
    "load_state",
    # Meta-backtest
    "MetaBacktestResult",
    "MetaTrade",
    "run_fixed_weight_baseline",
    "run_m3s_simulation",
    "compare_baselines",
    # Regime detector (Tier 1 #1)
    "Regime",
    "RegimeInputs",
    "RegimeClassification",
    "RegimeClassifier",
    "AutoModeSwitcher",
    "ModeTransition",
    # Evaluation (Tier 1 #6 + #7)
    "PurgedFold",
    "StrategyEvaluation",
    "purged_kfold_splits",
    "bayesian_fractional_kelly",
    "evaluate_strategy",
    # State
    "M3SStore",
]
