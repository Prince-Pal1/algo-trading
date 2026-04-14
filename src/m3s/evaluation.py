"""M3S Evaluation Layer — Purged K-Fold CV + Bayesian Fractional Kelly.

Two Tier 1 additions bundled because they are cheap and deeply intertwined:

## #6 — Purged & Embargoed K-Fold Cross-Validation (Lopez de Prado, AFML ch.7)

Standard K-fold CV leaks in financial data because labels overlap in time
(a signal on Monday predicts returns through Friday). Purging removes
training samples whose labels overlap the test set; embargoing adds a gap
after the test window to prevent serial-correlation leakage.

Our use: split a strategy's historical per-trade PnL stream into K folds
and compute the **per-fold Sharpe ratio**. The distribution of fold Sharpes
is the strategy's "how certain are we about its edge" distribution. A
strategy with stable Sharpe across folds has high confidence; a strategy
with wildly varying Sharpes is overfit to one window.

## #7 — Bayesian Fractional Kelly (Baker-McHale 2013)

Kelly's optimal bet size assumes you KNOW the mean return. In practice you
estimate it, with standard error `σ_μ`. The parameter-uncertainty-adjusted
optimum is:

    f* = μ̂ / (σ² + σ_μ²)

As the data window grows, `σ_μ → 0` and the formula converges to plain
Kelly. For a new strategy with 3 months of data, `σ_μ` is large and the
fraction is smaller (~⅕ Kelly). For a veteran strategy with 2 years of
data, it approaches ½ Kelly.

## Integration

The allocator (sub-phase 0.4) reads `StrategyEvaluation.kelly_fraction`
instead of the static `mode.kelly_fraction` when the strategy has enough
history. Mature strategies get their full mode fraction; new strategies
get throttled automatically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from src.utils.logger import get_logger

log = get_logger("m3s.evaluation")


_CRYPTO_ANNUALIZATION = math.sqrt(365.0)  # Legacy default — G.0 parameterized callers
_DEFAULT_PERIODS_PER_YEAR = 365.0          # Pass 252 at call time for forex


# ══════════════════════════════════════════════════════════════════════
# Purged K-Fold CV
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PurgedFold:
    """One fold from purged K-fold CV. Indices are into the sorted-by-time input."""
    fold_index: int
    train_indices: np.ndarray
    test_indices: np.ndarray


def purged_kfold_splits(
    n_samples: int,
    *,
    n_splits: int = 5,
    embargo_pct: float = 0.01,
    label_overlap_bars: int = 0,
) -> Iterator[PurgedFold]:
    """Yield purged and embargoed K-fold splits.

    Args:
        n_samples: total number of ordered samples (trades, rows, etc.)
        n_splits: number of folds (must be >= 2)
        embargo_pct: fraction of samples to embargo AFTER each test window
            (e.g., 0.01 = 1%). Prevents serial-correlation leakage across
            the train/test boundary.
        label_overlap_bars: how many trailing training samples to purge
            behind each test window. If trades have labels computed over
            the next N bars, set this to N so overlapping train samples
            are removed.

    Yields PurgedFold objects with numpy arrays of train/test indices.
    """
    if n_splits < 2:
        raise ValueError(f"n_splits must be >= 2, got {n_splits}")
    if n_samples < n_splits:
        raise ValueError(f"n_samples ({n_samples}) < n_splits ({n_splits})")
    if not (0.0 <= embargo_pct < 1.0):
        raise ValueError(f"embargo_pct must be in [0, 1), got {embargo_pct}")
    if label_overlap_bars < 0:
        raise ValueError(f"label_overlap_bars must be >= 0, got {label_overlap_bars}")

    embargo_size = max(0, int(n_samples * embargo_pct))
    indices = np.arange(n_samples)
    fold_size = n_samples // n_splits

    for fold_idx in range(n_splits):
        test_start = fold_idx * fold_size
        # Last fold absorbs any remainder.
        test_end = n_samples if fold_idx == n_splits - 1 else (fold_idx + 1) * fold_size
        test_indices = indices[test_start:test_end]

        # Build the train mask: everything outside [test_start, test_end).
        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[test_start:test_end] = False

        # Purge: remove training samples whose labels overlap the test window.
        # A sample at position i has a label that spans [i, i + label_overlap_bars].
        # If i + label_overlap_bars >= test_start and i < test_start, it overlaps.
        purge_lo = max(0, test_start - label_overlap_bars)
        train_mask[purge_lo:test_start] = False

        # Embargo: skip training samples in [test_end, test_end + embargo_size).
        embargo_hi = min(n_samples, test_end + embargo_size)
        train_mask[test_end:embargo_hi] = False

        train_indices = indices[train_mask]
        yield PurgedFold(
            fold_index=fold_idx,
            train_indices=train_indices,
            test_indices=test_indices,
        )


# ══════════════════════════════════════════════════════════════════════
# Per-fold Sharpe + distribution
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class StrategyEvaluation:
    """Output of evaluating one strategy's trade log via purged CV."""
    strategy: str
    n_trades: int
    n_folds: int
    fold_sharpes: list[float]
    mean_sharpe: float
    sharpe_std_err: float
    deflated_sharpe: float
    kelly_fraction: float
    reasoning: str


