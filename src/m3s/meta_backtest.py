"""M3S Meta-Backtest Simulator.

Replays historical per-strategy trade logs through an in-memory M3S instance
and produces a full scaled equity curve + metrics. The simulator's entire
purpose is to answer one question before live:

    **Does M3S beat fixed-weight 40/30/30?**

If meta-backtest Sharpe is not ≥ 2.35 (the approved bar in §17 of the plan),
M3S does not ship in authoritative mode. Period.

## How the replay works

1. Load trade logs per strategy (list of dicts: strategy, symbol, pnl, ts_ms).
2. Concatenate into one chronologically-sorted stream.
3. For each trade:
    - Build a synthetic signal with risk_pct = 0.01 (the post-risk-manager
      default) and run `M3S.on_signal` on it in non-shadow mode.
    - Apply `scaled_risk_pct / 0.01` as a PnL multiplier (sizing scaling is
      linear in risk_pct under the backtest engine — verified Session 20
      bit-exact TV match).
    - Feed the scaled PnL to `M3S.on_trade_close` so the tracker + compounder
      state advance correctly.
    - Trigger `M3S.rebalance()` on the configured cadence (daily/weekly).
4. Record equity after each step → build equity curve.
5. Compute metrics: Sharpe, max DD, Calmar, avg turnover.

## Baselines

Four baselines are always run side-by-side for comparison:
- `fixed_weight` — the current live portfolio (bb_rsi_mr_opt 40%, donchian 30%,
  vol_momentum 30%)
- `inverse_vol` — weight by 1/σ from the return matrix
- `m3s_conservative` — M3S in CONSERVATIVE mode
- `m3s_standard` — M3S in STANDARD mode

GROWTH and CUSTOM are optional (tests add them explicitly when comparing
aggression).

## Metric conventions

- Crypto annualization: √365 (24/7 market)
- Sharpe from daily-resampled returns (sparse strategies — see ARCHITECTURE
  Known Gotcha 2026-04-11)
- Max drawdown from equity curve low after peak
- Calmar = annualized return / |max DD|
- Turnover = average L1 distance between consecutive allocation weight vectors
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from src.m3s.allocator import Allocator
from src.m3s.compounder import Compounder
from src.m3s.conviction import ConvictionScorer
from src.m3s.edge_decay import EdgeDecayMonitor
from src.m3s.hooks import M3S
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

log = get_logger("m3s.meta_backtest")


_MS_PER_DAY = 86_400_000
_CRYPTO_ANNUALIZATION = math.sqrt(365.0)  # Legacy default — G.0 parameterized callers
_DEFAULT_PERIODS_PER_YEAR = 365.0          # Pass 252 at call time for forex


# ══════════════════════════════════════════════════════════════════════
# Trade record + curve
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MetaTrade:
    strategy: str
    symbol: str
    pnl: float
    ts_ms: int
    risk_pct: float = 0.01


@dataclass
class MetaBacktestResult:
    name: str
    equity_curve: list[tuple[int, float]]     # list of (ts_ms, equity)
    initial_equity: float
    final_equity: float
    max_drawdown_pct: float
    sharpe_annualized: float
    calmar: float
    avg_turnover: float
    turnover_series: list[float] = field(default_factory=list)
    n_trades: int = 0

    @property
    def total_return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return (self.final_equity / self.initial_equity) - 1.0


# ══════════════════════════════════════════════════════════════════════
# Metric helpers
# ══════════════════════════════════════════════════════════════════════


def _daily_returns_from_curve(curve: list[tuple[int, float]]) -> list[float]:
    """Resample a (ts_ms, equity) curve to daily returns."""
    if len(curve) < 2:
        return []
    by_day: dict[int, float] = {}
    for ts, eq in curve:
        day = ts // _MS_PER_DAY
        by_day[day] = eq  # last equity of the day wins
    days = sorted(by_day.keys())
    if len(days) < 2:
        return []
    rets: list[float] = []
    prev_eq = by_day[days[0]]
    for day in days[1:]:
        cur_eq = by_day[day]
        if prev_eq > 0:
            rets.append((cur_eq / prev_eq) - 1.0)
        prev_eq = cur_eq
    return rets


def _sharpe(
    returns: list[float],
    periods_per_year: float = _DEFAULT_PERIODS_PER_YEAR,
) -> float:
    """Annualized Sharpe on a per-day returns series.

    Args:
        periods_per_year: annualization denominator. Default 365 for crypto
            24/7 (backward compat). Pass 252 for forex 24/5 (XAUUSD).
    """
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def _max_drawdown(curve: list[tuple[int, float]]) -> float:
    if not curve:
        return 0.0
    peak = -math.inf
    max_dd = 0.0
    for _, eq in curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = 1.0 - (eq / peak)
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _calmar(initial: float, final: float, max_dd: float, n_days: int) -> float:
    if max_dd <= 0.0 or initial <= 0.0 or n_days <= 0:
        return 0.0
    total_return = (final / initial) - 1.0
    years = n_days / 365.0
    if years <= 0:
        return 0.0
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0
    return cagr / max_dd


# ══════════════════════════════════════════════════════════════════════
# Simulator
# ══════════════════════════════════════════════════════════════════════


def run_fixed_weight_baseline(
    trades: list[MetaTrade],
    weights: dict[str, float],
    *,
    initial_equity: float = 10_000.0,
    periods_per_year: float = _DEFAULT_PERIODS_PER_YEAR,
) -> MetaBacktestResult:
    """Baseline: apply a static weight per strategy to every trade's PnL.

    Args:
        periods_per_year: annualization for Sharpe. Default 365 (crypto 24/7),
            pass 252 for forex (XAUUSD).
    """
    equity = initial_equity
    curve: list[tuple[int, float]] = []
    peak = initial_equity
    for t in sorted(trades, key=lambda x: x.ts_ms):
        w = weights.get(t.strategy, 0.0)
        equity += t.pnl * w
        curve.append((t.ts_ms, equity))
        if equity > peak:
            peak = equity

    rets = _daily_returns_from_curve(curve)
    max_dd = _max_drawdown([(0, initial_equity)] + curve)
    n_days = (curve[-1][0] - curve[0][0]) // _MS_PER_DAY if curve else 0
    return MetaBacktestResult(
        name="fixed_weight",
        equity_curve=curve,
        initial_equity=initial_equity,
        final_equity=equity,
        max_drawdown_pct=max_dd,
        sharpe_annualized=_sharpe(rets, periods_per_year=periods_per_year),
        calmar=_calmar(initial_equity, equity, max_dd, n_days),
        avg_turnover=0.0,  # fixed weight = zero turnover
        n_trades=len(trades),
    )


def run_m3s_simulation(
    trades: list[MetaTrade],
    *,
    mode: M3SMode,
    initial_equity: float = 10_000.0,
    rebalance_cadence_days: int = 7,
    include_conviction: bool = True,
    include_edge_decay: bool = True,
    periods_per_year: float = _DEFAULT_PERIODS_PER_YEAR,
) -> MetaBacktestResult:
    """Replay trades through a live M3S instance in non-shadow mode.

    Args:
        periods_per_year: annualization denominator. Default 365 for crypto
            24/7 (backward compat). Pass 252 for forex (XAUUSD). Threaded
            into both PortfolioTracker and the final Sharpe computation.
    """
    mode_cfg = MODE_PRESETS[mode]
    tracker = PortfolioTracker(initial_equity=initial_equity, periods_per_year=periods_per_year)
    compounder = Compounder(mode=mode_cfg, tracker=tracker)
    allocator = Allocator(mode=mode_cfg, tracker=tracker)
    edge_decay = EdgeDecayMonitor() if include_edge_decay else None
    conviction = ConvictionScorer(enabled=include_conviction)
    m3s = M3S(
        mode=mode_cfg,
        tracker=tracker,
        compounder=compounder,
        allocator=allocator,
        edge_decay=edge_decay,
        conviction_scorer=conviction,
        shadow_mode=False,  # authoritative mode
    )

    equity = initial_equity
    curve: list[tuple[int, float]] = []
    turnovers: list[float] = []
    last_weights: dict[str, float] | None = None

    sorted_trades = sorted(trades, key=lambda x: x.ts_ms)
    if not sorted_trades:
        return MetaBacktestResult(
            name=f"m3s_{mode.value.lower()}",
            equity_curve=[],
            initial_equity=initial_equity,
            final_equity=initial_equity,
            max_drawdown_pct=0.0,
            sharpe_annualized=0.0,
            calmar=0.0,
            avg_turnover=0.0,
            n_trades=0,
        )

    first_day = sorted_trades[0].ts_ms // _MS_PER_DAY
    last_rebalance_day = first_day - rebalance_cadence_days  # force first rebalance

    for t in sorted_trades:
        day = t.ts_ms // _MS_PER_DAY

        # Time-based rebalance
        if day - last_rebalance_day >= rebalance_cadence_days:
            try:
                decision = m3s.rebalance(now_ms=t.ts_ms)
                if last_weights is not None:
                    names = set(last_weights) | set(decision.weights)
                    l1 = sum(
                        abs(last_weights.get(n, 0.0) - decision.weights.get(n, 0.0))
                        for n in names
                    )
                    turnovers.append(l1)
                last_weights = dict(decision.weights)
            except Exception as e:
                log.warning("m3s.meta.rebalance_failed", error=str(e))
            last_rebalance_day = day

        # Build and scale the synthetic signal
        sig = Signal(
            symbol=t.symbol,
            action=SignalAction.LONG,
            confidence=0.8,
            strategy_name=t.strategy,
            timeframe="1h",
            risk_pct=t.risk_pct,
            timestamp=t.ts_ms,
        )
        scaled_sig = m3s.on_signal(sig)
        scalar = (scaled_sig.risk_pct or 0.0) / t.risk_pct if t.risk_pct > 0 else 0.0

        # Apply PnL
        scaled_pnl = t.pnl * scalar
        equity += scaled_pnl
        # Let the tracker see the SCALED trade so its stats reflect reality
        m3s.on_trade_close(t.strategy, scaled_pnl, t.symbol, t.ts_ms)

        curve.append((t.ts_ms, equity))

    rets = _daily_returns_from_curve(curve)
    max_dd = _max_drawdown([(curve[0][0], initial_equity)] + curve)
    n_days = (curve[-1][0] - curve[0][0]) // _MS_PER_DAY if curve else 0
    avg_turnover = sum(turnovers) / len(turnovers) if turnovers else 0.0

    return MetaBacktestResult(
        name=f"m3s_{mode.value.lower()}",
        equity_curve=curve,
        initial_equity=initial_equity,
        final_equity=equity,
        max_drawdown_pct=max_dd,
        sharpe_annualized=_sharpe(rets, periods_per_year=periods_per_year),
        calmar=_calmar(initial_equity, equity, max_dd, n_days),
        avg_turnover=avg_turnover,
        turnover_series=turnovers,
        n_trades=len(trades),
    )


def compare_baselines(
    trades: list[MetaTrade],
    *,
    fixed_weights: dict[str, float],
    initial_equity: float = 10_000.0,
    rebalance_cadence_days: int = 7,
) -> dict[str, MetaBacktestResult]:
    """Run the full set of baselines plus CONSERVATIVE/STANDARD/GROWTH.

    Returns a dict of name → MetaBacktestResult for side-by-side comparison.
    """
    results: dict[str, MetaBacktestResult] = {}

    # Fixed weight baseline
    results["fixed_weight"] = run_fixed_weight_baseline(
        trades, fixed_weights, initial_equity=initial_equity,
    )

    # M3S modes
    for mode in [M3SMode.CONSERVATIVE, M3SMode.STANDARD, M3SMode.GROWTH]:
        res = run_m3s_simulation(
            trades,
            mode=mode,
            initial_equity=initial_equity,
            rebalance_cadence_days=rebalance_cadence_days,
        )
        results[res.name] = res

    return results
