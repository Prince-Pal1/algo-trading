"""Backfill the `strategies` + `strategy_versions` tables from existing
`reports/deep_backtest_*/` directories (task #118, Stage 2).

Auto-capture from `run_deep_backtest()` only fires on NEW runs. Prince has
17+ historical report dirs sitting on disk from the 2026-04-15 Day 2 sprint
that predate task #117 — this script walks them, reads each `summary.json`
and `matrix.csv`, and upserts rows so the strategies CLI + (future) Streamlit
page see the full history.

Design:
- Idempotent. Re-running upserts the same rows (no duplicates).
- Reads summary.json for verdict + config + WF data.
- Reads matrix.csv for cells + max-return pick.
- Preserves the original report_dir path (relative to REPO_ROOT).
- Stores the report's own timestamp (from the dir name or summary.json) in
  last_backtested_at — NOT the current time, so backfilled rows reflect
  when the backtest actually ran.

Usage:
    PYTHONPATH=. python3 scripts/strategies_backfill.py
    PYTHONPATH=. python3 scripts/strategies_backfill.py --reports-dir reports/
    PYTHONPATH=. python3 scripts/strategies_backfill.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.strategies.storage import (  # noqa: E402
    StoredStrategy,
    StoredVersion,
    _append_version_run,
    _generate_version_slug,
    _to_relative,
    list_versions,
    upsert_strategy,
    upsert_version,
)
from src.backtest.deep_backtest import (  # noqa: E402
    _compute_max_return_cell,
    _is_max_return_sane,
)


def _parse_timestamp_from_dir(dir_name: str) -> str | None:
    """Extract the ISO timestamp from a dir name like
    'deep_backtest_donchian_gold_2026-04-15_124551'. Returns an RFC3339 string
    or None if the name doesn't match."""
    # Grab the last two components: <date>_<time>
    parts = dir_name.split("_")
    if len(parts) < 2:
        return None
    try:
        ts = datetime.strptime(f"{parts[-2]}_{parts[-1]}", "%Y-%m-%d_%H%M%S")
        return ts.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def _load_summary(report_dir: Path) -> dict | None:
    path = report_dir / "summary.json"
    if not path.exists():
        print(f"  [skip] {report_dir.name}: no summary.json (aborted run?)", file=sys.stderr)
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as e:
        print(f"  [skip] {report_dir.name}: failed to parse summary.json: {e}", file=sys.stderr)
        return None


def _load_matrix(report_dir: Path) -> pd.DataFrame | None:
    path = report_dir / "matrix.csv"
    if not path.exists():
        return None
    try:
        return pd.read_csv(path)
    except (ValueError, OSError, pd.errors.ParserError) as e:
        print(f"  [skip] {report_dir.name}: failed to parse matrix.csv: {e}", file=sys.stderr)
        return None


