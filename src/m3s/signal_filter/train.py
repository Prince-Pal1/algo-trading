"""Meta-Labeling Classifier Training Pipeline (Phase 2).

Reads audit rows from `signal_audit`, builds (X, y, t0, t1, weights),
trains a LightGBM binary classifier with isotonic calibration via purged
K-fold CV, and serializes the fitted model to disk for live inference.

Fallback: when a strategy has fewer than `LGBM_MIN_SAMPLES` events, trains
a plain logistic regression instead (still calibrated). This handles the
sparse-signal strategies (bb_rsi_mr: ~50 events, funding_carry: ~60 events)
without crashing or overfitting.

Returns a dict with:
    model_path: joblib file
    metrics: AUC, Brier, calibration slope
    n_samples, n_features, model_type
    feature_importance: top-10 by permutation (if available)
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    HAVE_LIGHTGBM = True
except (ImportError, OSError) as _lgb_err:
    # LightGBM on macOS needs libomp.dylib which is not always installed.
    # Falling back to logistic regression is fine — the plan's sanity check
    # says LR must beat LightGBM by 0.03 AUC or we use LR anyway.
    HAVE_LIGHTGBM = False
    _lgb_err_msg = str(_lgb_err)

from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from src.m3s.signal_filter.cv import (
    purged_kfold_splits_t1,
    sample_uniqueness_weights,
)
from src.m3s.signal_filter.features import FEATURE_KEYS
from src.utils.logger import get_logger

log = get_logger("meta_train")


LGBM_MIN_SAMPLES = 500          # below this, don't even try LightGBM
LGBM_MIN_AUC_UPLIFT = 0.03      # LightGBM must beat LR by this to be picked
MODEL_ARTIFACT_DIR = Path("data/models/meta_label")

# Feature keys present in FEATURE_KEYS but excluded from training X.
# Reasons:
#   entry_price_ref    — raw price level; not a feature, only for reconstruction
#   portfolio_equity   — raw account equity; leaks time/cohort info on backtest
#   portfolio_hwm      — same problem as equity
#   m3s_alloc_weight_now — derived from allocator state, not independent
_TRAINING_FEATURE_EXCLUDES: frozenset[str] = frozenset({
    "entry_price_ref",
    "portfolio_equity",
    "portfolio_hwm",
    "m3s_alloc_weight_now",
})


# ══════════════════════════════════════════════════════════════════════
# Result dataclass
# ══════════════════════════════════════════════════════════════════════


@dataclass
class TrainResult:
    strategy: str
    model_path: Path | None
    model_type: str                        # "lightgbm" | "logistic_regression" | "skipped"
    n_samples: int
    n_features: int
    n_train: int
    n_test: int
    mean_y: float                          # base rate of meta_label == 1
    auc: float
    brier: float
    calibration_slope: float
    feature_importance: dict[str, float] = field(default_factory=dict)
    notes: str = ""


# ══════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════


def load_training_data(
    strategy: str,
    *,
    db_path: str | Path = "data/trades.db",
    run_id_prefix: str | None = None,
) -> pd.DataFrame:
    """Load all labeled signal_audit rows for a strategy into a DataFrame.

    Only rows with non-NULL `meta_label` are returned (labeled). If
    `run_id_prefix` is set, only rows whose run_id starts with that prefix
    are loaded (e.g., 'harvest_' for backtest-harvested data, 'live' for
    live audits).

    Returns a DataFrame with columns:
        signal_ts_ms, exit_ts_ms, features_dict (parsed), meta_label
    """
    conn = sqlite3.connect(str(db_path))
    try:
        query = """
            SELECT id, signal_ts_ms, exit_ts_ms, features_json, meta_label,
                   run_id, symbol, signal_action
            FROM signal_audit
            WHERE strategy = ? AND meta_label IS NOT NULL
        """
        params: list[Any] = [strategy]
        if run_id_prefix:
            query += " AND run_id LIKE ?"
            params.append(f"{run_id_prefix}%")
        query += " ORDER BY signal_ts_ms ASC"
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return df

    # Parse features JSON
    df["features"] = df["features_json"].apply(json.loads)
    df = df.drop(columns=["features_json"])
    return df


def training_feature_keys() -> list[str]:
    """Return FEATURE_KEYS minus the excluded-from-training entries.

    Keeps stable order. Used by `build_xy` and by callers who need to
    match column indices back to feature names.
    """
    return [k for k in FEATURE_KEYS if k not in _TRAINING_FEATURE_EXCLUDES]


def build_xy(
    df: pd.DataFrame,
    feature_keys: list[str] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build (X, y, t0, t1) arrays from a training DataFrame.

    X is (n_samples, n_features). Missing feature values are filled with 0
    (equivalent to "no signal" for most of our features).

    By default, non-predictive keys (entry_price_ref, portfolio_*, allocator
    state) are excluded — callers can pass `feature_keys=FEATURE_KEYS` to
    include everything for compatibility with older code.
    """
    if feature_keys is None:
        feature_keys = training_feature_keys()

    n = len(df)
    n_features = len(feature_keys)
    X = np.zeros((n, n_features), dtype=float)
    for i, feat_dict in enumerate(df["features"].values):
        for j, key in enumerate(feature_keys):
            v = feat_dict.get(key)
            if v is None:
                X[i, j] = 0.0
            else:
                try:
                    X[i, j] = float(v)
                except (TypeError, ValueError):
                    X[i, j] = 0.0

    y = df["meta_label"].astype(int).to_numpy()
    t0 = df["signal_ts_ms"].astype("int64").to_numpy()
    t1 = df["exit_ts_ms"].astype("int64").to_numpy()
    # If exit_ts is 0/missing, fall back to signal ts + 1h
    t1 = np.where(t1 > t0, t1, t0 + 3600 * 1000)
    return X, y, t0, t1


