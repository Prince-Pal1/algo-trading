#!/usr/bin/env python3
"""Compute per-strategy Deflated Sharpe Ratio from signal_audit rows.

The Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) is the honesty
anchor specified in the Phase 3c plan — before flipping the meta-label
filter to advisory or live, we require that the Deflated Sharpe does NOT
regress vs the pre-filter baseline.

This script reads signal_audit, derives per-strategy PnL series, computes
raw Sharpe, then applies deflation for sample size + multiple testing.
Two calculations are emitted per strategy:

    raw_sharpe         — annualized Sharpe from per-trade PnL series
    deflated_sharpe    — DSR after finite-sample + trials penalty

A "trials" count of 1 is used for the primary (no multiple testing). When
calling this from a promotion script comparing live vs. baseline, pass
`--trials 2` to penalize for the comparison itself.

Usage:
    python3 scripts/deflated_sharpe_from_audit.py
    python3 scripts/deflated_sharpe_from_audit.py --run-prefix live
    python3 scripts/deflated_sharpe_from_audit.py --run-prefix harvest_
    python3 scripts/deflated_sharpe_from_audit.py --verbose
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
TRADES_DB = DATA_DIR / "trades.db"
OUT_JSON = DATA_DIR / "deflated_sharpe_live.json"

from src.m3s.evaluation import _deflated_sharpe  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

log = get_logger("deflated_sharpe_from_audit")

# Crypto annualization for daily bars in a 24/7 market
_SQRT_365 = math.sqrt(365.0)
_SQRT_365x3 = math.sqrt(365.0 * 3)  # for 8h bar frequency


def load_pnls(
    conn: sqlite3.Connection,
    strategy: str,
    *,
    run_id_prefix: str | None = None,
    meta_decision_filter: list[str] | None = None,
) -> list[float]:
    """Load per-trade realized_pnl values for one strategy.

    Args:
        run_id_prefix: only consider audit rows whose run_id starts with
            this (e.g., 'live' or 'harvest_')
        meta_decision_filter: only consider rows where meta_decision is
            in this list. None = no filter (all decisions including NULL).
            To compute "baseline" PnL as if the filter had not vetoed
            anything, pass ['pass', 'scale', None].
    """
    query = """
        SELECT realized_pnl FROM signal_audit
        WHERE strategy = ? AND realized_pnl IS NOT NULL
    """
    params: list = [strategy]
    if run_id_prefix:
        query += " AND run_id LIKE ?"
        params.append(f"{run_id_prefix}%")
    if meta_decision_filter is not None:
        placeholders = ",".join("?" for _ in meta_decision_filter)
        # NULL is not captured by IN; handle separately
        has_null = None in meta_decision_filter
        non_null = [v for v in meta_decision_filter if v is not None]
        conds = []
        if non_null:
            conds.append(f"meta_decision IN ({','.join('?' for _ in non_null)})")
            params.extend(non_null)
        if has_null:
            conds.append("meta_decision IS NULL")
        if conds:
            query += " AND (" + " OR ".join(conds) + ")"
    query += " ORDER BY signal_ts_ms ASC"

    try:
        cursor = conn.execute(query, params)
    except sqlite3.OperationalError as e:
        log.warning("deflated_sharpe_query_failed", strategy=strategy, error=str(e))
        return []
    return [float(row[0]) for row in cursor.fetchall()]


def sharpe_from_pnls(pnls: list[float], annualization: float = _SQRT_365) -> float:
    """Compute annualized Sharpe from a per-trade PnL series."""
    n = len(pnls)
    if n < 2:
        return 0.0
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / n
    std = math.sqrt(var)
    if std <= 0.0:
        return 0.0
    # Per-trade Sharpe annualized by sqrt(bars per year)
    return (mean / std) * annualization


def compute_for_strategy(
    conn: sqlite3.Connection,
    strategy: str,
    *,
    run_id_prefix: str | None,
    n_trials: int,
) -> dict:
    """Return a dict with {n, raw_sharpe, deflated_sharpe, baseline_*}."""
    all_pnls = load_pnls(conn, strategy, run_id_prefix=run_id_prefix)
    baseline_pnls = load_pnls(
        conn, strategy,
        run_id_prefix=run_id_prefix,
        meta_decision_filter=["pass", "scale", None],
    )

    raw = sharpe_from_pnls(all_pnls)
    deflated = _deflated_sharpe(raw, len(all_pnls), n_trials=n_trials)

    baseline_raw = sharpe_from_pnls(baseline_pnls)
    baseline_deflated = _deflated_sharpe(
        baseline_raw, len(baseline_pnls), n_trials=n_trials
    )

    return {
        "strategy": strategy,
        "n_samples": len(all_pnls),
        "raw_sharpe": raw,
        "deflated_sharpe": deflated,
        "baseline_n_samples": len(baseline_pnls),
        "baseline_raw_sharpe": baseline_raw,
        "baseline_deflated_sharpe": baseline_deflated,
        "delta_raw": raw - baseline_raw,
        "delta_deflated": deflated - baseline_deflated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-prefix",
        default=None,
        help="Only use audit rows whose run_id starts with this (e.g., 'live' or 'harvest_')",
    )
    parser.add_argument("--trials", type=int, default=1,
                        help="Number of multiple-testing trials for DSR deflation")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not TRADES_DB.exists():
        print(f"ERROR: {TRADES_DB} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(TRADES_DB))
    try:
        # Discover strategies with audit rows
        cursor = conn.execute(
            """
            SELECT DISTINCT strategy FROM signal_audit
            WHERE realized_pnl IS NOT NULL
            """
            + (" AND run_id LIKE ?" if args.run_prefix else ""),
            ([f"{args.run_prefix}%"] if args.run_prefix else []),
        )
        strategies = sorted(row[0] for row in cursor.fetchall())
    finally:
        pass

    results: list[dict] = []
    try:
        for name in strategies:
            r = compute_for_strategy(
                conn, name,
                run_id_prefix=args.run_prefix,
                n_trials=args.trials,
            )
            results.append(r)
    finally:
        conn.close()

    # Total book Sharpe across all strategies (per-trade PnL concatenation)
    total = {
        "n_samples": sum(r["n_samples"] for r in results),
        "raw_sharpe": 0.0,
        "deflated_sharpe": 0.0,
    }
    if results:
        raw_sum = sum(r["raw_sharpe"] * r["n_samples"] for r in results)
        if total["n_samples"] > 0:
            total["raw_sharpe"] = raw_sum / total["n_samples"]
        # Aggregate DSR via weighted average (approximation — for exact DSR
        # the caller should compute from concatenated PnL series)
        defl_sum = sum(r["deflated_sharpe"] * r["n_samples"] for r in results)
        if total["n_samples"] > 0:
            total["deflated_sharpe"] = defl_sum / total["n_samples"]

    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id_prefix": args.run_prefix,
        "n_trials": args.trials,
        "per_strategy": results,
        "total_book": total,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2))

    if args.verbose:
        print(f"Deflated Sharpe report — run_prefix={args.run_prefix or '*'}")
        print()
        for r in results:
            print(f"  {r['strategy']}:")
            print(f"    n={r['n_samples']:>6}  raw={r['raw_sharpe']:+.3f}  deflated={r['deflated_sharpe']:+.3f}")
            print(f"    baseline (no-veto): n={r['baseline_n_samples']:>6}  "
                  f"raw={r['baseline_raw_sharpe']:+.3f}  deflated={r['baseline_deflated_sharpe']:+.3f}")
            print(f"    delta_raw={r['delta_raw']:+.3f}  delta_deflated={r['delta_deflated']:+.3f}")
            print()
        print(f"Total book:")
        print(f"  n={total['n_samples']}  raw={total['raw_sharpe']:+.3f}  deflated={total['deflated_sharpe']:+.3f}")
        print()
        print(f"Written: {OUT_JSON}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