def _fold_sharpe(
    pnls: np.ndarray,
    periods_per_year: float = _DEFAULT_PERIODS_PER_YEAR,
) -> float:
    """Annualized Sharpe of a fold's PnL vector. Zero if too few trades.

    Args:
        periods_per_year: annualization denominator. Default 365 for crypto
            24/7 (backward compat). Pass 252 for forex (XAUUSD etc.).
    """
    if len(pnls) < 2:
        return 0.0
    mean = float(pnls.mean())
    std = float(pnls.std(ddof=0))
    if std == 0.0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def _deflated_sharpe(
    observed_sharpe: float,
    n_trades: int,
    n_trials: int = 1,
) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014, simplified form).

    Corrects an observed Sharpe for:
    - Finite sample size (more samples → less variance in the estimator)
    - Multiple testing inflation (trying many strategies inflates the best one)

    Returns a scalar in [0, observed_sharpe] — the "honest" Sharpe after
    adjustment. We use the conservative approximation:

        DSR = observed_sharpe * sqrt(1 - (trials_penalty + finite_sample_penalty))

    where penalties are each capped at 0.4 to prevent the DSR from going
    negative on small samples.
    """
    if n_trades < 10:
        return 0.0
    finite_sample_penalty = min(0.4, 5.0 / n_trades)
    trials_penalty = min(0.4, 0.05 * math.log(max(1, n_trials)))
    total_penalty = finite_sample_penalty + trials_penalty
    if total_penalty >= 1.0:
        return 0.0
    return observed_sharpe * math.sqrt(1.0 - total_penalty)


# ══════════════════════════════════════════════════════════════════════
# Bayesian fractional Kelly
# ══════════════════════════════════════════════════════════════════════


def bayesian_fractional_kelly(
    mean_return: float,
    variance: float,
    parameter_variance: float,
    *,
    kelly_cap: float = 0.50,
    min_fraction: float = 0.20,
) -> float:
    """Return the parameter-uncertainty-adjusted Kelly fraction.

    Formula (Baker-McHale 2013):
        f* = μ̂ / (σ² + σ_μ²)

    where:
        μ̂   = mean_return
        σ²  = variance (asset/strategy return variance)
        σ_μ² = parameter_variance (uncertainty about the mean itself)

    Clamped to [min_fraction, kelly_cap]. A new strategy (high σ_μ²) lands
    near `min_fraction`; a veteran strategy (low σ_μ²) lands near `kelly_cap`.

    If mean return is non-positive, returns `min_fraction` (the allocator
    can then decide to allocate nothing; we don't return 0 here because
    calling code multiplies this into other factors).
    """
    if mean_return <= 0.0:
        return min_fraction
    denom = max(variance + parameter_variance, 1e-12)
    raw = mean_return / denom
    # Normalize: Kelly is already dimensionally fraction-of-wealth so we
    # interpret `raw` against the "full-Kelly" baseline of `mean / variance`.
    full_kelly = mean_return / max(variance, 1e-12)
    if full_kelly <= 0:
        return min_fraction
    relative = raw / full_kelly  # in (0, 1], shrinks with parameter_variance
    fraction = relative * kelly_cap
    return max(min_fraction, min(kelly_cap, fraction))


# ══════════════════════════════════════════════════════════════════════
# Strategy evaluator
# ══════════════════════════════════════════════════════════════════════


def evaluate_strategy(
    strategy: str,
    pnls: list[float] | np.ndarray,
    *,
    n_splits: int = 5,
    embargo_pct: float = 0.01,
    n_trials: int = 1,
    kelly_cap: float = 0.50,
    min_fraction: float = 0.20,
    periods_per_year: float = _DEFAULT_PERIODS_PER_YEAR,
) -> StrategyEvaluation:
    """Run purged K-fold CV on a strategy's PnL series and compute its
    Bayesian fractional Kelly.

    Args:
        strategy: name for the evaluation record
        pnls: ordered list of realized trade PnLs (no daily resampling —
            trade-level is the right granularity for strategy evaluation)
        n_splits: K for the purged K-fold
        embargo_pct: CV embargo fraction
        n_trials: number of strategies tried (inflates the DSR correction)
        kelly_cap: maximum Kelly fraction returned
        min_fraction: minimum Kelly fraction returned
        periods_per_year: annualization denominator for fold Sharpes.
            Default 365 for crypto 24/7 (backward compat). Pass 252 for
            forex (XAUUSD, FX majors) — G.0 gold-plan parameterization.

    Returns:
        StrategyEvaluation with fold Sharpes, mean/std_err, DSR, and
        Bayesian fractional Kelly.
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)

    if n < 10:
        return StrategyEvaluation(
            strategy=strategy,
            n_trades=n,
            n_folds=0,
            fold_sharpes=[],
            mean_sharpe=0.0,
            sharpe_std_err=0.0,
            deflated_sharpe=0.0,
            kelly_fraction=min_fraction,
            reasoning=f"insufficient data: {n} trades < 10 minimum",
        )

    # Run purged K-fold
    fold_sharpes: list[float] = []
    actual_splits = min(n_splits, max(2, n // 5))
    for fold in purged_kfold_splits(n, n_splits=actual_splits, embargo_pct=embargo_pct):
        test_pnls = arr[fold.test_indices]
        fs = _fold_sharpe(test_pnls, periods_per_year=periods_per_year)
        fold_sharpes.append(fs)

    # Sharpe distribution stats
    if len(fold_sharpes) < 2:
        mean_sharpe = 0.0
        sharpe_std_err = 1.0
    else:
        mean_sharpe = float(np.mean(fold_sharpes))
        # Standard error of the mean fold Sharpe
        sharpe_std_err = float(np.std(fold_sharpes, ddof=1) / math.sqrt(len(fold_sharpes)))

    # Deflated Sharpe
    dsr = _deflated_sharpe(mean_sharpe, n, n_trials=n_trials)

    # Bayesian fractional Kelly
    # Using per-trade mean/variance from the PnL series, plus σ_μ² = (sharpe_std_err * std)²
    mean_r = float(arr.mean())
    var_r = float(arr.var(ddof=0))
    # Convert the Sharpe std err back into a mean-return std err: since
    # sharpe = mean/std, std_err_of_mean ≈ sharpe_std_err * std.
    std_mean = sharpe_std_err * math.sqrt(var_r) if var_r > 0 else 0.0
    param_var = std_mean * std_mean

    kelly_fraction = bayesian_fractional_kelly(
        mean_return=mean_r,
        variance=var_r,
        parameter_variance=param_var,
        kelly_cap=kelly_cap,
        min_fraction=min_fraction,
    )

    reasoning = (
        f"n={n} folds={len(fold_sharpes)} mean_sharpe={mean_sharpe:.3f} "
        f"std_err={sharpe_std_err:.3f} dsr={dsr:.3f} f*={kelly_fraction:.3f}"
    )

    return StrategyEvaluation(
        strategy=strategy,
        n_trades=n,
        n_folds=len(fold_sharpes),
        fold_sharpes=fold_sharpes,
        mean_sharpe=mean_sharpe,
        sharpe_std_err=sharpe_std_err,
        deflated_sharpe=dsr,
        kelly_fraction=kelly_fraction,
        reasoning=reasoning,
    )
