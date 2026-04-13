"""MetaLabelFilter — live inference layer for Phase 3.

Loads a trained classifier artifact (joblib) from `data/models/meta_label/`
and exposes `on_signal(signal, features, snapshot) → (signal|None, FilterDecision)`.

**Filter semantics:**
- Never up-scales. Output multiplier is in [0, 1].
- `shadow_mode=True` (default): logs decision, returns signal unchanged
- `shadow_mode=False`:
    - If `p < veto_threshold`: return None (veto the signal)
    - Else: multiply `signal.risk_pct` by `sigmoid_scale(p)` and return

**Model loading:** lazy on first call. Hot reload happens when the
underlying joblib file's mtime changes — the filter watches the model
file every `reload_interval_seconds`.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from src.m3s.signal_filter.features import FEATURE_KEYS, build_meta_features
from src.m3s.types import PortfolioSnapshot
from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

log = get_logger("meta_label_filter")


@dataclass(frozen=True)
class FilterDecision:
    """Audit trail of one filter decision for logging + downstream analysis."""
    strategy: str
    probability: float
    threshold: float
    size_mult: float           # multiplier applied to risk_pct (or would have been in shadow)
    decision: str              # "pass" | "veto" | "scale"
    shadow_mode: bool
    model_type: str
    model_path: str


class MetaLabelFilter:
    """Per-strategy meta-label filter.

    Usage:
        filt = MetaLabelFilter(model_dir="data/models/meta_label", shadow_mode=True)
        filtered_sig, decision = filt.on_signal(sig, features, snapshot)
    """

    def __init__(
        self,
        *,
        model_dir: str | Path = "data/models/meta_label",
        shadow_mode: bool = True,
        veto_threshold: float = 0.30,
        scale_sharpness: float = 8.0,    # steeper = harder veto around threshold
        scale_mode: bool = False,        # if True, use continuous scaling instead of veto
        reload_interval_seconds: int = 60,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._shadow_mode = bool(shadow_mode)
        self._veto_threshold = float(veto_threshold)
        self._scale_sharpness = float(scale_sharpness)
        self._scale_mode = bool(scale_mode)
        self._reload_interval = int(reload_interval_seconds)

        # Per-strategy loaded models
        self._models: dict[str, dict[str, Any]] = {}
        self._mtimes: dict[str, float] = {}
        self._last_reload_check: float = 0.0

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    def set_shadow_mode(self, value: bool) -> None:
        log.info("meta_label_shadow_mode_set", shadow=bool(value))
        self._shadow_mode = bool(value)

    # ── Model loading ───────────────────────────────────────────────

    def _latest_path(self, strategy: str) -> Path | None:
        """Return the latest model path for a strategy, or None if missing."""
        # Prefer `<strategy>_latest.joblib` symlink, fall back to newest mtime
        latest = self._model_dir / f"{strategy}_latest.joblib"
        if latest.exists():
            return latest
        candidates = sorted(self._model_dir.glob(f"{strategy}_*.joblib"))
        if not candidates:
            return None
        return candidates[-1]

    def _load_model(self, strategy: str) -> dict[str, Any] | None:
        path = self._latest_path(strategy)
        if path is None:
            return None
        try:
            payload = joblib.load(path)
            payload["_path"] = str(path)
            self._models[strategy] = payload
            try:
                self._mtimes[strategy] = path.stat().st_mtime
            except OSError:
                self._mtimes[strategy] = 0.0
            log.info("meta_model_loaded", strategy=strategy, path=str(path),
                     model_type=payload.get("model_type"),
                     auc=payload.get("auc"))
            return payload
        except Exception as e:
            log.warning("meta_model_load_failed", strategy=strategy, error=str(e))
            return None

    def _maybe_reload(self, strategy: str) -> dict[str, Any] | None:
        """Check if the model file changed since last load; reload if so."""
        now = time.time()
        if (now - self._last_reload_check) < self._reload_interval:
            return self._models.get(strategy)
        self._last_reload_check = now

        path = self._latest_path(strategy)
        if path is None:
            return self._models.get(strategy)
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return self._models.get(strategy)

        last = self._mtimes.get(strategy, 0.0)
        if current_mtime > last:
            return self._load_model(strategy)
        return self._models.get(strategy)

    # ── Inference ──────────────────────────────────────────────────

    def _feature_row(
        self,
        signal: Signal,
        features: pd.Series | None,
        snapshot: PortfolioSnapshot | None,
    ) -> np.ndarray:
        feat_dict = build_meta_features(signal, features, snapshot)
        row = np.zeros((1, len(FEATURE_KEYS)), dtype=float)
        for j, key in enumerate(FEATURE_KEYS):
            v = feat_dict.get(key)
            if v is None:
                row[0, j] = 0.0
            else:
                try:
                    row[0, j] = float(v)
                except (TypeError, ValueError):
                    row[0, j] = 0.0
        return row

    def _compute_size_mult(self, p: float) -> tuple[float, str]:
        """Map probability → (multiplier, decision_label)."""
        if self._scale_mode:
            # Continuous scaling: sigmoid((p - 0.5) * k) in [0, 1]
            x = (p - 0.5) * self._scale_sharpness
            mult = 1.0 / (1.0 + math.exp(-x))
            mult = max(0.0, min(1.0, mult))
            return mult, "scale"
        # Binary veto mode
        if p < self._veto_threshold:
            return 0.0, "veto"
        return 1.0, "pass"

    def on_signal(
        self,
        signal: Signal,
        features: pd.Series | None = None,
        snapshot: PortfolioSnapshot | None = None,
    ) -> tuple[Signal | None, FilterDecision | None]:
        """Evaluate the filter on a signal.

        Returns (sig_or_None, decision). In shadow mode, sig is always the
        original (pass-through). In non-shadow mode, sig may be None
        (veto), unchanged (pass), or mutated (risk_pct scaled).
        """
        # Only evaluate entries (LONG/SHORT). CLOSE/HOLD pass through.
        if signal.action not in (SignalAction.LONG, SignalAction.SHORT):
            return signal, None

        strategy = signal.strategy_name or ""
        payload = self._maybe_reload(strategy)
        if payload is None:
            # No model available — pass through silently (happens early
            # in the Phase 1 wall-clock window before any training has run)
            return signal, None

        model = payload["model"]
        X = self._feature_row(signal, features, snapshot)

        try:
            proba = float(model.predict_proba(X)[0, 1])
        except Exception as e:
            log.warning("meta_predict_failed", strategy=strategy, error=str(e))
            return signal, None

        size_mult, label = self._compute_size_mult(proba)

        decision = FilterDecision(
            strategy=strategy,
            probability=proba,
            threshold=self._veto_threshold,
            size_mult=size_mult,
            decision=label,
            shadow_mode=self._shadow_mode,
            model_type=str(payload.get("model_type", "")),
            model_path=str(payload.get("_path", "")),
        )

        log.info(
            "meta_filter_decision",
            strategy=strategy,
            p=round(proba, 4),
            decision=label,
            mult=round(size_mult, 3),
            shadow=self._shadow_mode,
        )

        if self._shadow_mode:
            return signal, decision

        if label == "veto":
            return None, decision

        # Apply size scaling
        if signal.risk_pct is not None:
            new_risk = max(0.0, signal.risk_pct * size_mult)
            signal.risk_pct = new_risk
        return signal, decision
