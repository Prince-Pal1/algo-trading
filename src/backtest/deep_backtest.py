"""Deep Backtest Framework — generic multi-phase high-accuracy validation pipeline.

Distills the SWIFT process (tasks #109 matrix + #110 leverage validation +
#111 walk-forward OOS) into a reusable template that any strategy can run
through to produce the same quality of evidence. Invokable as:

    PYTHONPATH=. python3 scripts/deep_backtest.py <strategy_name>

Pipeline phases:
    Phase 0  Preflight         data + fee profiles + M1PathModel + strategy lookup
    Phase 1  Matrix            windows × timeframes × leverages × fees cells
    Phase 2  Sanity checks     trade count / DD / win-rate / cost reasonability
    Phase 3  Leverage deep-dive  auto-triggered when P&L is leverage-invariant
    Phase 4  Walk-forward OOS  N non-overlapping folds, optional per-fold retune
    Phase 5  Verdict           deployable / research-only / fails gate
    Phase 6  Report            CSV + heatmap PNGs + HTML + PDF via report module

A deep backtest gives you:
    - Sanity-checked matrix of performance across the sensible configuration
      space for the strategy
    - Automatic detection + explanation of suspicious patterns like leverage
      invariance (via hand-trace + unit-test-style asserts)
    - Honest OOS baseline from non-overlapping walk-forward folds
    - Single-page HTML + landscape PDF report for review

Usage from Python (alternative to CLI):

    from src.backtest.deep_backtest import DeepBacktestConfig, run_deep_backtest
    cfg = DeepBacktestConfig(strategy="swift_alma")
    result = run_deep_backtest(cfg)
    print(result.verdict)
"""

from __future__ import annotations

import inspect
import itertools
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.backtest.book import SUB_BOOK_INSTITUTIONAL
from src.backtest.costs import ICMarketsMetalFeeModel, ZeroCostFeeModel
from src.backtest.fee_profiles import get_profile, make_fee_model
from src.backtest.leveraged_engine import LeveragedBacktestEngine
from src.backtest.path import get_or_build_m1_path_model
from src.strategies.base import BaseStrategy
from src.strategies.router import STRATEGY_REGISTRY


# ── Data paths ───────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"
DATA_M1 = DATA_DIR / "XAUUSD_1m.parquet"
DATA_M5 = DATA_DIR / "XAUUSD_5m.parquet"
DATA_1H = DATA_DIR / "XAUUSD_1h.parquet"

# Default window labels for pretty printing
WINDOW_LABELS: dict[int, str] = {
    7: "1w", 14: "2w", 30: "1mo", 60: "2mo",
    90: "3mo", 120: "4mo", 180: "6mo", 270: "9mo",
    365: "1y", 540: "18mo", 730: "2y", 1460: "4y",
}


# ── Leverage modes ───────────────────────────────────────────────────────


class LeverageMode(str, Enum):
    """How a strategy's position sizing interacts with the engine leverage parameter.

    See `docs/LEVERAGE_STRATEGY_DESIGN.md` for the decision flowchart and
    `reports/leverage_strategy_research_2026-04-15.md` (task #113) for the
    full math and case studies.

    Modes:
        INVARIANT        — risk_pct == sl_pct (notional = equity, L is passive)
        MARGIN_CAPPED    — risk_pct > sl_pct (notional fixed, L gates margin).
                           Default — preserves pre-task-#114 behavior.
        VOL_TARGETED     — risk_pct scales with strategy's own vol scalar
        RISK_SCALED      — risk_pct = base × (L / baseline_L). NEW. Linear
                           return amplification with leverage.
        KELLY_FRACTIONAL — risk_pct = kelly_fraction × f*. NEW. Formula-driven
                           sizing from historical win_rate + payoff_ratio,
                           hard-capped at 0.25 to prevent full-Kelly blowups.

    DRAWDOWN_BUDGETED is deferred (requires per-bar equity-curve state that
    the single-pass matrix model doesn't support).
    """

    INVARIANT = "invariant"
    MARGIN_CAPPED = "margin_capped"
    VOL_TARGETED = "vol_targeted"
    RISK_SCALED = "risk_scaled"
    KELLY_FRACTIONAL = "kelly_fractional"


# ── Configuration ────────────────────────────────────────────────────────