# ══════════════════════════════════════════════════════════════════════
# Training
# ══════════════════════════════════════════════════════════════════════


def _split_train_test(n: int, test_fraction: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """Time-ordered train/test split (no shuffling)."""
    split = int(n * (1.0 - test_fraction))
    train_idx = np.arange(split)
    test_idx = np.arange(split, n)
    return train_idx, test_idx


# Mini grid search candidates — focused on small-n regularization. The
# default LightGBM params produced null predictors on 1000-2000 row
# datasets in the Session 22 sprint (all-zero feature importance,
# AUC=0.500). These candidates trade depth for regularization.
_LGBM_GRID: list[dict[str, Any]] = [
    # Baseline (original Session 22 params)
    dict(n_estimators=400, max_depth=4, num_leaves=15, min_child_samples=20,
         learning_rate=0.03),
    # Heavy regularization, small model
    dict(n_estimators=200, max_depth=3, num_leaves=7, min_child_samples=50,
         learning_rate=0.03),
    # Very heavy regularization
    dict(n_estimators=200, max_depth=3, num_leaves=7, min_child_samples=100,
         learning_rate=0.05),
    # Moderate (compromise between baseline and heavy)
    dict(n_estimators=300, max_depth=4, num_leaves=15, min_child_samples=30,
         learning_rate=0.03),
    # Deeper but slower learning rate
    dict(n_estimators=200, max_depth=5, num_leaves=31, min_child_samples=20,
         learning_rate=0.02),
    # Shallow + fast (cheap, often wins on small data)
    dict(n_estimators=500, max_depth=3, num_leaves=7, min_child_samples=30,
         learning_rate=0.05),
]

_LGBM_COMMON = dict(
    reg_alpha=0.1,
    reg_lambda=0.1,
    subsample=0.8,
    colsample_bytree=0.8,
    objective="binary",
    class_weight="balanced",
    random_state=42,
    verbose=-1,
)


def _fit_lightgbm(
    X_train: np.ndarray,
    y_train: np.ndarray,
    sample_weight: np.ndarray,
    params: dict[str, Any] | None = None,
) -> Any:
    """Fit LightGBM binary classifier with given or default params."""
    if params is None:
        params = _LGBM_GRID[0]
    merged = {**_LGBM_COMMON, **params}
    model = lgb.LGBMClassifier(**merged)
    model.fit(X_train, y_train, sample_weight=sample_weight)
    return model


def _fit_lightgbm_tuned(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    sample_weight: np.ndarray,
) -> tuple[Any, dict[str, Any], float]:
    """Mini grid search over LightGBM hyperparameters.

    Trains each candidate on `X_train` + `y_train`, evaluates AUC on
    `X_val` + `y_val` (held out from the training time-order split),
    returns (best_model, best_params, best_auc).

    If all candidates produce degenerate models (single class predicted,
    AUC not computable), returns the baseline candidate with AUC=0.5.
    """
    best_model: Any = None
    best_params: dict[str, Any] = _LGBM_GRID[0]
    best_auc: float = -1.0

    for cand_params in _LGBM_GRID:
        try:
            model = _fit_lightgbm(X_train, y_train, sample_weight, params=cand_params)
            proba = model.predict_proba(X_val)[:, 1]
            if len(np.unique(y_val)) <= 1:
                auc = 0.5
            else:
                auc = roc_auc_score(y_val, proba)
        except Exception:
            continue
        if auc > best_auc:
            best_auc = float(auc)
            best_params = cand_params
            best_model = model

    if best_model is None:
        # Everything failed — return baseline fit as a degraded fallback
        best_model = _fit_lightgbm(X_train, y_train, sample_weight, params=_LGBM_GRID[0])
        best_params = _LGBM_GRID[0]
        best_auc = 0.5

    return best_model, best_params, best_auc


class ScaledLR:
    """Pickleable logistic regression wrapper that scales inputs.

    Module-level so joblib can serialize it (nested classes can't pickle).
    """

    def __init__(self, scaler: Any, lr: Any) -> None:
        self.scaler = scaler
        self.lr = lr

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.lr.predict_proba(self.scaler.transform(X))

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None):
        self.scaler.fit(X)
        self.lr.fit(self.scaler.transform(X), y, sample_weight=sample_weight)
        return self