def _backfill_one(report_dir: Path, *, db_path: str, dry_run: bool = False) -> bool:
    """Upsert a single report dir into storage. Returns True on success."""
    summary = _load_summary(report_dir)
    if summary is None:
        return False

    strategy_name = summary.get("strategy") or "unknown"
    timestamp = summary.get("timestamp") or _parse_timestamp_from_dir(report_dir.name) or ""

    config = summary.get("config") or {}
    lev_mode = config.get("leverage_mode")
    baseline = config.get("baseline_leverage")
    tfs = config.get("timeframes") or []
    timeframe = tfs[0] if tfs else None
    strategy_params = config.get("strategy_params") or {}

    # Matrix DataFrame → max-return cell + sanity flag
    matrix_df = _load_matrix(report_dir)
    max_cell = _compute_max_return_cell(matrix_df) if matrix_df is not None else {}
    sane, warning = _is_max_return_sane(max_cell)

    # Best-Calmar cell from summary.json
    best_cell_data = summary.get("best_cell") or {}
    best_calmar = best_cell_data.get("calmar")
    best_calmar_ret = best_cell_data.get("return_pct")

    # Walk-forward denormalization from summary.walk_forward.continuous_*
    wf = summary.get("walk_forward") or {}
    wf_ret = wf.get("continuous_return_pct")
    wf_dd = wf.get("continuous_dd_pct")
    wf_calmar = wf.get("continuous_calmar")
    wf_gate = wf.get("continuous_gate_passed")
    wf_gate = str(wf_gate) if wf_gate is not None else None
    wf_nf = wf.get("n_folds")
    wf_prof = wf.get("profitable_folds")

    # Timestamps — prefer the report's own timestamp so backfilled rows
    # reflect when the backtest actually ran (not today).
    iso_timestamp = _parse_timestamp_from_dir(report_dir.name)

    report_dir_rel = _to_relative(str(report_dir))
    html_path = report_dir / "index.html"
    summary_path = report_dir / "summary.json"
    html_rel = _to_relative(str(html_path)) if html_path.exists() else None
    summary_rel = _to_relative(str(summary_path)) if summary_path.exists() else None

    matrix = summary.get("matrix_n_cells") or (len(matrix_df) if matrix_df is not None else 0)

    if dry_run:
        print(
            f"  [dry] {strategy_name}: slug candidate, "
            f"verdict={summary.get('verdict')}, "
            f"max_return={max_cell.get('return_pct') if max_cell else 'n/a'}, "
            f"wf_calmar={wf_calmar}, dir={report_dir.name}"
        )
        return True

    # Parent row — minimal metadata because backfill doesn't instantiate
    # the strategy class (introspection is an auto-capture feature).
    parent = StoredStrategy(
        name=strategy_name,
        status="researching",
    )
    strategy_id = upsert_strategy(parent, db_path=db_path)

    # Slug generation — same rules as auto-capture
    existing = list_versions(strategy_name=strategy_name, db_path=db_path)
    slug = _generate_version_slug(
        leverage_mode=lev_mode,
        baseline_leverage=baseline,
        timeframe=timeframe,
        params=strategy_params,
        existing_versions=existing,
    )

    version = StoredVersion(
        strategy_id=strategy_id,
        version_slug=slug,
        params=strategy_params or None,
        leverage_mode=lev_mode,
        baseline_leverage=baseline,
        timeframe=timeframe,
        last_backtested_at=iso_timestamp,
        report_dir=report_dir_rel,
        report_html_path=html_rel,
        summary_json_path=summary_rel,
        verdict=summary.get("verdict"),
        verdict_reason=summary.get("verdict_reason"),
        matrix_n_cells=matrix,
        max_return_pct=max_cell.get("return_pct") if max_cell else None,
        max_return_cell=max_cell or None,
        max_return_sane=sane if max_cell else None,
        max_return_warning=warning or None,
        best_calmar=best_calmar,
        best_calmar_return_pct=best_calmar_ret,
        best_calmar_cell=best_cell_data or None,
        wf_continuous_return_pct=wf_ret,
        wf_continuous_dd_pct=wf_dd,
        wf_continuous_calmar=wf_calmar,
        wf_gate_passed=wf_gate,
        wf_n_folds=wf_nf,
        wf_profitable_folds=wf_prof,
    )
    version_id = upsert_version(version, db_path=db_path)

    # History row — backfill is "a run that happened in the past", so
    # we append one history entry per report dir.
    _append_version_run(version_id=version_id, version=version, db_path=db_path)

    mr = f"{max_cell.get('return_pct', 0):+.1f}%" if max_cell else "n/a"
    print(f"  [ok]  {strategy_name} / {slug}: max={mr}, verdict={summary.get('verdict')}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Backfill strategy storage tables from existing deep_backtest run dirs.",
    )
    ap.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
        help="Directory containing deep_backtest_* subdirectories (default: reports/)",
    )
    ap.add_argument(
        "--db",
        default="data/trades.db",
        help="SQLite DB path (default: data/trades.db)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report but do NOT write to the DB",
    )
    args = ap.parse_args(argv)

    if not args.reports_dir.exists():
        print(f"error: reports dir not found: {args.reports_dir}", file=sys.stderr)
        return 2

    dirs = sorted(
        d for d in args.reports_dir.iterdir()
        if d.is_dir() and d.name.startswith("deep_backtest_")
    )
    if not dirs:
        print(f"(no deep_backtest_* dirs in {args.reports_dir})")
        return 0

    print(f"Found {len(dirs)} deep_backtest run dirs{' (dry-run)' if args.dry_run else ''}")
    success = 0
    for d in dirs:
        if _backfill_one(d, db_path=args.db, dry_run=args.dry_run):
            success += 1

    print(f"\n{success}/{len(dirs)} backfilled successfully{' (dry-run, nothing written)' if args.dry_run else ''}")
    return 0 if success == len(dirs) else 1


if __name__ == "__main__":
    sys.exit(main())
