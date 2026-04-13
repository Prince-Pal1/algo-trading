"""Meta-labeling signal filter (Phase 3c).

Phase 0 scope: audit plumbing only. No classifier yet.

- `features.build_meta_features(signal, features, snapshot)` → feature dict at signal time
- `labels.triple_barrier_label(...)` → AFML triple-barrier labeler
- `audit.audit_signal(...) / audit_close(...)` → SQLite write-through

See `docs/planning/phase3c_meta_labeling_plan.md` for the full rollout plan.
"""

from __future__ import annotations

from src.m3s.signal_filter.audit import audit_close, audit_signal
from src.m3s.signal_filter.features import (
    FEATURE_KEYS,
    build_meta_features,
)
from src.m3s.signal_filter.labels import (
    BarrierHit,
    TripleBarrierResult,
    meta_label_from_outcome,
    triple_barrier_label,
)

__all__ = [
    "audit_signal",
    "audit_close",
    "build_meta_features",
    "FEATURE_KEYS",
    "triple_barrier_label",
    "meta_label_from_outcome",
    "BarrierHit",
    "TripleBarrierResult",
]
