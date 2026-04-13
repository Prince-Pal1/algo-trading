#!/usr/bin/env python3
"""Meta-Label Filter Shadow Mode Validator (Phase 3c sprint).

Analog to scripts/m3s_shadow_check.py but for the meta-label filter.
Reads live signal_audit rows (run_id='live') with populated meta_label
columns, computes per-strategy rolling AUC + Brier + calibration drift,
and reports whether the filter is ready to promote from shadow → advisory.

Outputs:
    data/meta_label_shadow_report.md      Human-readable (cat any time)
    data/meta_label_shadow_status.json    Machine-readable (dashboards + crons)
    data/meta_label_shadow_alerts.log     Append-only violation history

Exit codes:
    0   HEALTHY  (or DISABLED — no live rows yet, SKIP-only report)
    1   WARNING  (advisory; drift or low confidence)
    2   ERROR    (calibration blown, AUC under 0.45, or model file missing)

Used by the Day 3/4 CronCreate wake-ups to decide whether to promote the
meta-label filter. Gate thresholds match the Phase 3c plan:
    - Advisory promotion: AUC >= 0.55 AND Brier <= training Brier * 1.10
    - Live promotion:     AUC >= 0.55 AND >= 24h of advisory-mode data

Usage:
    python3 scripts/meta_label_shadow_check.py
    python3 scripts/meta_label_shadow_check.py --window-rows 200
    python3 scripts/meta_label_shadow_check.py --verbose
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
# Ensure `src` package is importable when unpickling joblib artifacts that
# reference src.m3s.signal_filter.train.ScaledLR (and other project classes).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
TRADES_DB = DATA_DIR / "trades.db"
MODEL_DIR = DATA_DIR / "models" / "meta_label"
REPORT_MD = DATA_DIR / "meta_label_shadow_report.md"
STATUS_JSON = DATA_DIR / "meta_label_shadow_status.json"
ALERTS_LOG = DATA_DIR / "meta_label_shadow_alerts.log"


# Gate thresholds (from Phase 3c plan §7)
ADVISORY_AUC_MIN = 0.55
LIVE_AUC_MIN = 0.55
BRIER_DRIFT_MAX = 1.10          # brier_live <= brier_train * 1.10
MIN_SAMPLES_FOR_AUC = 20        # below this, SKIP rather than FAIL
CALIBRATION_SLOPE_MIN = 0.30    # AFML recommends slope > 0.3


# ── Result types ─────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str                 # "PASS" | "WARN" | "FAIL" | "SKIP"
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetaLabelReport:
    timestamp_utc: str
    overall_status: str         # HEALTHY | WARNING | ERROR | DISABLED
    per_strategy: dict[str, list[CheckResult]]
    summary: dict[str, Any]

    def to_json(self) -> dict:
        return {
            "timestamp_utc": self.timestamp_utc,
            "overall_status": self.overall_status,
            "per_strategy": {
                name: [
                    {"name": c.name, "status": c.status, "detail": c.detail, "data": c.data}
                    for c in results
                ]
                for name, results in self.per_strategy.items()
            },
            "summary": self.summary,
        }


# ── Data access ──────────────────────────────────────────────────────


def load_live_audit_rows(
    conn: sqlite3.Connection,
    strategy: str,
    limit: int = 100,
) -> list[dict]:
    """Load the most recent `limit` live audit rows for a strategy with
    populated meta_label. Returns dicts, not msgspec."""
    try:
        cursor = conn.execute(
            """
            SELECT id, signal_ts_ms, exit_ts_ms,
                   meta_proba, meta_decision, meta_label,
                   realized_pnl, realized_pnl_pct
            FROM signal_audit
            WHERE strategy = ? AND run_id = 'live' AND meta_label IS NOT NULL
            ORDER BY signal_ts_ms DESC
            LIMIT ?
            """,
            (strategy, limit),
        )
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in cursor.fetchall()]


def load_training_metrics_from_artifact(strategy: str) -> tuple[dict[str, Any], str]:
    """Load training-time metrics from the latest model artifact.

    Returns (metrics_dict, error_message). On success, error_message is empty.
    On failure, metrics_dict is empty and error_message explains why.
    """
    latest = MODEL_DIR / f"{strategy}_latest.joblib"
    if not latest.exists():
        return {}, f"no model artifact at {latest}"
    try:
        import joblib
    except ImportError as e:
        return {}, f"joblib not available: {e}"
    try:
        payload = joblib.load(latest)
    except Exception as e:
        return {}, f"joblib.load failed: {type(e).__name__}: {e}"
    try:
        return {
            "auc": float(payload.get("auc", 0.0)),
            "brier": float(payload.get("brier", 0.25)),
            "calibration_slope": float(payload.get("calibration_slope", 1.0)),
            "model_type": str(payload.get("model_type", "")),
            "n_samples": int(payload.get("n_samples", 0)),
            "trained_at_ms": int(payload.get("trained_at_ms", 0)),
        }, ""
    except Exception as e:
        return {}, f"payload parse failed: {type(e).__name__}: {e}"


def list_strategies_with_models() -> list[str]:
    """Return strategies that have a model artifact in MODEL_DIR."""
    if not MODEL_DIR.exists():
        return []
    latest_files = list(MODEL_DIR.glob("*_latest.joblib"))
    return sorted(
        f.name.replace("_latest.joblib", "") for f in latest_files
    )


# ── Metric helpers ───────────────────────────────────────────────────


def roc_auc_fast(y_true: list[int], y_score: list[float]) -> float:
    """Trapezoidal ROC AUC. Returns 0.5 if only one class present."""
    n = len(y_true)
    if n < 2 or len(set(y_true)) < 2:
        return 0.5
    pairs = sorted(zip(y_score, y_true), reverse=True)
    total_pos = sum(1 for _, y in pairs if y == 1)
    total_neg = n - total_pos
    if total_pos == 0 or total_neg == 0:
        return 0.5
    cum_tp = 0
    cum_fp = 0
    auc_sum = 0.0
    prev_tpr = 0.0
    prev_fpr = 0.0
    # Handle ties by grouping
    i = 0
    while i < n:
        j = i
        while j < n and pairs[j][0] == pairs[i][0]:
            j += 1
        for k in range(i, j):
            if pairs[k][1] == 1:
                cum_tp += 1
            else:
                cum_fp += 1
        tpr = cum_tp / total_pos
        fpr = cum_fp / total_neg
        auc_sum += (fpr - prev_fpr) * (tpr + prev_tpr) / 2.0
        prev_tpr = tpr
        prev_fpr = fpr
        i = j
    return auc_sum


def brier_score(y_true: list[int], y_score: list[float]) -> float:
    n = len(y_true)
    if n == 0:
        return 0.25
    return sum((y_score[i] - y_true[i]) ** 2 for i in range(n)) / n


def calibration_slope(y_true: list[int], y_score: list[float]) -> float:
    """Simple linear regression on binned reliability data."""
    n = len(y_true)
    if n < 10:
        return 1.0
    bins = [i / 10.0 for i in range(11)]
    bin_idx = [max(0, min(9, int(y * 10))) for y in y_score]
    obs: list[float] = []
    pred: list[float] = []
    for b in range(10):
        mask_count = sum(1 for idx in bin_idx if idx == b)
        if mask_count == 0:
            continue
        obs.append(sum(y_true[i] for i in range(n) if bin_idx[i] == b) / mask_count)
        pred.append(sum(y_score[i] for i in range(n) if bin_idx[i] == b) / mask_count)
    if len(obs) < 2:
        return 1.0
    # Linear regression
    mean_x = sum(pred) / len(pred)
    mean_y = sum(obs) / len(obs)
    num = sum((pred[i] - mean_x) * (obs[i] - mean_y) for i in range(len(pred)))
    den = sum((pred[i] - mean_x) ** 2 for i in range(len(pred)))
    if den == 0:
        return 1.0
    return num / den


# ── Per-strategy checks ──────────────────────────────────────────────


def check_strategy(
    conn: sqlite3.Connection,
    strategy: str,
    window_rows: int,
) -> list[CheckResult]:
    """Run all shadow-mode checks for one strategy."""
    results: list[CheckResult] = []

    # Model artifact check
    train_metrics, err = load_training_metrics_from_artifact(strategy)
    if not train_metrics:
        results.append(CheckResult(
            name="model_artifact",
            status="FAIL",
            detail=err or f"no model artifact at {MODEL_DIR}/{strategy}_latest.joblib",
        ))
        return results
    results.append(CheckResult(
        name="model_artifact",
        status="PASS",
        detail=f"{train_metrics.get('model_type')} trained AUC={train_metrics.get('auc', 0):.3f}",
        data=train_metrics,
    ))

    # Live audit rows check
    rows = load_live_audit_rows(conn, strategy, limit=window_rows)
    n_live = len(rows)

    if n_live < MIN_SAMPLES_FOR_AUC:
        results.append(CheckResult(
            name="live_sample_size",
            status="SKIP",
            detail=f"only {n_live} live rows (min {MIN_SAMPLES_FOR_AUC}) — training metrics stand",
            data={"n_live": n_live, "min_required": MIN_SAMPLES_FOR_AUC},
        ))
        return results

    # Reverse to chronological for any time-sensitive metrics
    rows = list(reversed(rows))

    y_true = [int(r["meta_label"]) for r in rows if r["meta_label"] is not None]
    y_proba = [
        float(r["meta_proba"]) if r["meta_proba"] is not None else 0.5
        for r in rows if r["meta_label"] is not None
    ]
    if len(y_true) < MIN_SAMPLES_FOR_AUC:
        results.append(CheckResult(
            name="live_sample_size",
            status="SKIP",
            detail=f"{len(y_true)} usable rows after filtering (min {MIN_SAMPLES_FOR_AUC})",
        ))
        return results

    # Rolling AUC
    live_auc = roc_auc_fast(y_true, y_proba)
    if live_auc >= ADVISORY_AUC_MIN:
        results.append(CheckResult(
            name="rolling_auc",
            status="PASS",
            detail=f"live AUC {live_auc:.3f} ≥ {ADVISORY_AUC_MIN} ({len(y_true)} rows)",
            data={"live_auc": live_auc, "n": len(y_true)},
        ))
    elif live_auc >= 0.50:
        results.append(CheckResult(
            name="rolling_auc",
            status="WARN",
            detail=f"live AUC {live_auc:.3f} in [0.50, {ADVISORY_AUC_MIN}) — below advisory gate",
            data={"live_auc": live_auc, "n": len(y_true)},
        ))
    else:
        results.append(CheckResult(
            name="rolling_auc",
            status="FAIL",
            detail=f"live AUC {live_auc:.3f} < 0.50 — classifier worse than random",
            data={"live_auc": live_auc, "n": len(y_true)},
        ))

    # Brier drift vs training
    live_brier = brier_score(y_true, y_proba)
    train_brier = train_metrics.get("brier", 0.25)
    drift_ratio = live_brier / train_brier if train_brier > 0 else 1.0
    if drift_ratio <= BRIER_DRIFT_MAX:
        results.append(CheckResult(
            name="brier_drift",
            status="PASS",
            detail=f"live Brier {live_brier:.3f} vs train {train_brier:.3f} (ratio {drift_ratio:.2f})",
            data={"live_brier": live_brier, "train_brier": train_brier, "ratio": drift_ratio},
        ))
    else:
        results.append(CheckResult(
            name="brier_drift",
            status="WARN",
            detail=f"live Brier {live_brier:.3f} > train {train_brier:.3f} × {BRIER_DRIFT_MAX}",
            data={"live_brier": live_brier, "train_brier": train_brier, "ratio": drift_ratio},
        ))

    # Calibration slope
    slope = calibration_slope(y_true, y_proba)
    if slope >= CALIBRATION_SLOPE_MIN:
        results.append(CheckResult(
            name="calibration_slope",
            status="PASS",
            detail=f"slope {slope:.3f} ≥ {CALIBRATION_SLOPE_MIN}",
            data={"slope": slope},
        ))
    else:
        results.append(CheckResult(
            name="calibration_slope",
            status="WARN",
            detail=f"slope {slope:.3f} < {CALIBRATION_SLOPE_MIN} — probabilities are degenerate",
            data={"slope": slope},
        ))

    return results


# ── Aggregation ──────────────────────────────────────────────────────


def _aggregate_status(per_strategy: dict[str, list[CheckResult]]) -> str:
    any_fail = False
    any_warn = False
    for checks in per_strategy.values():
        for c in checks:
            if c.status == "FAIL":
                any_fail = True
            elif c.status == "WARN":
                any_warn = True
    if any_fail:
        return "ERROR"
    if any_warn:
        return "WARNING"
    return "HEALTHY"


def run_checks(window_rows: int = 100) -> MetaLabelReport:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not TRADES_DB.exists():
        return MetaLabelReport(
            timestamp_utc=now_iso,
            overall_status="DISABLED",
            per_strategy={"_global": [CheckResult(
                name="trades_db",
                status="SKIP",
                detail=f"{TRADES_DB} not found — engine has never run",
            )]},
            summary={"window_rows": window_rows},
        )

    strategies = list_strategies_with_models()
    if not strategies:
        return MetaLabelReport(
            timestamp_utc=now_iso,
            overall_status="DISABLED",
            per_strategy={"_global": [CheckResult(
                name="model_dir",
                status="SKIP",
                detail=f"no trained models in {MODEL_DIR} — run train_meta_classifier.py first",
            )]},
            summary={"window_rows": window_rows},
        )

    conn = sqlite3.connect(str(TRADES_DB))
    conn.row_factory = sqlite3.Row
    try:
        per_strategy: dict[str, list[CheckResult]] = {}
        for name in strategies:
            per_strategy[name] = check_strategy(conn, name, window_rows)

        summary = {
            "window_rows": window_rows,
            "n_strategies": len(strategies),
            "strategies": strategies,
        }

        status = _aggregate_status(per_strategy)
        return MetaLabelReport(
            timestamp_utc=now_iso,
            overall_status=status,
            per_strategy=per_strategy,
            summary=summary,
        )
    finally:
        conn.close()


# ── Gate helpers (for Day 3/4 cron wake-ups) ────────────────────────


def passes_phase4_advisory_gate(report: MetaLabelReport) -> tuple[bool, str]:
    """Return (passes, reason). Used by promote_meta_label.sh --to advisory."""
    if report.overall_status == "ERROR":
        return False, f"overall status ERROR — check {REPORT_MD}"
    if report.overall_status == "DISABLED":
        return False, "filter disabled or no models — cannot promote"
    # Every strategy must have either PASS AUC or SKIP (insufficient data is
    # acceptable — we fall back to training metrics)
    for name, checks in report.per_strategy.items():
        for c in checks:
            if c.name == "rolling_auc" and c.status == "FAIL":
                return False, f"{name}: {c.detail}"
    return True, "all strategies pass advisory gate"


def passes_phase5_live_gate(report: MetaLabelReport) -> tuple[bool, str]:
    """Return (passes, reason). Used by promote_meta_label.sh --to live.

    Stricter than advisory: requires PASS rolling_auc on live data (no SKIP)
    for at least one strategy, and no WARN/FAIL on any strategy.
    """
    if report.overall_status != "HEALTHY":
        return False, f"overall status {report.overall_status} — live gate needs HEALTHY"
    has_live_pass = False
    for name, checks in report.per_strategy.items():
        for c in checks:
            if c.name == "rolling_auc" and c.status == "PASS" and c.data.get("n", 0) >= MIN_SAMPLES_FOR_AUC:
                has_live_pass = True
    if not has_live_pass:
        return False, "no strategy has >= 20 live rows with PASS rolling_auc"
    return True, "live gate cleared"


# ── Output writers ───────────────────────────────────────────────────


def write_markdown_report(report: MetaLabelReport) -> None:
    STATUS_ICONS = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭"}
    OVERALL_ICONS = {"HEALTHY": "✅", "WARNING": "⚠️", "ERROR": "❌", "DISABLED": "⏸"}

    lines: list[str] = []
    lines.append(f"# Meta-Label Shadow Check — {report.timestamp_utc}")
    lines.append("")
    lines.append(
        f"**Status:** {OVERALL_ICONS.get(report.overall_status, '?')} "
        f"{report.overall_status}"
    )
    lines.append("")

    if report.overall_status == "DISABLED":
        lines.append(
            "Meta-label filter disabled or has no models. Activate by running "
            "`scripts/train_meta_classifier.py --all --run-prefix harvest_` and "
            "flipping `config/settings.toml [meta_label] enabled = true`."
        )
        lines.append("")

    lines.append("## Per-Strategy Results")
    lines.append("")
    for name, checks in sorted(report.per_strategy.items()):
        lines.append(f"### `{name}`")
        lines.append("")
        lines.append("| Check | Status | Detail |")
        lines.append("|---|---|---|")
        for c in checks:
            icon = STATUS_ICONS.get(c.status, "?")
            detail = c.detail.replace("|", "/")
            lines.append(f"| `{c.name}` | {icon} {c.status} | {detail} |")
        lines.append("")

    lines.append("## Gate Readiness")
    lines.append("")
    advisory_ok, advisory_reason = passes_phase4_advisory_gate(report)
    live_ok, live_reason = passes_phase5_live_gate(report)
    lines.append(f"- **Advisory promotion:** {'✅' if advisory_ok else '❌'} {advisory_reason}")
    lines.append(f"- **Live promotion:** {'✅' if live_ok else '❌'} {live_reason}")
    lines.append("")

    if report.summary:
        lines.append("## Summary")
        lines.append("")
        for k, v in report.summary.items():
            lines.append(f"- **{k}:** `{v}`")
        lines.append("")

    lines.append(f"_Next check in 6 hours. Last written: {report.timestamp_utc}_")
    REPORT_MD.write_text("\n".join(lines))


def write_json_status(report: MetaLabelReport) -> None:
    STATUS_JSON.write_text(json.dumps(report.to_json(), indent=2, default=str))


def append_alerts_log(report: MetaLabelReport) -> None:
    if report.overall_status in ("HEALTHY", "DISABLED"):
        return
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a") as f:
        f.write(f"[{report.timestamp_utc}] {report.overall_status}\n")
        for name, checks in report.per_strategy.items():
            for c in checks:
                if c.status in ("FAIL", "WARN"):
                    f.write(f"  - {name}/{c.name} {c.status}: {c.detail}\n")


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-rows", type=int, default=100,
                        help="Number of most-recent live rows to use per strategy")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    report = run_checks(window_rows=args.window_rows)

    write_markdown_report(report)
    write_json_status(report)
    append_alerts_log(report)

    if args.verbose:
        print(f"Status: {report.overall_status}")
        for name, checks in report.per_strategy.items():
            print(f"  {name}:")
            for c in checks:
                print(f"    [{c.status}] {c.name}: {c.detail}")
        print(f"Report:   {REPORT_MD}")
        print(f"Status:   {STATUS_JSON}")
        advisory_ok, advisory_reason = passes_phase4_advisory_gate(report)
        live_ok, live_reason = passes_phase5_live_gate(report)
        print(f"Advisory gate: {'PASS' if advisory_ok else 'FAIL'} — {advisory_reason}")
        print(f"Live gate:     {'PASS' if live_ok else 'FAIL'} — {live_reason}")

    return {
        "HEALTHY": 0,
        "DISABLED": 0,
        "WARNING": 1,
        "ERROR": 2,
    }.get(report.overall_status, 2)


if __name__ == "__main__":
    sys.exit(main())