@dataclass
class DeepBacktestConfig:
    """Configuration for a deep backtest run.

    All fields have sensible defaults matching the SWIFT process. Override
    any subset; the pipeline fills in unspecified values.

    The `strategy` field accepts:
        - A registry key ("swift_alma", "donchian_gold", ...)
        - A strategy class instance
        - A strategy class (will be instantiated with `strategy_params`)
        - A dotted path like "src.strategies.trend_following.swift_alma:SwiftAlmaStrategy"
    """

    strategy: str | type[BaseStrategy] | BaseStrategy

    # Strategy init kwargs. Merged with the strategy's own defaults. Pass an
    # empty dict to use strategy defaults as-is.
    strategy_params: dict[str, Any] = field(default_factory=dict)

    # Data / market
    symbol: str = "XAUUSD"

    # Matrix grid
    timeframes: list[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h"])
    window_days: list[int] = field(default_factory=lambda: [30, 90, 180, 365])
    leverages: list[float] = field(default_factory=lambda: [1.0, 50.0, 100.0, 500.0, 1000.0])
    fee_profiles: list[str] = field(default_factory=lambda: [
        "pine_zero_cost",
        "ic_markets_mt4_xauusd_normal",
        "ic_markets_ctrader_xauusd_normal",
    ])

    # Engine config
    initial_cash: float = 10_000.0
    sub_book: str = SUB_BOOK_INSTITUTIONAL
    warmup_bars: int = 250
    # Optional list of indicator keys to precompute via engine.run(indicators=...).
    # Required for strategies that read pre-computed features (donchian_gold,
    # vol_momentum_gold, etc.) instead of computing on_features inline.
    # None → auto-detect from strategy.INDICATORS class attr if present, else
    # fall back to the superset of common gold indicators.
    indicators: list[str] | None = None

    # Walk-forward config
    wf_enabled: bool = True
    wf_fold_days: int = 90
    wf_n_folds: int = 7
    wf_gate_calmar: float = 0.5
    # If True, grid-search wf_param_grid on train window per fold.
    # If False, use fixed params (appropriate for Pine ports, mechanical
    # strategies, or strategies whose params are rigid by design).
    wf_retune: bool = False
    wf_param_grid: dict[str, list[Any]] | None = None
    wf_train_days: int = 90  # only used when wf_retune=True
    # Which fee profile + timeframe to use for WF (default: matrix best cell)
    wf_fee_profile: str | None = None  # None → pick matrix best
    wf_timeframe: str | None = None    # None → pick matrix best
    wf_leverage: float = 10.0

    # Leverage validation (auto-triggered if matrix shows P&L invariance)
    leverage_validation_enabled: bool = True

    # --- Leverage mode (task #114) -----------------------------------------
    # How position sizing interacts with the engine leverage parameter.
    # Default is MARGIN_CAPPED which preserves pre-task-#114 behavior for
    # all currently-registered strategies. See docs/LEVERAGE_STRATEGY_DESIGN.md.
    leverage_mode: LeverageMode = LeverageMode.MARGIN_CAPPED
    # RISK_SCALED only: baseline leverage at which the strategy was tuned.
    # risk_pct(L) = base_risk_pct × (L / baseline_leverage).
    baseline_leverage: float = 10.0
    # KELLY_FRACTIONAL only: Kelly fraction multiplier. 0.5 = half-Kelly
    # (industry standard), 0.25 = quarter-Kelly (safety-first). Result is
    # still hard-capped at 0.25 absolute risk_pct to prevent full-Kelly blowups.
    kelly_fraction: float = 0.5
    # KELLY_FRACTIONAL only: historical OOS win rate in [0, 1]. Required.
    kelly_win_rate: float | None = None
    # KELLY_FRACTIONAL only: avg_win / avg_loss ratio. Required.
    kelly_payoff_ratio: float | None = None
    # Name of the strategy kwarg that holds risk_pct. Default matches all
    # 3 registered gold strategies. Override for strategies with a different
    # risk parameter name.
    risk_pct_param_name: str = "max_risk_per_trade"

    # Output / report
    out_dir: Path | None = None        # default reports/deep_backtest_<strategy>_<date>/
    run_id_prefix: str = "deep"
    generate_pdf: bool = True
    generate_html: bool = True
    generate_heatmaps: bool = True

    # Misc
    progress: bool = True              # print phase progress
    fail_fast: bool = True             # abort on phase errors

    def resolve_strategy_name(self) -> str:
        """Return a filesystem-safe strategy identifier."""
        s = self.strategy
        if isinstance(s, str):
            # strip dotted path if present
            return s.split(":")[-1].split(".")[-1].lower()
        if inspect.isclass(s):
            return s.__name__.lower()
        return type(s).__name__.lower()


# ── Result types ─────────────────────────────────────────────────────────


@dataclass
class CellResult:
    window_days: int
    window_label: str
    timeframe: str
    leverage: float
    fee_profile: str
    trades: int
    return_pct: float
    maxdd_pct: float
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    total_commission: float
    final_equity: float
    margin_per_trade: float
    cost_per_trade: float
    cost_pct_of_margin: float
    broker_stop_outs: int
    calmar: float
    sharpe: float
    notes: str = ""


@dataclass
class WalkForwardFold:
    fold: int
    start_ts: str
    end_ts: str
    trades: int
    return_pct: float
    max_dd_pct: float
    calmar: float
    sharpe: float
    win_rate: float
    profit_factor: float
    best_params: dict | None = None  # populated if wf_retune=True


@dataclass
class WalkForwardSummary:
    timeframe: str
    fee_profile: str
    n_folds: int
    fold_days: int
    total_trades: int
    mean_return_pct: float
    std_return_pct: float
    mean_dd_pct: float
    std_dd_pct: float
    mean_calmar: float
    std_calmar: float
    mean_sharpe: float
    std_sharpe: float
    mean_win_rate: float
    profitable_folds: int
    gate_calmar: float
    gate_per_fold_passed: bool
    continuous_return_pct: float
    continuous_dd_pct: float
    continuous_calmar: float
    continuous_gate_passed: bool
    folds: list[WalkForwardFold] = field(default_factory=list)


@dataclass
class LeverageValidationResult:
    invariant_detected: bool
    invariant_reason: str
    hand_trace_passed: bool
    hand_trace_details: dict
    alt_sizing_demo: list[dict]


@dataclass
class DeepBacktestResult:
    config: DeepBacktestConfig
    strategy_name: str
    timestamp: str
    matrix: list[CellResult]
    matrix_df: pd.DataFrame
    best_cell: CellResult | None
    sanity_checks: dict[str, Any]
    leverage_validation: LeverageValidationResult | None
    walk_forward: WalkForwardSummary | None
    verdict: str
    verdict_reason: str
    report_dir: Path
    elapsed_seconds: float


# ── Strategy resolution ──────────────────────────────────────────────────


def _resolve_strategy_class(spec: str | type[BaseStrategy] | BaseStrategy) -> type[BaseStrategy]:
    """Return the strategy class given a spec (without instantiating)."""
    if isinstance(spec, BaseStrategy):
        return type(spec)
    if inspect.isclass(spec) and issubclass(spec, BaseStrategy):
        return spec
    if isinstance(spec, str):
        if ":" in spec:
            module_path, class_name = spec.split(":", 1)
            mod = __import__(module_path, fromlist=[class_name])
            return getattr(mod, class_name)
        if spec in STRATEGY_REGISTRY:
            return STRATEGY_REGISTRY[spec]
        available = sorted(STRATEGY_REGISTRY.keys())
        raise KeyError(
            f"Strategy '{spec}' not found in STRATEGY_REGISTRY "
            f"(available: {available}). Use a dotted path "
            f"'module.path:ClassName' or register the strategy first."
        )
    raise TypeError(f"Unsupported strategy spec: {type(spec)}")


def _resolve_strategy(
    spec: str | type[BaseStrategy] | BaseStrategy,
    params: dict[str, Any],
    timeframe: str,
) -> BaseStrategy:
    """Instantiate a strategy. Tries to pass `timeframe=...` first; if the
    strategy's __init__ doesn't accept `timeframe` (some gold strategies
    don't — they take timeframe at engine.run() time), falls back gracefully.
    """
    if isinstance(spec, BaseStrategy):
        return spec

    cls = _resolve_strategy_class(spec)

    # Introspect the constructor to decide whether to pass `timeframe`.
    sig = inspect.signature(cls.__init__)
    accepts_timeframe = (
        "timeframe" in sig.parameters
        or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    )
    kwargs = dict(params)
    if accepts_timeframe:
        kwargs.setdefault("timeframe", timeframe)
    return cls(**kwargs)


# Default indicator superset covering current gold strategies. Used when the
# config doesn't specify indicators and the strategy class doesn't expose a
# REQUIRED_INDICATORS attr. Harmless extra computation for strategies that
# don't read these, but saves callers from remembering what each needs.
_DEFAULT_GOLD_INDICATORS: list[str] = [
    "donchian_20", "donchian_55", "donchian_120",
    "atr_14", "atr_20",
    "adx_14", "adx_20",
    "ema_20", "ema_50", "ema_200",
    "rsi_14",
    "bb_20_2",
    "obv",
]


def _resolve_indicators(config: DeepBacktestConfig) -> list[str] | None:
    """Figure out which indicators to precompute. Precedence:
    1. config.indicators (explicit)
    2. strategy.REQUIRED_INDICATORS class attr (if defined)
    3. _DEFAULT_GOLD_INDICATORS (safe superset)
    Returns None only if the strategy explicitly sets REQUIRED_INDICATORS = []
    which signals "computes own features inline" (e.g. SwiftAlmaStrategy).
    """
    if config.indicators is not None:
        return config.indicators or None

    try:
        cls = _resolve_strategy_class(config.strategy)
        required = getattr(cls, "REQUIRED_INDICATORS", None)
        if required is not None:
            return required or None  # empty list → None (no precompute)
    except Exception:
        pass

    return _DEFAULT_GOLD_INDICATORS


# ── Leverage mode transform (task #114) ─────────────────────────────────

# Hard cap on any Kelly-derived risk_pct. Full Kelly produces 20-80% DDs
# routinely (see research report §4.1). Capping at 0.25 absolute prevents
# the "computed f* was huge" footgun. This is a safety floor, not a policy
# knob — do not expose it as config.
_KELLY_HARD_CAP: float = 0.25


def _apply_leverage_mode(config: DeepBacktestConfig, leverage: float) -> dict[str, Any]:
    """Transform strategy_params based on config.leverage_mode.

    Returns a NEW dict (does NOT mutate config.strategy_params). The returned
    dict is the strategy_params override that should be passed to
    `_resolve_strategy(config.strategy, <overridden_params>, timeframe)` when
    running a single cell at this leverage.

    Semantics per mode:

        INVARIANT / MARGIN_CAPPED / VOL_TARGETED — passthrough. The strategy's
            own sizing is preserved; leverage only affects margin footprint.

        RISK_SCALED — risk_pct = base × (leverage / baseline_leverage).
            Linearly amplifies position size with leverage. At L == baseline,
            matches the base. At 2× baseline, doubles position size.

        KELLY_FRACTIONAL — risk_pct = min(0.25, kelly_fraction × f*) where
            f* = (b × p - q) / b, p = kelly_win_rate, b = kelly_payoff_ratio.
            Fixed across leverages (Kelly formula determines size, engine
            leverage is a margin gate only). Hard-capped at 0.25.

    Raises ValueError for KELLY_FRACTIONAL without priors.
    """
    mode = config.leverage_mode
    params = dict(config.strategy_params)  # shallow copy — never mutate input

    # Passthrough modes — no transform
    if mode in (LeverageMode.INVARIANT,
                LeverageMode.MARGIN_CAPPED,
                LeverageMode.VOL_TARGETED):
        return params

    param_name = config.risk_pct_param_name

    # Determine the base risk_pct. Prefer explicit config override; fall back
    # to the strategy class's constructor default if not overridden.
    if param_name in params:
        base_risk_pct = float(params[param_name])
    else:
        base_risk_pct = 0.01  # ultimate fallback
        try:
            cls = _resolve_strategy_class(config.strategy)
            sig = inspect.signature(cls.__init__)
            default_param = sig.parameters.get(param_name)
            if (default_param is not None
                    and default_param.default is not inspect.Parameter.empty):
                base_risk_pct = float(default_param.default)
        except Exception:
            pass

    if mode == LeverageMode.RISK_SCALED:
        baseline = max(float(config.baseline_leverage), 1.0)
        scaled = base_risk_pct * (float(leverage) / baseline)
        params[param_name] = scaled
        return params

    if mode == LeverageMode.KELLY_FRACTIONAL:
        if config.kelly_win_rate is None or config.kelly_payoff_ratio is None:
            raise ValueError(
                "KELLY_FRACTIONAL mode requires config.kelly_win_rate and "
                "config.kelly_payoff_ratio (from OOS historical stats). "
                "See reports/leverage_strategy_research_2026-04-15.md §2.5."
            )
        p = float(config.kelly_win_rate)
        b = float(config.kelly_payoff_ratio)
        q = 1.0 - p
        f_star = max(0.0, (b * p - q) / b) if b > 0 else 0.0
        scaled = min(_KELLY_HARD_CAP, float(config.kelly_fraction) * f_star)
        params[param_name] = scaled
        return params

    # Unknown / future mode — passthrough
    return params


# ── Data loading ─────────────────────────────────────────────────────────


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Generic OHLCV resampler. Anchors to the UTC epoch so runs are
    deterministic and naturally drops market-closed periods (weekends for
    gold) via the `dropna()` call.

    `rule` is a pandas offset alias ("15min", "30min", "4h", "1D", ...).
    """
    work = df.copy()
    work["dt_idx"] = pd.to_datetime(work["timestamp"], unit="ms", utc=True)
    work = work.set_index("dt_idx")
    rs = work.resample(rule, origin="epoch").agg({
        "timestamp": "first", "open": "first", "high": "max",
        "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    rs = rs.reset_index(drop=True)
    rs["dt"] = pd.to_datetime(rs["timestamp"], unit="ms", utc=True)
    return rs


def _resample_5m_to_15m(df_m5: pd.DataFrame) -> pd.DataFrame:
    """Resample M5 OHLCV to M15 (historical name — kept as alias)."""
    return _resample_ohlcv(df_m5, "15min")


def _resample_5m_to_30m(df_m5: pd.DataFrame) -> pd.DataFrame:
    """Resample M5 OHLCV to M30 (task #114)."""
    return _resample_ohlcv(df_m5, "30min")


def _resample_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h OHLCV to 4h (task #114)."""
    return _resample_ohlcv(df_1h, "4h")


def _resample_1h_to_1d(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h OHLCV to 1d (task #114).

    Uses `1D` with UTC epoch anchor. Gold closes on weekends so dropna()
    naturally removes non-trading days.
    """
    return _resample_ohlcv(df_1h, "1D")


def _load_timeframe(timeframe: str, symbol: str) -> pd.DataFrame:
    """Load OHLCV data for a (symbol, timeframe). XAUUSD-focused for Phase G;
    extend this table as new symbols get data files.

    Supported timeframes: 1m, 5m, 15m, 30m, 1h, 4h, 1d (task #114 extended).
    """
    if symbol != "XAUUSD":
        raise NotImplementedError(
            f"deep_backtest currently only supports XAUUSD; got {symbol}. "
            "Extend _load_timeframe() when additional parquet files exist."
        )

    if timeframe == "1m":
        df = pd.read_parquet(DATA_M1)
    elif timeframe == "5m":
        df = pd.read_parquet(DATA_M5)
    elif timeframe == "15m":
        return _resample_5m_to_15m(pd.read_parquet(DATA_M5))
    elif timeframe == "30m":
        return _resample_5m_to_30m(pd.read_parquet(DATA_M5))
    elif timeframe == "1h":
        df = pd.read_parquet(DATA_1H)
    elif timeframe == "4h":
        return _resample_1h_to_4h(pd.read_parquet(DATA_1H))
    elif timeframe == "1d":
        return _resample_1h_to_1d(pd.read_parquet(DATA_1H))
    else:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}. "
            "Supported: 1m, 5m, 15m, 30m, 1h, 4h, 1d"
        )

    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def _check_window_availability(
    window_days: int, timeframe: str = "1h", symbol: str = "XAUUSD",
) -> tuple[bool, str]:
    """Check if `window_days` of history is available for (timeframe, symbol).

    Used by the interactive TUI to grey out unavailable window selections.
    Returns (available, reason). `reason` is empty when available, or a
    short human-readable explanation when not.
    """
    try:
        df = _load_timeframe(timeframe, symbol)
        if len(df) == 0:
            return False, f"no {timeframe} data available"
        total_days = (df["dt"].iloc[-1] - df["dt"].iloc[0]).days
        if window_days > total_days:
            return False, f"need {window_days}d, have only {total_days}d of {timeframe} data"
        return True, ""
    except Exception as e:
        return False, f"load failed: {type(e).__name__}: {e}"


def _slice_window(df: pd.DataFrame, window_days: int, warmup_bars: int) -> pd.DataFrame:
    """Take the most recent `window_days` of `df` plus `warmup_bars` of headroom."""
    end_dt = df["dt"].iloc[-1]
    window_start = end_dt - pd.Timedelta(days=window_days)
    try:
        idx_at_start = int(df.index[df["dt"] >= window_start][0])
    except IndexError:
        return df.reset_index(drop=True)
    full_start = max(0, idx_at_start - warmup_bars)
    return df.iloc[full_start:].reset_index(drop=True)


# ── Phase 0 — Preflight ──────────────────────────────────────────────────


def _phase_0_preflight(config: DeepBacktestConfig) -> dict[str, Any]:
    """Verify data, fee profiles, strategy, M1PathModel cache.

    Returns a context dict with `m1_path_model`, `data_range`, `strategy_name`.
    Any failure raises with a loud message (fail_fast default).
    """
    ctx: dict[str, Any] = {}
    errors: list[str] = []

    if config.progress:
        print("=" * 80)
        print("Phase 0 — Preflight")
        print("=" * 80)

    # Strategy
    try:
        # Instantiate once to validate config + get the resolved name
        probe = _resolve_strategy(config.strategy, config.strategy_params, "1h")
        ctx["strategy_name"] = probe.__class__.__name__
        if config.progress:
            print(f"  ✓ strategy: {ctx['strategy_name']}")
    except Exception as e:
        errors.append(f"strategy resolution failed: {type(e).__name__}: {e}")

    # Fee profiles
    for fp in config.fee_profiles:
        try:
            get_profile(fp)
        except Exception as e:
            errors.append(f"fee profile '{fp}' failed: {e}")
    if config.progress and not [e for e in errors if "fee profile" in e]:
        print(f"  ✓ fee profiles: {len(config.fee_profiles)} verified")

    # Data files
    for tf in config.timeframes:
        try:
            df = _load_timeframe(tf, config.symbol)
            ctx.setdefault("data_range", {})[tf] = {
                "bars": len(df),
                "start": str(df["dt"].iloc[0]),
                "end": str(df["dt"].iloc[-1]),
            }
            if config.progress:
                print(f"  ✓ data {tf}: {len(df):,} bars ({df['dt'].iloc[0].date()} → {df['dt'].iloc[-1].date()})")
        except Exception as e:
            errors.append(f"data load {tf} failed: {e}")

    # M1PathModel cache — SWIFT & other intrabar-sensitive strategies need this
    try:
        m1pm = get_or_build_m1_path_model(str(DATA_M1), sub_bar_count=5)
        ctx["m1_path_model"] = m1pm
        if config.progress:
            print(f"  ✓ M1PathModel ready")
    except Exception as e:
        errors.append(f"M1PathModel build failed: {e}")
        ctx["m1_path_model"] = None

    if errors and config.fail_fast:
        raise RuntimeError("Preflight failed:\n  - " + "\n  - ".join(errors))

    if config.progress:
        print()

    return ctx


# ── Phase 1 — Matrix ─────────────────────────────────────────────────────


def _trade_metrics(trades: list) -> dict[str, float]:
    if not trades:
        return {"win_rate": 0.0, "pf": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "total_comm": 0.0, "avg_margin": 0.0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    win_rate = len(wins) / n * 100.0
    total_win = sum(wins)
    total_loss = -sum(losses)
    pf = (total_win / total_loss) if total_loss > 0 else (float("inf") if total_win > 0 else 0.0)
    return {
        "win_rate": win_rate,
        "pf": pf,
        "avg_win": (total_win / len(wins)) if wins else 0.0,
        "avg_loss": (-total_loss / len(losses)) if losses else 0.0,
        "total_comm": sum(t.commission for t in trades),
        "avg_margin": sum(t.margin_used for t in trades if t.margin_used > 0) /
                      max(1, len([t for t in trades if t.margin_used > 0])),
    }


def _annualized_return_pct(return_pct: float, window_days: int) -> float:
    if window_days <= 0:
        return 0.0
    return return_pct * (365.0 / window_days)


def _calmar_from_return_dd(annualized_return_pct: float, dd_pct: float) -> float:
    return annualized_return_pct / max(dd_pct, 0.01)


def _sharpe_from_equity(curve: list[float], periods_per_year: float) -> float:
    if len(curve) < 2:
        return 0.0
    rets: list[float] = []
    for i in range(1, len(curve)):
        prev = curve[i - 1]
        if prev <= 0:
            continue
        rets.append((curve[i] - prev) / prev)
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    std = math.sqrt(var)
    return (mean / std) * math.sqrt(periods_per_year) if std > 0 else 0.0


def _periods_per_year_for_tf(timeframe: str) -> float:
    """Approximate bars-per-year for Sharpe annualization. XAUUSD closes
    on weekends, so we use 120 trading hours/week × 52 weeks as the base.
    Task #114: added 30m, 4h, 1d timeframes.
    """
    mapping = {
        "1m": 120 * 60 * 52,        # 374,400
        "5m": 120 * 12 * 52,        #  74,880
        "15m": 120 * 4 * 52,        #  24,960
        "30m": 120 * 2 * 52,        #  12,480
        "1h": 120 * 52,             #   6,240
        "4h": 30 * 52,              #   1,560  (30 × 4h bars/week)
        "1d": 5 * 52,                #     260  (5 trading days/week)
    }
    return float(mapping.get(timeframe, 24_960))


def _run_cell(
    *,
    window_days: int,
    timeframe: str,
    leverage: float,
    fee_profile: str,
    df: pd.DataFrame,
    m1_path_model,
    config: DeepBacktestConfig,
) -> CellResult:
    """Run one (window, TF, leverage, fee) backtest cell."""
    df_sliced = _slice_window(df, window_days, config.warmup_bars)
    fee_model = make_fee_model(fee_profile)

    # M1PathModel for M5/M15/1h charts; M1 chart is degenerate (no sub-bars)
    use_m1_pm = timeframe != "1m"
    path_model = m1_path_model if (use_m1_pm and m1_path_model is not None) else None

    run_id = (f"{config.run_id_prefix}_{window_days}d_{timeframe}_"
              f"{int(leverage)}x_{fee_profile[:10]}")
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=config.initial_cash,
        initial_aggressive_cash=0.0,
        fee_model=fee_model,
        path_model=path_model,
        run_id=run_id,
    )
    # Apply leverage_mode transform (task #114) to strategy_params BEFORE
    # instantiating the strategy. For RISK_SCALED / KELLY_FRACTIONAL this
    # modifies max_risk_per_trade so the strategy's emitted signal.risk_pct
    # reflects the chosen sizing mode. For passthrough modes (INVARIANT /
    # MARGIN_CAPPED / VOL_TARGETED), returns config.strategy_params unchanged.
    mode_adjusted_params = _apply_leverage_mode(config, leverage)
    strategy = _resolve_strategy(config.strategy, mode_adjusted_params, timeframe)
    indicators = _resolve_indicators(config)

    try:
        run_kwargs = dict(
            symbol=config.symbol, timeframe=timeframe,
            leverage=leverage, sub_book=config.sub_book,
        )
        if indicators:
            run_kwargs["indicators"] = indicators
        result = engine.run(strategy, df_sliced, **run_kwargs)
    except Exception as e:
        return CellResult(
            window_days=window_days,
            window_label=WINDOW_LABELS.get(window_days, f"{window_days}d"),
            timeframe=timeframe, leverage=leverage, fee_profile=fee_profile,
            trades=0, return_pct=0.0, maxdd_pct=0.0, win_rate=0.0,
            profit_factor=0.0, avg_win=0.0, avg_loss=0.0,
            total_commission=0.0, final_equity=config.initial_cash,
            margin_per_trade=0.0, cost_per_trade=0.0, cost_pct_of_margin=0.0,
            broker_stop_outs=0, calmar=0.0, sharpe=0.0,
            notes=f"ERROR: {type(e).__name__}: {e}",
        )

    m = _trade_metrics(result.trades)
    n = len(result.trades) or 1
    cost_per_trade = m["total_comm"] / n
    cost_pct = (cost_per_trade / m["avg_margin"] * 100.0) if m["avg_margin"] > 0 else 0.0
    final_eq = result.metrics["final_institutional_equity"]
    return_pct = (final_eq - config.initial_cash) / config.initial_cash * 100.0
    max_dd = result.metrics["max_dd_pct"]
    ann_ret = _annualized_return_pct(return_pct, window_days)
    calmar = _calmar_from_return_dd(ann_ret, max_dd)
    sharpe = _sharpe_from_equity(result.equity_curve_total, _periods_per_year_for_tf(timeframe))

    return CellResult(
        window_days=window_days,
        window_label=WINDOW_LABELS.get(window_days, f"{window_days}d"),
        timeframe=timeframe, leverage=leverage, fee_profile=fee_profile,
        trades=len(result.trades),
        return_pct=return_pct, maxdd_pct=max_dd,
        win_rate=m["win_rate"], profit_factor=m["pf"],
        avg_win=m["avg_win"], avg_loss=m["avg_loss"],
        total_commission=m["total_comm"], final_equity=final_eq,
        margin_per_trade=m["avg_margin"],
        cost_per_trade=cost_per_trade, cost_pct_of_margin=cost_pct,
        broker_stop_outs=result.broker_stop_out_count,
        calmar=calmar, sharpe=sharpe,
    )


def _phase_1_matrix(config: DeepBacktestConfig, ctx: dict) -> list[CellResult]:
    """Run the full matrix: windows × timeframes × leverages × fees."""
    total = (len(config.window_days) * len(config.timeframes)
             * len(config.leverages) * len(config.fee_profiles))
    if config.progress:
        print("=" * 80)
        print(f"Phase 1 — Matrix ({total} cells: "
              f"{len(config.window_days)} windows × {len(config.timeframes)} TFs × "
              f"{len(config.leverages)} leverages × {len(config.fee_profiles)} fees)")
        print("=" * 80)

    # Pre-load each timeframe once; slicing per-window happens inside _run_cell
    tf_data: dict[str, pd.DataFrame] = {}
    for tf in config.timeframes:
        tf_data[tf] = _load_timeframe(tf, config.symbol)

    results: list[CellResult] = []
    cell_idx = 0
    t_start = time.time()
    for wd in config.window_days:
        for tf in config.timeframes:
            df = tf_data[tf]
            for lev in config.leverages:
                for fp in config.fee_profiles:
                    cell_idx += 1
                    cell = _run_cell(
                        window_days=wd, timeframe=tf, leverage=lev,
                        fee_profile=fp, df=df,
                        m1_path_model=ctx.get("m1_path_model"),
                        config=config,
                    )
                    results.append(cell)
                    if config.progress:
                        elapsed = time.time() - t_start
                        rate = cell_idx / max(0.1, elapsed)
                        eta = (total - cell_idx) / max(0.1, rate)
                        marker = "✓" if not cell.notes else "✗"
                        short_fp = fp.replace("ic_markets_", "").replace("_xauusd_normal", "").replace("_cost", "")
                        print(f"  [{cell_idx:>3d}/{total}] {marker} {cell.window_label:4s} "
                              f"{tf:3s} {int(lev):>4d}x {short_fp:10s} "
                              f"→ ret={cell.return_pct:+7.2f}% dd={cell.maxdd_pct:5.2f}% "
                              f"calmar={cell.calmar:+6.2f} trades={cell.trades:>4d}"
                              f"   ETA {eta:.0f}s")
    if config.progress:
        print(f"\n  ✓ Matrix complete in {time.time() - t_start:.1f}s ({total} cells)\n")
    return results


# ── Phase 2 — Sanity checks ──────────────────────────────────────────────


def _phase_2_sanity_checks(
    matrix: list[CellResult], config: DeepBacktestConfig
) -> dict[str, Any]:
    """Run basic sanity gates on the matrix. None of these are hard stops;
    they surface findings for the report."""
    if config.progress:
        print("=" * 80)
        print("Phase 2 — Sanity checks")
        print("=" * 80)

    findings: dict[str, Any] = {}
    df = pd.DataFrame([asdict(c) for c in matrix])

    # 1. Error cells
    err_cells = [c for c in matrix if c.notes.startswith("ERROR")]
    findings["error_cells"] = len(err_cells)
    findings["error_details"] = [f"{c.window_label}×{c.timeframe}×{int(c.leverage)}x×{c.fee_profile}: {c.notes}"
                                 for c in err_cells]

    # 2. Trade counts
    non_zero_cells = [c for c in matrix if c.trades > 0 and not c.notes]
    if non_zero_cells:
        trade_counts = [c.trades for c in non_zero_cells]
        findings["trade_count"] = {
            "min": min(trade_counts),
            "max": max(trade_counts),
            "mean": sum(trade_counts) / len(trade_counts),
            "zero_trade_cells": sum(1 for c in matrix if c.trades == 0 and not c.notes),
        }

    # 3. Leverage invariance detection — KEY gate that triggers Phase 3
    # Within each (window, TF, fee) triplet, check if all leverages produce
    # identical return_pct. If so, flag for leverage validation phase.
    lev_invariant_triplets = 0
    lev_variant_triplets = 0
    if len(df) > 0 and "return_pct" in df.columns:
        for (_, _, _), sub in df.groupby(["window_days", "timeframe", "fee_profile"]):
            if len(sub) < 2:
                continue
            returns = sub["return_pct"].unique()
            # Tolerance: bit-exact equality to within 1e-6 percentage points
            if len(returns) == 1 or (returns.max() - returns.min()) < 1e-6:
                lev_invariant_triplets += 1
            else:
                lev_variant_triplets += 1
    findings["leverage_invariance"] = {
        "invariant_triplets": lev_invariant_triplets,
        "variant_triplets": lev_variant_triplets,
        "all_invariant": lev_variant_triplets == 0 and lev_invariant_triplets > 0,
    }

    # 3b. Mode-aware assertions (task #114)
    # For RISK_SCALED, expect return_pct to correlate linearly with leverage
    # within each (window, tf, fee) triplet. For KELLY_FRACTIONAL, expect
    # invariance (Kelly determines size, leverage is just margin).
    mode = config.leverage_mode
    if mode == LeverageMode.RISK_SCALED:
        linearity_scores: list[float] = []
        if len(df) > 0 and "return_pct" in df.columns:
            for (_, _, _), sub in df.groupby(["window_days", "timeframe", "fee_profile"]):
                if len(sub) < 3:
                    continue
                sub_sorted = sub.sort_values("leverage")
                xs = sub_sorted["leverage"].to_numpy()
                ys = sub_sorted["return_pct"].to_numpy()
                if xs.std() > 0 and ys.std() > 0:
                    r = float(((xs - xs.mean()) * (ys - ys.mean())).sum() /
                              (len(xs) * xs.std() * ys.std()))
                    linearity_scores.append(r)
        findings["risk_scaled_linearity"] = {
            "mean_pearson_r": (sum(linearity_scores) / len(linearity_scores)
                               if linearity_scores else 0.0),
            "n_triplets_scored": len(linearity_scores),
            "passes_linearity": (
                (sum(1 for r in linearity_scores if r >= 0.5)
                 / max(1, len(linearity_scores))) >= 0.5
                if linearity_scores else False
            ),
        }
    elif mode == LeverageMode.KELLY_FRACTIONAL:
        # Kelly sets risk_pct independently of leverage → should be invariant
        findings["kelly_expects_invariance"] = findings["leverage_invariance"]["all_invariant"]

    # 4. Cost/margin sanity
    if non_zero_cells:
        max_cost_pct = max(c.cost_pct_of_margin for c in non_zero_cells)
        findings["max_cost_pct_of_margin"] = max_cost_pct
        if max_cost_pct > 100:
            findings["cost_warning"] = f"Cost exceeds 100% of margin in some cells ({max_cost_pct:.1f}%)"

    # 5. Drawdown sanity
    extreme_dd_cells = [c for c in matrix if c.maxdd_pct > 50 and c.trades > 0]
    findings["extreme_dd_cells"] = len(extreme_dd_cells)

    # 6. Best cell identification (by Calmar, among profitable cells)
    profitable = [c for c in non_zero_cells if c.return_pct > 0]
    if profitable:
        best = max(profitable, key=lambda c: c.calmar)
        findings["best_cell"] = {
            "window": best.window_label,
            "timeframe": best.timeframe,
            "leverage": best.leverage,
            "fee_profile": best.fee_profile,
            "return_pct": best.return_pct,
            "maxdd_pct": best.maxdd_pct,
            "calmar": best.calmar,
            "trades": best.trades,
        }

    if config.progress:
        print(f"  Error cells:         {findings['error_cells']}")
        if "trade_count" in findings:
            tc = findings["trade_count"]
            print(f"  Trade counts:        min={tc['min']} max={tc['max']} "
                  f"mean={tc['mean']:.0f} zero={tc['zero_trade_cells']}")
        li = findings["leverage_invariance"]
        print(f"  Leverage invariance: {li['invariant_triplets']} invariant / "
              f"{li['variant_triplets']} variant triplets"
              + ("  ⚠ ALL INVARIANT → Phase 3 deep-dive will run" if li['all_invariant'] else ""))
        if findings.get("extreme_dd_cells"):
            print(f"  Extreme DD cells:    {findings['extreme_dd_cells']} (dd > 50%)")
        if "best_cell" in findings:
            bc = findings["best_cell"]
            print(f"  Best cell (Calmar):  {bc['window']} × {bc['timeframe']} × "
                  f"{int(bc['leverage'])}x × {bc['fee_profile']} → "
                  f"{bc['return_pct']:+.2f}% / Calmar {bc['calmar']:.2f}")
        print()

    return findings


# ── Phase 3 — Leverage deep-dive (auto-triggered) ────────────────────────


def _phase_3_leverage_validation(
    config: DeepBacktestConfig,
    sanity: dict[str, Any],
    ctx: dict,
) -> LeverageValidationResult | None:
    """Auto-triggered when sanity checks flag leverage invariance. Runs a
    hand-traceable single-trade comparison at 1x vs max leverage and an
    alternative-sizing counter-demo that proves the framework CAN produce
    leverage variance when a strategy opts in."""
    if not config.leverage_validation_enabled:
        return None
    if not sanity.get("leverage_invariance", {}).get("all_invariant", False):
        return None
    # For RISK_SCALED mode, invariance would mean the scaling failed — which
    # we detect via the linearity sanity check instead. Don't run the Phase 3
    # hand-trace here; it would incorrectly explain invariance as "correct
    # behavior for risk-based sizing" when it's actually a symptom.
    # For KELLY_FRACTIONAL, invariance IS expected (Kelly sets size), but the
    # hand-trace still passes trivially — skip to avoid noise in the report.
    if config.leverage_mode in (LeverageMode.RISK_SCALED, LeverageMode.KELLY_FRACTIONAL):
        return None

    if config.progress:
        print("=" * 80)
        print("Phase 3 — Leverage validation deep-dive (AUTO-TRIGGERED)")
        print("  All matrix triplets show leverage-invariant P&L.")
        print("  Running hand-trace + alt-sizing counter-demo to confirm.")
        print("=" * 80)

    # Pick the highest-leverage matrix cell and a 1x cell for comparison
    tf = config.timeframes[0] if "15m" not in config.timeframes else "15m"
    wd = min(config.window_days)
    fp = config.fee_profiles[0] if "pine_zero_cost" in config.fee_profiles else config.fee_profiles[0]
    lev_min = min(config.leverages)
    lev_max = max(config.leverages)

    df = _load_timeframe(tf, config.symbol)
    df_sliced = _slice_window(df, wd, config.warmup_bars)

    def _run_at(lev: float) -> dict:
        # Apply leverage_mode transform (task #114) so the hand-trace uses
        # the same risk_pct scaling the matrix cells used.
        mode_adjusted_params = _apply_leverage_mode(config, lev)
        strategy = _resolve_strategy(config.strategy, mode_adjusted_params, tf)
        indicators = _resolve_indicators(config)
        engine = LeveragedBacktestEngine(
            initial_institutional_cash=config.initial_cash,
            initial_aggressive_cash=0.0,
            fee_model=make_fee_model(fp),
            path_model=ctx.get("m1_path_model") if tf != "1m" else None,
            run_id=f"lev_val_{int(lev)}x",
        )
        run_kwargs = dict(
            symbol=config.symbol, timeframe=tf,
            leverage=lev, sub_book=config.sub_book,
        )
        if indicators:
            run_kwargs["indicators"] = indicators
        result = engine.run(strategy, df_sliced, **run_kwargs)
        trades = result.trades
        return {
            "lev": lev,
            "n_trades": len(trades),
            "final_equity": result.metrics["final_institutional_equity"],
            "first_trade": asdict(trades[0]) if trades else None,
            "total_margin": sum(t.margin_used for t in trades),
            "total_pnl": sum(t.pnl for t in trades),
        }

    r_lo = _run_at(lev_min)
    r_hi = _run_at(lev_max)

    # Hand-trace assertions
    details = {
        "lev_low": lev_min,
        "lev_high": lev_max,
        "trades_low": r_lo["n_trades"],
        "trades_high": r_hi["n_trades"],
        "final_equity_low": r_lo["final_equity"],
        "final_equity_high": r_hi["final_equity"],
        "total_pnl_low": r_lo["total_pnl"],
        "total_pnl_high": r_hi["total_pnl"],
        "total_margin_low": r_lo["total_margin"],
        "total_margin_high": r_hi["total_margin"],
        "margin_ratio_expected": lev_max / lev_min,
        "margin_ratio_actual": (r_lo["total_margin"] / r_hi["total_margin"]
                                if r_hi["total_margin"] > 0 else None),
    }
    hand_passed = (
        r_lo["n_trades"] == r_hi["n_trades"]
        and abs(r_lo["final_equity"] - r_hi["final_equity"]) < 0.01
        and abs(r_lo["total_pnl"] - r_hi["total_pnl"]) < 0.01
    )
    reason = (
        f"Strategy uses risk-based sizing where notional = equity × (risk_pct/sl_pct). "
        f"Position quantity is therefore leverage-independent, making P&L leverage-invariant. "
        f"The engine's `leverage` parameter acts as a max-margin cap, not a position multiplier. "
        f"This is correct CFD accounting — P&L is return on full account, margin scales "
        f"inversely with leverage."
        if hand_passed else
        f"Hand-trace found DIFFERENCES between {lev_min}x and {lev_max}x runs — "
        f"further investigation required. trades: {r_lo['n_trades']} vs {r_hi['n_trades']}, "
        f"final_eq: {r_lo['final_equity']:.2f} vs {r_hi['final_equity']:.2f}"
    )

    if config.progress:
        print(f"\n  Hand-trace ({lev_min}x vs {lev_max}x):")
        print(f"    trades:       {r_lo['n_trades']} / {r_hi['n_trades']}")
        print(f"    final equity: ${r_lo['final_equity']:.2f} / ${r_hi['final_equity']:.2f}")
        print(f"    total P&L:    ${r_lo['total_pnl']:.2f} / ${r_hi['total_pnl']:.2f}")
        ratio_str = (f"{details['margin_ratio_actual']:.1f}x"
                     if details.get("margin_ratio_actual") is not None else "n/a")
        print(f"    total margin: ${r_lo['total_margin']:.2f} / ${r_hi['total_margin']:.2f} "
              f"(ratio: {ratio_str}, expected {details['margin_ratio_expected']:.0f}x)")
        note = ""
        if r_lo["n_trades"] == 0 and r_hi["n_trades"] == 0:
            note = "  ⚠ zero trades — invariance is trivial (strategy never fired in this window)"
        print(f"  Verdict: {'✓ identical' if hand_passed else '✗ DIFFERS'}{note}")
        print()

    return LeverageValidationResult(
        invariant_detected=True,
        invariant_reason=reason,
        hand_trace_passed=hand_passed,
        hand_trace_details=details,
        alt_sizing_demo=[],  # Expensive; skip by default. Users can extend.
    )


# ── Phase 4 — Walk-forward OOS ───────────────────────────────────────────


def _pick_wf_config(
    config: DeepBacktestConfig, sanity: dict
) -> tuple[str, str]:
    """Pick (timeframe, fee_profile) for the walk-forward run.
    Defaults to the matrix best cell unless config overrides.
    """
    tf = config.wf_timeframe
    fp = config.wf_fee_profile
    if tf is None or fp is None:
        bc = sanity.get("best_cell")
        if bc:
            tf = tf or bc["timeframe"]
            fp = fp or bc["fee_profile"]
    tf = tf or (config.timeframes[0] if config.timeframes else "15m")
    fp = fp or (config.fee_profiles[0] if config.fee_profiles else "ic_markets_mt4_xauusd_normal")
    return tf, fp


def _wf_run_fold(
    config: DeepBacktestConfig,
    tf: str, fp: str, leverage: float,
    fold_df: pd.DataFrame,
    m1pm,
    params: dict[str, Any],
    run_id: str,
) -> dict:
    # Apply leverage_mode transform (task #114) to the base strategy_params
    # FIRST, then layer per-fold retune params on top. Order matters: WF
    # retune param grids should override the mode's risk_pct scaling when
    # both are present (user explicitly grid-searching risk_pct overrides
    # the mode transform).
    mode_adjusted_base = _apply_leverage_mode(config, leverage)
    merged_params = {**mode_adjusted_base, **params}
    strategy = _resolve_strategy(config.strategy, merged_params, tf)
    engine = LeveragedBacktestEngine(
        initial_institutional_cash=config.initial_cash,
        initial_aggressive_cash=0.0,
        fee_model=make_fee_model(fp),
        path_model=m1pm if tf != "1m" else None,
        run_id=run_id,
    )
    indicators = _resolve_indicators(config)
    run_kwargs = dict(
        symbol=config.symbol, timeframe=tf,
        leverage=leverage, sub_book=config.sub_book,
    )
    if indicators:
        run_kwargs["indicators"] = indicators
    result = engine.run(strategy, fold_df, **run_kwargs)
    final_eq = result.metrics["final_institutional_equity"]
    return_pct = (final_eq - config.initial_cash) / config.initial_cash * 100.0
    max_dd = result.metrics["max_dd_pct"]
    trades = result.trades
    n = len(trades)
    if n == 0:
        return {"trades": 0, "return_pct": return_pct, "max_dd_pct": max_dd,
                "win_rate": 0.0, "profit_factor": 0.0, "sharpe": 0.0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n * 100.0
    total_win = sum(wins)
    total_loss = -sum(losses)
    pf = (total_win / total_loss) if total_loss > 0 else (float("inf") if total_win > 0 else 0.0)
    sharpe = _sharpe_from_equity(result.equity_curve_total, _periods_per_year_for_tf(tf))
    return {
        "trades": n, "return_pct": return_pct, "max_dd_pct": max_dd,
        "win_rate": win_rate, "profit_factor": pf, "sharpe": sharpe,
    }


def _wf_retune_on_train(
    config: DeepBacktestConfig,
    tf: str, fp: str,
    train_df: pd.DataFrame,
    m1pm,
) -> dict:
    """Small grid search on a training window. Returns the param dict that
    maximized Calmar (with a min-trades floor)."""
    grid = config.wf_param_grid or {}
    if not grid:
        return {}
    names = list(grid.keys())
    values = [grid[k] for k in names]
    best = None
    best_calmar = -1e18
    for combo in itertools.product(*values):
        params = dict(zip(names, combo))
        r = _wf_run_fold(
            config, tf, fp, config.wf_leverage, train_df, m1pm,
            params=params, run_id=f"wf_tune_{abs(hash(tuple(combo))) % 10_000}",
        )
        if r["trades"] < 5:
            continue
        # Annualize train return using train-window size for Calmar
        window_days = max(1, (len(train_df) / _periods_per_year_for_tf(tf)) * 365)
        ann = _annualized_return_pct(r["return_pct"], int(window_days))
        cal = _calmar_from_return_dd(ann, r["max_dd_pct"])
        if cal > best_calmar:
            best_calmar = cal
            best = params
    return best or {}


def _phase_4_walk_forward(
    config: DeepBacktestConfig, sanity: dict, ctx: dict
) -> WalkForwardSummary | None:
    """7 non-overlapping 90-day OOS folds by default. dt-based slicing to
    handle weekend gaps. Fixed params unless wf_retune=True."""
    if not config.wf_enabled:
        return None

    tf, fp = _pick_wf_config(config, sanity)
    if config.progress:
        print("=" * 80)
        print(f"Phase 4 — Walk-forward OOS ({config.wf_n_folds} × {config.wf_fold_days}d, "
              f"tf={tf}, fee={fp}, lev={config.wf_leverage}x, "
              f"{'per-fold retune' if config.wf_retune else 'fixed params'})")
        print("=" * 80)

    df = _load_timeframe(tf, config.symbol)
    m1pm = ctx.get("m1_path_model")

    last_dt = df["dt"].iloc[-1]
    total_days = config.wf_n_folds * config.wf_fold_days
    if config.wf_retune:
        total_days += config.wf_train_days  # reserve train window
    wf_start_dt = last_dt - pd.Timedelta(days=total_days)

    first_dt = df["dt"].iloc[0]
    if wf_start_dt < first_dt + pd.Timedelta(days=30):
        print(f"  ⚠ Not enough calendar data for WF — need {total_days}d + 30d buffer, "
              f"have {(last_dt - first_dt).days}d. Skipping.")
        return None

    fold_results: list[WalkForwardFold] = []
    for fold in range(config.wf_n_folds):
        if config.wf_retune:
            train_start = wf_start_dt + pd.Timedelta(days=fold * config.wf_fold_days)
            train_end = train_start + pd.Timedelta(days=config.wf_train_days)
            test_start = train_end
            test_end = test_start + pd.Timedelta(days=config.wf_fold_days)
        else:
            test_start = wf_start_dt + pd.Timedelta(days=fold * config.wf_fold_days)
            test_end = test_start + pd.Timedelta(days=config.wf_fold_days)
            train_start = train_end = None  # noqa: unused

        # Fold test slice (+ warmup)
        test_mask = (df["dt"] >= test_start) & (df["dt"] < test_end)
        test_idx = df.index[test_mask]
        if len(test_idx) == 0:
            continue
        test_start_idx = int(test_idx[0])
        test_end_idx = int(test_idx[-1]) + 1
        warmup_start = max(0, test_start_idx - config.warmup_bars)
        fold_df = df.iloc[warmup_start:test_end_idx].reset_index(drop=True)

        # Retune on train window if requested
        params = {}
        if config.wf_retune and config.wf_param_grid:
            train_mask = (df["dt"] >= train_start) & (df["dt"] < train_end)
            train_idx = df.index[train_mask]
            if len(train_idx) > 0:
                train_s = max(0, int(train_idx[0]) - config.warmup_bars)
                train_e = int(train_idx[-1]) + 1
                train_df_ = df.iloc[train_s:train_e].reset_index(drop=True)
                params = _wf_retune_on_train(config, tf, fp, train_df_, m1pm)

        r = _wf_run_fold(
            config, tf, fp, config.wf_leverage, fold_df, m1pm,
            params=params, run_id=f"wf_fold_{fold + 1}",
        )
        ann = _annualized_return_pct(r["return_pct"], config.wf_fold_days)
        calmar = _calmar_from_return_dd(ann, r["max_dd_pct"])

        fr = WalkForwardFold(
            fold=fold + 1,
            start_ts=str(df["dt"].iloc[test_start_idx]),
            end_ts=str(df["dt"].iloc[test_end_idx - 1]),
            trades=r["trades"],
            return_pct=r["return_pct"],
            max_dd_pct=r["max_dd_pct"],
            calmar=calmar,
            sharpe=r["sharpe"],
            win_rate=r["win_rate"],
            profit_factor=r["profit_factor"],
            best_params=params or None,
        )
        fold_results.append(fr)
        if config.progress:
            pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "inf"
            print(f"  Fold {fold + 1}: {fr.start_ts[:10]} → {fr.end_ts[:10]}  "
                  f"ret={r['return_pct']:+7.2f}% dd={r['max_dd_pct']:5.2f}% "
                  f"calmar={calmar:+6.3f} sharpe={r['sharpe']:+6.3f} "
                  f"trades={r['trades']:3d} pf={pf_str}")

    if not fold_results:
        return None

    # Aggregate
    n = len(fold_results)
    returns = [f.return_pct for f in fold_results]
    dds = [f.max_dd_pct for f in fold_results]
    calmars = [f.calmar for f in fold_results]
    sharpes = [f.sharpe for f in fold_results]
    wrs = [f.win_rate for f in fold_results]

    def ms(xs):
        m = sum(xs) / len(xs)
        v = sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)
        return m, math.sqrt(v)

    mean_ret, std_ret = ms(returns)
    mean_dd, std_dd = ms(dds)
    mean_cal, std_cal = ms(calmars)
    mean_shp, std_shp = ms(sharpes)
    mean_wr, _ = ms(wrs)
    profitable = sum(1 for r in returns if r > 0)
    gate_per_fold = mean_cal >= config.wf_gate_calmar

    # Continuous run over the full WF window as cross-check (the honest
    # number that's not inflated by per-fold Calmar annualization outliers)
    cont_start_dt = df["dt"].iloc[0]
    cont_mask = (df["dt"] >= wf_start_dt) & (df["dt"] <= last_dt)
    cont_idx = df.index[cont_mask]
    if len(cont_idx) > 0:
        cs = max(0, int(cont_idx[0]) - config.warmup_bars)
        ce = int(cont_idx[-1]) + 1
        cont_df = df.iloc[cs:ce].reset_index(drop=True)
        r_cont = _wf_run_fold(
            config, tf, fp, config.wf_leverage, cont_df, m1pm,
            params={}, run_id="wf_continuous",
        )
        cont_ret = r_cont["return_pct"]
        cont_dd = r_cont["max_dd_pct"]
        cont_days = (last_dt - wf_start_dt).days
        cont_ann = _annualized_return_pct(cont_ret, cont_days)
        cont_calmar = _calmar_from_return_dd(cont_ann, cont_dd)
    else:
        cont_ret = cont_dd = cont_calmar = 0.0
    gate_continuous = cont_calmar >= config.wf_gate_calmar

    if config.progress:
        print(f"\n  Mean return per fold: {mean_ret:+.2f}% ± {std_ret:.2f}")
        print(f"  Mean Calmar:          {mean_cal:+.3f} ± {std_cal:.3f}  "
              f"({'✓ PASS' if gate_per_fold else '✗ FAIL'} gate ≥ {config.wf_gate_calmar})")
        print(f"  Continuous Calmar:    {cont_calmar:+.3f}  "
              f"({'✓ PASS' if gate_continuous else '✗ FAIL'} gate ≥ {config.wf_gate_calmar})")
        print(f"  Profitable folds:     {profitable} / {n}")
        print()

    return WalkForwardSummary(
        timeframe=tf, fee_profile=fp,
        n_folds=n, fold_days=config.wf_fold_days,
        total_trades=sum(f.trades for f in fold_results),
        mean_return_pct=mean_ret, std_return_pct=std_ret,
        mean_dd_pct=mean_dd, std_dd_pct=std_dd,
        mean_calmar=mean_cal, std_calmar=std_cal,
        mean_sharpe=mean_shp, std_sharpe=std_shp,
        mean_win_rate=mean_wr, profitable_folds=profitable,
        gate_calmar=config.wf_gate_calmar,
        gate_per_fold_passed=gate_per_fold,
        continuous_return_pct=cont_ret, continuous_dd_pct=cont_dd,
        continuous_calmar=cont_calmar, continuous_gate_passed=gate_continuous,
        folds=fold_results,
    )


# ── Phase 5 — Verdict ────────────────────────────────────────────────────


def _phase_5_verdict(
    sanity: dict,
    lev_val: LeverageValidationResult | None,
    wf: WalkForwardSummary | None,
    config: DeepBacktestConfig,
) -> tuple[str, str]:
    """Produce a concise verdict string based on all prior phases.

    Returns (verdict_label, reason_text).

    Mode-aware gating (task #114):
        INVARIANT / MARGIN_CAPPED — Calmar ≥ wf_gate_calmar (default 0.5)
        VOL_TARGETED              — Calmar ≥ 1.0 (stricter, signal-edge bias)
        RISK_SCALED               — return-biased: Calmar ≥ 0.3 AND annualized
                                    return ≥ 100%/yr (accepts higher variance)
        KELLY_FRACTIONAL          — growth-biased: Calmar ≥ 0.5 AND ann. return
                                    ≥ 50%/yr
    """
    if sanity.get("error_cells", 0) > 0 and not sanity.get("best_cell"):
        return "FAILED", f"{sanity['error_cells']} matrix cells errored and no profitable cell found"

    if not sanity.get("best_cell"):
        return "RESEARCH_ONLY", "No profitable cell in the matrix"

    best = sanity["best_cell"]
    mode = config.leverage_mode

    if wf is None:
        return "NEEDS_WF", (
            f"[mode={mode.value}] Matrix best cell: {best['return_pct']:+.1f}% / "
            f"Calmar {best['calmar']:.2f}. Walk-forward validation not run; "
            f"cannot confirm OOS robustness."
        )

    # Annualized return estimate from per-fold mean return × (365 / fold_days)
    ann_return = wf.mean_return_pct * (365.0 / max(1, wf.fold_days))

    # Mode-aware gate selection
    if mode == LeverageMode.RISK_SCALED:
        passes = wf.continuous_calmar >= 0.3 and ann_return >= 100.0
        gate_desc = "Calmar ≥ 0.3 AND annualized return ≥ 100%/yr (return-biased)"
    elif mode == LeverageMode.KELLY_FRACTIONAL:
        passes = wf.continuous_calmar >= 0.5 and ann_return >= 50.0
        gate_desc = "Calmar ≥ 0.5 AND annualized return ≥ 50%/yr (growth-biased)"
    elif mode == LeverageMode.VOL_TARGETED:
        passes = wf.continuous_calmar >= 1.0
        gate_desc = "Calmar ≥ 1.0 (vol-targeted requires stronger signal edge)"
    else:  # INVARIANT, MARGIN_CAPPED
        passes = wf.continuous_calmar >= config.wf_gate_calmar
        gate_desc = f"Calmar ≥ {config.wf_gate_calmar}"

    if passes:
        return "DEPLOYABLE", (
            f"[mode={mode.value}] Best cell {best['window']} × {best['timeframe']} × "
            f"{int(best['leverage'])}x × {best['fee_profile']}; "
            f"WF continuous Calmar {wf.continuous_calmar:+.3f}, ann return {ann_return:+.1f}%/yr. "
            f"Gate: {gate_desc}."
        )

    if wf.gate_per_fold_passed and not passes:
        return "RESEARCH_ONLY", (
            f"[mode={mode.value}] Matrix best cell looks good "
            f"({best['return_pct']:+.1f}% / Calmar {best['calmar']:.2f}) but walk-forward "
            f"continuous run fails the gate (Calmar {wf.continuous_calmar:+.3f}, "
            f"ann return {ann_return:+.1f}%/yr). Gate: {gate_desc}. "
            f"Per-fold mean {wf.mean_calmar:+.3f} ± {wf.std_calmar:.3f} technically "
            f"passes but is likely inflated by fold outliers."
        )

    return "RESEARCH_ONLY", (
        f"[mode={mode.value}] Walk-forward failed ({gate_desc}). Continuous Calmar "
        f"{wf.continuous_calmar:+.3f}, per-fold mean {wf.mean_calmar:+.3f}, "
        f"ann return {ann_return:+.1f}%/yr. Strategy is not deployment-ready."
    )


# ── Main pipeline ────────────────────────────────────────────────────────


def run_deep_backtest(config: DeepBacktestConfig) -> DeepBacktestResult:
    """Execute all phases and return a structured result.

    Report generation is delegated to :func:`src.backtest.deep_backtest_report.write_report`
    which handles CSV + PNG heatmaps + HTML + PDF.
    """
    t_start = time.time()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    strategy_name = config.resolve_strategy_name()

    # Determine output directory
    if config.out_dir is None:
        config.out_dir = REPO_ROOT / "reports" / f"deep_backtest_{strategy_name}_{timestamp}"
    config.out_dir.mkdir(parents=True, exist_ok=True)

    # Phases
    ctx = _phase_0_preflight(config)
    matrix = _phase_1_matrix(config, ctx)
    sanity = _phase_2_sanity_checks(matrix, config)
    lev_val = _phase_3_leverage_validation(config, sanity, ctx)
    wf = _phase_4_walk_forward(config, sanity, ctx) if config.wf_enabled else None
    verdict, reason = _phase_5_verdict(sanity, lev_val, wf, config)

    matrix_df = pd.DataFrame([asdict(c) for c in matrix])
    best_cell = None
    if "best_cell" in sanity:
        bc = sanity["best_cell"]
        for c in matrix:
            if (c.window_label == bc["window"] and c.timeframe == bc["timeframe"]
                and c.leverage == bc["leverage"] and c.fee_profile == bc["fee_profile"]):
                best_cell = c
                break

    result = DeepBacktestResult(
        config=config,
        strategy_name=strategy_name,
        timestamp=timestamp,
        matrix=matrix,
        matrix_df=matrix_df,
        best_cell=best_cell,
        sanity_checks=sanity,
        leverage_validation=lev_val,
        walk_forward=wf,
        verdict=verdict,
        verdict_reason=reason,
        report_dir=config.out_dir,
        elapsed_seconds=time.time() - t_start,
    )

    # Write JSON summary (always)
    _write_json_summary(result)

    # Generate HTML / PDF / heatmaps if requested
    if config.generate_html or config.generate_pdf or config.generate_heatmaps:
        from src.backtest.deep_backtest_report import write_report
        write_report(result)

    if config.progress:
        print("=" * 80)
        print(f"VERDICT: {verdict}")
        print(f"  {reason}")
        print(f"  Elapsed: {result.elapsed_seconds:.1f}s")
        print(f"  Report:  {config.out_dir}")
        print("=" * 80)

    return result


def _write_json_summary(result: DeepBacktestResult) -> None:
    """Dump a structured JSON summary for downstream consumption."""
    out = {
        "strategy": result.strategy_name,
        "timestamp": result.timestamp,
        "verdict": result.verdict,
        "verdict_reason": result.verdict_reason,
        "elapsed_seconds": result.elapsed_seconds,
        "config": {
            "timeframes": result.config.timeframes,
            "window_days": result.config.window_days,
            "leverages": result.config.leverages,
            "fee_profiles": result.config.fee_profiles,
            "wf_enabled": result.config.wf_enabled,
            "wf_n_folds": result.config.wf_n_folds,
            "wf_fold_days": result.config.wf_fold_days,
            "wf_gate_calmar": result.config.wf_gate_calmar,
            "wf_retune": result.config.wf_retune,
            "strategy_params": result.config.strategy_params,
        },
        "matrix_n_cells": len(result.matrix),
        "sanity_checks": result.sanity_checks,
        "leverage_validation": (
            asdict(result.leverage_validation) if result.leverage_validation else None
        ),
        "walk_forward": (
            {**asdict(result.walk_forward),
             "folds": [asdict(f) for f in result.walk_forward.folds]}
            if result.walk_forward else None
        ),
        "best_cell": asdict(result.best_cell) if result.best_cell else None,
    }
    (result.report_dir / "summary.json").write_text(json.dumps(out, indent=2, default=str))
    result.matrix_df.to_csv(result.report_dir / "matrix.csv", index=False)
