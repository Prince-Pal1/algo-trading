"""M3S Allocator — HRP-lite + Ledoit-Wolf shrinkage + mode caps.

Produces per-strategy capital weights from a `PortfolioSnapshot`. Two paths:

- **Cold start:** any strategy with fewer than `mode.cold_start_trades_required`
  trades in the rolling window → equal-weight over all strategies, then apply
  mode caps. Prevents the allocator from chasing ghost edges while the book
  builds history.

- **Mature (HRP-lite):**
  1. Build T×N daily-return matrix from the tracker's per-strategy trade log.
  2. **Ledoit-Wolf linear shrinkage** (Tier 1 #4) on the sample covariance,
     target = scaled identity. Produces a stable `Sigma_shrunk` even with
     short histories and ill-conditioned inputs.
  3. Derive the shrunk correlation matrix and cluster strategies by signal
     correlation > `signal_corr_cluster_threshold`.
  4. Within each cluster, inverse-vol weighting using `Sigma_shrunk` diagonal.
  5. Across clusters, inverse-vol weighting by cluster aggregate vol.
  6. Apply `mode.max_per_strategy_cap` + `mode.max_cluster_cap`; any excess
     spills into a cash buffer (residual not allocated to any strategy).

Ledoit-Wolf formula (linear shrinkage to scaled-identity target):

    S      = sample covariance over T observations
    mu     = trace(S) / N                       (average variance)
    F      = mu * I                             (target)
    pi     = sum of asy. variances of S entries
    gamma  = ||S - F||_F^2                      (Frobenius distance)
    delta  = clip(pi / (gamma * T), 0, 1)
    Sigma  = delta * F + (1 - delta) * S

Reference: Ledoit & Wolf (2004) "A Well-Conditioned Estimator for
Large-Dimensional Covariance Matrices," J. Multivariate Analysis.

The allocator does NOT depend on `src/risk/`. It sits in front of the
RiskClient in sub-phase 0.5 (hooks layer) and only *shrinks* or *shifts*
weights — never bypasses a risk check.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np

from src.m3s.modes import ModeConfig
from src.m3s.portfolio import PortfolioTracker
from src.m3s.types import AllocationDecision, PortfolioSnapshot
from src.utils.logger import get_logger

log = get_logger("m3s.allocator")


_MS_PER_DAY = 86_400_000
_MIN_LW_OBSERVATIONS = 10
_DEFAULT_RETURN_WINDOW_DAYS = 60


# ══════════════════════════════════════════════════════════════════════
# Ledoit-Wolf shrinkage (Tier 1 #4)
# ══════════════════════════════════════════════════════════════════════


def ledoit_wolf_shrinkage(returns: np.ndarray) -> tuple[np.ndarray, float]:
    """Linear Ledoit-Wolf shrinkage of sample covariance toward scaled identity.

    Args:
        returns: (T, N) matrix — T observations of N assets. Must have T >= 2.

    Returns:
        (Sigma_shrunk, delta) — the shrunk covariance matrix and the applied
        shrinkage intensity in [0, 1]. `delta = 1` means the estimator is
        pure target (scaled identity); `delta = 0` means it is the raw sample
        covariance.
    """
    returns = np.asarray(returns, dtype=float)
    if returns.ndim != 2:
        raise ValueError(f"returns must be 2D (T, N), got shape {returns.shape}")
    T, N = returns.shape
    if T < 2 or N < 1:
        return np.eye(max(N, 1)), 1.0

    # Center
    X = returns - returns.mean(axis=0, keepdims=True)

    # Sample covariance (population divisor T — matches Ledoit-Wolf paper)
    S = (X.T @ X) / T

    # Target: scaled identity using the mean diagonal variance
    mu = float(np.trace(S)) / N
    F = mu * np.eye(N)

    # Asymptotic sum-of-variances estimator for the off-target noise.
    # pi_mat[i,j] = Var(S[i,j]) estimated from squared centered data.
    X2 = X * X
    pi_mat = (X2.T @ X2) / T - S * S
    pi = float(pi_mat.sum())

    # Frobenius distance^2 from S to the target.
    gamma = float(((S - F) ** 2).sum())

    if gamma <= 1e-12:
        # Already at target — no shrinkage possible.
        return S, 0.0

    delta_raw = pi / (gamma * T)
    delta = max(0.0, min(1.0, delta_raw))

    Sigma = delta * F + (1.0 - delta) * S
    return Sigma, float(delta)


def cov_to_corr(Sigma: np.ndarray) -> np.ndarray:
    """Convert covariance matrix to correlation matrix, safe for zero-var columns."""
    d = np.sqrt(np.maximum(np.diag(Sigma), 0.0))
    # Avoid divide-by-zero for zero-variance assets
    d_safe = np.where(d > 0, d, 1.0)
    corr = Sigma / np.outer(d_safe, d_safe)
    # Wipe rows/cols for zero-variance to avoid NaNs leaking
    zero_var = d == 0
    corr[zero_var, :] = 0.0
    corr[:, zero_var] = 0.0
    # Force diagonal to 1 for non-zero-var, 1 for zero-var (self-correlation)
    np.fill_diagonal(corr, 1.0)
    # Clamp numerical error outside [-1, 1]
    return np.clip(corr, -1.0, 1.0)


# ══════════════════════════════════════════════════════════════════════
# Return matrix builder
# ══════════════════════════════════════════════════════════════════════


def build_return_matrix(
    tracker: PortfolioTracker,
    names: list[str],
    *,
    now_ms: int,
    window_days: int = _DEFAULT_RETURN_WINDOW_DAYS,
) -> np.ndarray | None:
    """Build a (T, N) daily-return matrix from the tracker's trade log.

    Rows are days within `[now - window_days, now]`, columns are strategies
    in the order of `names`. Missing-trade days are zero-filled. Trade PnLs
    are divided by the tracker's current equity to produce returns.

    Returns None if any strategy has fewer than `_MIN_LW_OBSERVATIONS`
    non-zero days in the window — the allocator should fall back to
    inverse-vol-only or equal-weight in that case.
    """
    equity = tracker.equity
    if equity <= 0:
        return None

    today = now_ms // _MS_PER_DAY
    window_start = today - window_days + 1

    # Per-strategy per-day aggregate PnL
    per_strat_daily: dict[str, dict[int, float]] = {name: {} for name in names}
    for name in names:
        st = tracker._strategies.get(name)
        if st is None:
            continue
        for tr in st.trades:
            day = tr.ts_ms // _MS_PER_DAY
            if day < window_start or day > today:
                continue
            per_strat_daily[name][day] = per_strat_daily[name].get(day, 0.0) + tr.pnl

    T = window_days
    N = len(names)
    returns = np.zeros((T, N), dtype=float)
    for j, name in enumerate(names):
        nonzero_count = 0
        for i in range(T):
            day = window_start + i
            pnl = per_strat_daily[name].get(day, 0.0)
            if pnl != 0.0:
                nonzero_count += 1
            returns[i, j] = pnl / equity
        if nonzero_count < _MIN_LW_OBSERVATIONS:
            return None

    return returns


# ══════════════════════════════════════════════════════════════════════
# Allocator
# ══════════════════════════════════════════════════════════════════════


class Allocator:
    """HRP-lite allocator with Ledoit-Wolf shrinkage and mode caps."""

    def __init__(
        self,
        *,
        mode: ModeConfig,
        tracker: PortfolioTracker,
        signal_corr_cluster_threshold: float = 0.7,
        return_window_days: int = _DEFAULT_RETURN_WINDOW_DAYS,
    ) -> None:
        self._mode = mode
        self._tracker = tracker
        self._corr_threshold = float(signal_corr_cluster_threshold)
        self._return_window_days = int(return_window_days)

    @property
    def mode(self) -> ModeConfig:
        return self._mode

    def set_mode(self, mode: ModeConfig) -> None:
        log.info("m3s.allocator.mode_change", old=self._mode.name.value, new=mode.name.value)
        self._mode = mode

    # ── Public entrypoint ───────────────────────────────────────────

    def compute(self, snapshot: PortfolioSnapshot) -> AllocationDecision:
        names = sorted(snapshot.per_strategy.keys())
        if not names:
            return AllocationDecision(
                ts_ms=snapshot.ts_ms,
                weights={},
                method="empty",
                inputs_hash=self._hash_inputs([], snapshot),
                reasoning="no strategies in snapshot",
            )

        # Cold-start path: any strategy under the min-trades threshold.
        min_trades = self._mode.cold_start_trades_required
        cold = any(
            snapshot.per_strategy[n].n_trades_30d < min_trades for n in names
        )

        if cold:
            return self._equal_weight(names, snapshot)

        return self._hrp_lite(names, snapshot)

    # ── Cold-start ──────────────────────────────────────────────────

    def _equal_weight(
        self,
        names: list[str],
        snapshot: PortfolioSnapshot,
    ) -> AllocationDecision:
        n = len(names)
        raw = {name: 1.0 / n for name in names}
        capped = self._apply_caps(raw, clusters=[[name] for name in names])
        return AllocationDecision(
            ts_ms=snapshot.ts_ms,
            weights=capped,
            method="cold_start_equal",
            inputs_hash=self._hash_inputs(names, snapshot),
            reasoning=f"cold start: equal weight across {n} strategies",
        )

    # ── Mature path: HRP-lite ───────────────────────────────────────

    def _hrp_lite(
        self,
        names: list[str],
        snapshot: PortfolioSnapshot,
    ) -> AllocationDecision:
        # Build raw return matrix from tracker.
        ret_matrix = build_return_matrix(
            self._tracker,
            names,
            now_ms=snapshot.ts_ms,
            window_days=self._return_window_days,
        )

        # Tier 1 #4: shrink if we have enough data, otherwise fall back
        # to per-strategy vol from the snapshot directly.
        used_lw = False
        delta_applied = 0.0
        if ret_matrix is not None:
            Sigma, delta_applied = ledoit_wolf_shrinkage(ret_matrix)
            used_lw = True
            vols = np.sqrt(np.maximum(np.diag(Sigma), 1e-12))
            # Derive a correlation view of the shrunk covariance
            shrunk_corr_matrix = cov_to_corr(Sigma)
        else:
            vols = np.array([
                max(snapshot.per_strategy[n].realized_vol_30d, 1e-6)
                for n in names
            ])
            shrunk_corr_matrix = None

        # Cluster: prefer the LW-shrunk correlation from the return matrix
        # if available; otherwise fall back to the snapshot's signal_corr
        # (bar-level exposure correlation from the tracker).
        clusters = self._cluster_strategies(
            names,
            snapshot=snapshot,
            shrunk_corr_matrix=shrunk_corr_matrix,
        )

        # Intra-cluster inverse-vol, then across-cluster inverse-vol.
        raw_weights: dict[str, float] = {}
        cluster_agg_vols: list[float] = []
        for cluster in clusters:
            idx = [names.index(n) for n in cluster]
            cluster_vols = vols[idx]
            inv_vol = 1.0 / cluster_vols
            norm = inv_vol / inv_vol.sum()
            for k, nm in enumerate(cluster):
                raw_weights[nm] = float(norm[k])
            # RMS aggregate for the cluster's weight (treat cluster as a meta-asset)
            cluster_agg_vols.append(float(math.sqrt((cluster_vols ** 2).sum())))

        cluster_agg = np.array(cluster_agg_vols)
        inv_cluster = 1.0 / np.maximum(cluster_agg, 1e-12)
        cluster_scale = inv_cluster / inv_cluster.sum()

        scaled: dict[str, float] = {}
        for ci, cluster in enumerate(clusters):
            for nm in cluster:
                scaled[nm] = raw_weights[nm] * float(cluster_scale[ci])

        capped = self._apply_caps(scaled, clusters=clusters)

        reasoning = (
            f"hrp_lite: {len(clusters)} cluster(s); "
            f"lw={'on' if used_lw else 'off'} delta={delta_applied:.3f}; "
            f"caps: strat={self._mode.max_per_strategy_cap:.2f}, "
            f"cluster={self._mode.max_cluster_cap:.2f}"
        )
        return AllocationDecision(
            ts_ms=snapshot.ts_ms,
            weights=capped,
            method="hrp_lite_ledoit_wolf" if used_lw else "hrp_lite_inv_vol",
            inputs_hash=self._hash_inputs(names, snapshot),
            reasoning=reasoning,
        )

    # ── Clustering ──────────────────────────────────────────────────

    def _cluster_strategies(
        self,
        names: list[str],
        *,
        snapshot: PortfolioSnapshot,
        shrunk_corr_matrix: np.ndarray | None,
    ) -> list[list[str]]:
        """Partition strategies into clusters by correlation > threshold.

        Greedy single-linkage: for each unassigned strategy, start a new
        cluster and pull in any still-unassigned strategy whose correlation
        to ANY member of the cluster exceeds the threshold.
        """
        unassigned = set(names)
        clusters: list[list[str]] = []

        def corr(a: str, b: str) -> float:
            if shrunk_corr_matrix is not None:
                ia = names.index(a)
                ib = names.index(b)
                return float(shrunk_corr_matrix[ia, ib])
            # Fall back to snapshot signal_corr (sorted key)
            key = f"{a}|{b}" if a < b else f"{b}|{a}"
            return snapshot.signal_corr.get(key, 0.0)

        for seed in sorted(unassigned):  # deterministic order
            if seed not in unassigned:
                continue
            cluster = [seed]
            unassigned.discard(seed)
            changed = True
            while changed:
                changed = False
                for candidate in sorted(unassigned):
                    if any(corr(candidate, member) >= self._corr_threshold for member in cluster):
                        cluster.append(candidate)
                        unassigned.discard(candidate)
                        changed = True
            clusters.append(cluster)
        return clusters

    # ── Caps + normalization ────────────────────────────────────────

    def _apply_caps(
        self,
        raw: dict[str, float],
        *,
        clusters: list[list[str]],
    ) -> dict[str, float]:
        """Clamp per-strategy and per-cluster caps; spill residual to cash.

        Simple iterative clipper: clamp each strategy to its cap, redistribute
        the clipped excess within the cluster, then clamp each cluster's
        aggregate to its cap and drop any residual (becomes cash buffer).
        """
        per_strat_cap = self._mode.max_per_strategy_cap
        cluster_cap = self._mode.max_cluster_cap

        weights: dict[str, float] = dict(raw)

        # (1) Per-strategy clip within each cluster; redistribute excess.
        for cluster in clusters:
            for _ in range(10):  # converges in practice in ≤N iterations
                over = [n for n in cluster if weights[n] > per_strat_cap]
                if not over:
                    break
                excess = sum(weights[n] - per_strat_cap for n in over)
                for n in over:
                    weights[n] = per_strat_cap
                under = [n for n in cluster if weights[n] < per_strat_cap]
                if not under:
                    # All at cap; excess becomes cash for this cluster
                    break
                under_sum = sum(weights[n] for n in under)
                if under_sum <= 0:
                    break
                for n in under:
                    weights[n] += excess * (weights[n] / under_sum)

        # (2) Per-cluster cap: if cluster total > cap, scale cluster members
        #     down proportionally. Residual becomes cash.
        for cluster in clusters:
            total = sum(weights[n] for n in cluster)
            if total > cluster_cap and total > 0:
                factor = cluster_cap / total
                for n in cluster:
                    weights[n] *= factor

        # (3) Final sanity: non-negative, clamp NaN to zero.
        for n in list(weights.keys()):
            w = weights[n]
            if not math.isfinite(w) or w < 0.0:
                weights[n] = 0.0

        # (4) Do not re-normalize to 1.0 — any residual is cash buffer.
        return weights

    # ── Audit ───────────────────────────────────────────────────────

    def _hash_inputs(
        self,
        names: list[str],
        snapshot: PortfolioSnapshot,
    ) -> str:
        """Stable hash of allocator inputs for audit log traceability."""
        payload = {
            "mode": self._mode.name.value,
            "names": names,
            "ts_ms": snapshot.ts_ms,
            "per_strategy": {
                n: {
                    "vol": round(snapshot.per_strategy[n].realized_vol_30d, 6),
                    "sharpe": round(snapshot.per_strategy[n].rolling_sharpe_30d, 6),
                    "n_trades": snapshot.per_strategy[n].n_trades_30d,
                }
                for n in names
            },
            "signal_corr": {k: round(v, 6) for k, v in sorted(snapshot.signal_corr.items())},
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]