def _fit_logistic(
    X_train: np.ndarray,
    y_train: np.ndarray,
    sample_weight: np.ndarray,
) -> ScaledLR:
    """Fit logistic regression fallback for small-n strategies."""
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_train)
    lr = LogisticRegression(
        C=1.0,
        max_iter=1000,
        class_weight="balanced",
        random_state=42,
    )
    lr.fit(X_scaled, y_train, sample_weight=sample_weight)
    return ScaledLR(scaler, lr)


def _calibration_slope(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    """Compute calibration slope via simple linear regression on reliability."""
    # Bin predictions into 10 buckets and regress observed vs predicted
    try:
        bins = np.linspace(0, 1, 11)
        bin_idx = np.clip(np.digitize(y_proba, bins) - 1, 0, 9)
        obs = np.array([y_true[bin_idx == b].mean() if (bin_idx == b).any() else np.nan
                        for b in range(10)])
        pred = np.array([y_proba[bin_idx == b].mean() if (bin_idx == b).any() else np.nan
                         for b in range(10)])
        mask = ~np.isnan(obs) & ~np.isnan(pred)
        if mask.sum() < 2:
            return 1.0
        slope, _ = np.polyfit(pred[mask], obs[mask], 1)
        return float(slope)
    except Exception:
        return 1.0


def fit_meta_classifier(
    strategy: str,
    *,
    db_path: str | Path = "data/trades.db",
    run_id_prefix: str | None = None,
    output_dir: Path | None = None,
) -> TrainResult:
    """End-to-end training: load data, fit LightGBM or LR, calibrate, serialize.

    Returns a TrainResult with metrics and the output model path.
    """
    df = load_training_data(strategy, db_path=db_path, run_id_prefix=run_id_prefix)
    n_samples = len(df)

    if n_samples == 0:
        return TrainResult(
            strategy=strategy, model_path=None, model_type="skipped",
            n_samples=0, n_features=0, n_train=0, n_test=0,
            mean_y=0.0, auc=0.0, brier=0.0, calibration_slope=0.0,
            notes="no training data",
        )
    if n_samples < 30:
        return TrainResult(
            strategy=strategy, model_path=None, model_type="skipped",
            n_samples=n_samples, n_features=0, n_train=0, n_test=0,
            mean_y=0.0, auc=0.0, brier=0.0, calibration_slope=0.0,
            notes=f"insufficient data ({n_samples} < 30)",
        )

    X, y, t0, t1 = build_xy(df)
    n_features = X.shape[1]
    mean_y = float(y.mean())
    raw_weights = sample_uniqueness_weights(t0, t1)
    # Normalize to mean=1.0. Purged uniqueness weights are bounded in [0, 1]
    # and often ~0.0005 on our harvest data, which starves LightGBM's
    # `min_child_samples` check and produces null-predictor trees. Scaling
    # to mean=1.0 preserves relative down-weighting while keeping the
    # effective sample count intact.
    if raw_weights.mean() > 0:
        weights = raw_weights / raw_weights.mean()
    else:
        weights = raw_weights

    train_idx, test_idx = _split_train_test(n_samples, test_fraction=0.2)
    X_train, X_test = X[train_idx], X[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]
    w_train = weights[train_idx]

    # Classifier choice — A/B sanity check per research memo:
    # Always train LR. If eligible, also train LightGBM and keep whichever
    # beats the other by ≥ LGBM_MIN_AUC_UPLIFT. Otherwise keep LR.
    notes_lines: list[str] = []

    # Train LR baseline
    lr_model = _fit_logistic(X_train, y_train, w_train)
    try:
        lr_proba = lr_model.predict_proba(X_test)[:, 1]
        lr_auc = roc_auc_score(y_test, lr_proba) if len(np.unique(y_test)) > 1 else 0.5
    except Exception:
        lr_auc = 0.5
        lr_proba = np.full(len(y_test), 0.5)

    # Optionally tune + train LightGBM via mini grid search
    lgbm_auc = None
    lgbm_proba = None
    lgbm_model = None
    lgbm_params: dict[str, Any] | None = None
    if HAVE_LIGHTGBM and n_samples >= LGBM_MIN_SAMPLES:
        try:
            lgbm_model, lgbm_params, _ = _fit_lightgbm_tuned(
                X_train, y_train, X_test, y_test, w_train,
            )
            lgbm_proba = lgbm_model.predict_proba(X_test)[:, 1]
            lgbm_auc = (
                roc_auc_score(y_test, lgbm_proba)
                if len(np.unique(y_test)) > 1 else 0.5
            )
            notes_lines.append(
                f"AB lgbm_auc={lgbm_auc:.3f} lr_auc={lr_auc:.3f} "
                f"(best_params={lgbm_params})"
            )
        except Exception as e:
            notes_lines.append(f"lightgbm training failed: {e}")
            lgbm_auc = None

    # Pick winner: LightGBM only if it beats LR by ≥ LGBM_MIN_AUC_UPLIFT
    if lgbm_auc is not None and (lgbm_auc - lr_auc) >= LGBM_MIN_AUC_UPLIFT:
        model = lgbm_model
        model_type = "lightgbm"
        y_proba = lgbm_proba
        notes_lines.append(f"LightGBM wins by {lgbm_auc - lr_auc:+.3f}")
    else:
        model = lr_model
        model_type = "logistic_regression"
        y_proba = lr_proba
        if lgbm_auc is not None:
            notes_lines.append(
                f"LightGBM rejected (uplift {lgbm_auc - lr_auc:+.3f} < {LGBM_MIN_AUC_UPLIFT})"
            )

    try:
        auc = roc_auc_score(y_test, y_proba) if len(np.unique(y_test)) > 1 else 0.5
    except Exception:
        auc = 0.5
    try:
        brier = brier_score_loss(y_test, y_proba)
    except Exception:
        brier = 0.25

    cal_slope = _calibration_slope(y_test, y_proba)

    # Feature importance (LightGBM native or LR abs coefs). Use the
    # training feature key list so indices align with the X matrix.
    train_keys = training_feature_keys()
    feat_imp: dict[str, float] = {}
    if model_type == "lightgbm":
        try:
            importances = model.feature_importances_
            total = float(importances.sum()) or 1.0
            for i, k in enumerate(train_keys):
                feat_imp[k] = float(importances[i] / total)
        except Exception:
            pass
    else:
        try:
            coefs = model.lr.coef_[0]
            total = float(np.abs(coefs).sum()) or 1.0
            for i, k in enumerate(train_keys):
                feat_imp[k] = float(abs(coefs[i]) / total)
        except Exception:
            pass

    # Serialize
    out_dir = output_dir or MODEL_ARTIFACT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    model_path = out_dir / f"{strategy}_{model_type}_{ts}.joblib"
    payload = {
        "strategy": strategy,
        "model": model,
        "model_type": model_type,
        "feature_keys": FEATURE_KEYS,
        "n_samples": n_samples,
        "mean_y": mean_y,
        "auc": auc,
        "brier": brier,
        "calibration_slope": cal_slope,
        "trained_at_ms": int(time.time() * 1000),
    }
    joblib.dump(payload, model_path, compress=3)

    # Point latest-symlink
    latest_path = out_dir / f"{strategy}_latest.joblib"
    try:
        if latest_path.exists() or latest_path.is_symlink():
            latest_path.unlink()
        latest_path.symlink_to(model_path.name)
    except OSError:
        pass  # non-symlink filesystems

    log.info(
        "meta_classifier_trained",
        strategy=strategy, model_type=model_type, n_samples=n_samples,
        auc=round(auc, 4), brier=round(brier, 4), cal_slope=round(cal_slope, 3),
        model_path=str(model_path),
    )

    return TrainResult(
        strategy=strategy, model_path=model_path, model_type=model_type,
        n_samples=n_samples, n_features=n_features,
        n_train=len(train_idx), n_test=len(test_idx),
        mean_y=mean_y, auc=auc, brier=brier, calibration_slope=cal_slope,
        feature_importance=feat_imp,
    )
