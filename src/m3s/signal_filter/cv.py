"""Purged K-Fold Cross-Validation with per-sample t1 array (AFML ch. 7).

Standard K-fold leaks financial data because label intervals overlap. For
meta-labeling, each signal has its own label interval `[t0_i, t1_i]` where
t1 depends on when the trade actually closed. AFML's purged K-fold removes
training samples whose label intervals overlap the test fold, plus an
embargo window after each test fold.

This module differs from `src/m3s/evaluation.py:purged_kfold_splits` in that
it accepts a per-sample `t1` array instead of a fixed `label_overlap_bars`
scalar. Used by the meta-labeling training pipeline where label lifespans
vary from trade to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np


@dataclass(frozen=True)
class PurgedFoldT1:
    """One fold from purged K-fold CV with per-sample t1 tracking."""
    fold_index: int
    train_indices: np.ndarray
    test_indices: np.ndarray


def purged_kfold_splits_t1(
    t0: np.ndarray,
    t1: np.ndarray,
    *,
    n_splits: int = 5,
    embargo_pct: float = 0.01,
) -> Iterator[PurgedFoldT1]:
    """Yield purged + embargoed K-fold splits where each sample has its own
    label lifespan [t0_i, t1_i].

    Args:
        t0: array of label start timestamps (length N). Typically the signal
            timestamp.
        t1: array of label end timestamps (length N). For meta-labeling this
            is the trade close time. Must satisfy t1[i] >= t0[i].
        n_splits: number of folds (must be >= 2).
        embargo_pct: fraction of samples to embargo AFTER each test window.
            Prevents serial-correlation leakage across the train/test boundary.

    Yields:
        PurgedFoldT1 with train_indices / test_indices.

    Procedure (per fold):
        1. Test indices = contiguous slice [k*N/K, (k+1)*N/K)
        2. Train mask = all indices NOT in test
        3. Purge: drop training samples whose [t0_i, t1_i] overlaps with
           [min_test_t0, max_test_t1]
        4. Embargo: drop training samples with index in
           [test_end, test_end + embargo_size)
    """
    t0 = np.asarray(t0)
    t1 = np.asarray(t1)
    n = len(t0)
    if n != len(t1):
        raise ValueError(f"t0 and t1 must have same length; got {n} vs {len(t1)}")
    if n_splits < 2:
        raise ValueError(f"n_splits must be >= 2, got {n_splits}")
    if n < n_splits:
        raise ValueError(f"n_samples ({n}) < n_splits ({n_splits})")
    if not (0.0 <= embargo_pct < 1.0):
        raise ValueError(f"embargo_pct must be in [0, 1), got {embargo_pct}")
    if (t1 < t0).any():
        raise ValueError("found t1[i] < t0[i] — labels cannot end before they start")

    indices = np.arange(n)
    fold_size = n // n_splits
    embargo_size = max(0, int(n * embargo_pct))

    for k in range(n_splits):
        test_start = k * fold_size
        test_end = n if k == n_splits - 1 else (k + 1) * fold_size
        test_indices = indices[test_start:test_end]

        # Test window time range
        test_t0_min = float(t0[test_indices].min())
        test_t1_max = float(t1[test_indices].max())

        # Train mask: everything outside [test_start, test_end)
        train_mask = np.ones(n, dtype=bool)
        train_mask[test_start:test_end] = False

        # Purge: drop any training sample whose label interval [t0_i, t1_i]
        # overlaps [test_t0_min, test_t1_max]
        overlap_mask = (t1 >= test_t0_min) & (t0 <= test_t1_max)
        train_mask &= ~overlap_mask
        # Restore test indices as False (they must not be in training)
        train_mask[test_start:test_end] = False

        # Embargo: drop `embargo_size` samples immediately after the test fold
        embargo_start = test_end
        embargo_stop = min(n, test_end + embargo_size)
        train_mask[embargo_start:embargo_stop] = False

        train_indices = indices[train_mask]
        yield PurgedFoldT1(
            fold_index=k,
            train_indices=train_indices,
            test_indices=test_indices,
        )


def sample_uniqueness_weights(
    t0: np.ndarray,
    t1: np.ndarray,
) -> np.ndarray:
    """AFML ch. 4.4 — sample uniqueness weighting for non-IID labels.

    For each sample `i`, compute `u_i = mean(1 / c_t)` where `c_t` is the
    number of concurrent open labels at time `t`, averaged over the
    sample's lifespan `[t0_i, t1_i]`.

    Used as `sample_weight=...` in classifier training so clustered signals
    don't get double-counted.

    For meta-labeling with relatively sparse signals, uniqueness is close
    to 1 for most samples. When signals cluster (regime change, high-vol
    event), uniqueness drops — and those are the samples with the most
    training signal. Ignoring uniqueness means the model learns "cluster
    events are unusual" instead of "features at clusters predict failure."

    Approximation used here: instead of computing concurrency per time
    unit (which requires integration over arbitrary time grids), we
    count for each sample the number of OTHER samples whose interval
    overlaps it, then return `1 / (1 + overlap_count)`. This is bounded
    below by 1/N and matches AFML's intent for the clustered-cluster case.
    """
    t0 = np.asarray(t0)
    t1 = np.asarray(t1)
    n = len(t0)
    if n == 0:
        return np.array([], dtype=float)

    weights = np.zeros(n, dtype=float)
    for i in range(n):
        overlaps = int(((t1 >= t0[i]) & (t0 <= t1[i])).sum()) - 1
        overlaps = max(0, overlaps)
        weights[i] = 1.0 / (1.0 + overlaps)
    return weights
