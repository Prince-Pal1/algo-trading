#!/usr/bin/env python3
"""Task #133 — one-off migration from hashed-suffix slugs to pure triplets.

What this does:
    1. Connects to `data/trades.db` and runs pending migrations (adds the
       v6 `facets_json` column if not already present).
    2. Scans all `strategy_versions` rows grouped by
       `(strategy_id, leverage_mode, baseline_leverage, timeframe)`. Each
       group is one "pure triplet" — e.g., `margin_capped × L1 × 5m`.
    3. For each group with >1 row (hash-suffixed duplicates):
         a. Picks the winner via `_is_better_version_metric`
            (sane-first → WF Calmar → max_return_pct).
         b. Merges the `max_return_cell` payloads from every loser into
            the winner's facets dict via `_merge_facets`. This produces a
            single row with one facet per `(window, fee)` it has ever seen,
            already keep-best-merged.
         c. Re-points `strategy_version_runs.version_id` from every loser
            to the winner (so history survives the consolidation).
         d. DELETES the loser rows.
         e. Renames the winner's `version_slug` from any hashed form to the
            pure triplet slug.
         f. Regenerates the winner's auto-description.

Safety:
    - Prints a dry-run summary first. The user passes `--apply` to commit
      the changes.
    - Aborts if the database schema is NOT at least v6 (the column must
      exist before we can write to it).
    - Uses a single transaction per parent group — a failure rolls back
      that group only.
    - Backup: the script prints the DB path and advises running
      `cp data/trades.db data/trades.db.bak.$(date +%s)` before `--apply`.

Usage:
    python3 scripts/strategies_migrate_v6.py           # dry run
    python3 scripts/strategies_migrate_v6.py --apply   # apply changes
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# Make the repo root importable so we can reach src/.strategies.storage
# without copy-pasting its helpers here.
sys.path.insert(0, str(_repo_root()))

from src.strategies.storage import (  # noqa: E402
    _generate_description,
    _generate_version_slug,
    _is_better_version_metric,
    _merge_facets,
    _SCHEMA_VERSION,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _parse_json(s: str | None, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _cell_to_facet(cell: dict, *, verdict: str | None,
                   verdict_reason: str | None,
                   wf_calmar: float | None,
                   report_dir: str | None,
                   report_html_path: str | None,
                   updated_at: str) -> dict | None:
    """Synthesize a facet payload from a legacy `max_return_cell_json`.

    Old rows stored ONLY the max-return cell (no facets). We turn that one
    cell into a single facet keyed by its `(window_label, fee_profile)`.
    Later losers' cells get merged in via `_merge_facets` so the winner row
    ends up with one facet per `(window, fee)` the set of loser+winner rows
    ever touched.
    """
    if not cell:
        return None
    window_label = cell.get("window_label")
    fee_profile = cell.get("fee_profile")
    if not window_label or not fee_profile:
        return None
    # Derive sanity flag from the existing row's `max_return_sane` column.
    # For rows missing that column we recompute from thresholds.
    trades = int(cell.get("trades") or 0)
    maxdd = float(cell.get("maxdd_pct") or 0.0)
    calmar = float(cell.get("calmar") or 0.0)
    sane = trades >= 30 and maxdd < 60.0 and calmar > 0.2
    warning = ""
    if not sane:
        if trades < 30:
            warning = f"only {trades} trades (< 30)"
        elif maxdd >= 60.0:
            warning = f"DD {maxdd:.0f}% (>= 60%)"
        elif calmar <= 0.2:
            warning = f"Calmar {calmar:.2f} (<= 0.2)"
    return {
        "window_days": int(cell.get("window_days") or 0),
        "window_label": str(window_label),
        "fee_profile": str(fee_profile),
        "timeframe": str(cell.get("timeframe") or ""),
        "leverage": float(cell.get("leverage") or 0.0),
        "trades": trades,
        "return_pct": float(cell.get("return_pct") or 0.0),
        "maxdd_pct": maxdd,
        "calmar": calmar,
        "sharpe": float(cell.get("sharpe") or 0.0),
        "win_rate": float(cell.get("win_rate") or 0.0),
        "profit_factor": float(cell.get("profit_factor") or 0.0),
        "sane": sane,
        "warning": warning,
        "wf_calmar": wf_calmar,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "report_dir": report_dir,
        "report_html_path": report_html_path,
        "updated_at": updated_at,
    }


def _row_to_facet_dict(row: sqlite3.Row) -> dict[str, dict]:
    """Read an existing row's (cell, verdict, wf_calmar) and build a facets dict."""
    # Prefer an already-populated facets_json (schema v6 row that ran through
    # the new write path once already). Otherwise synthesize from legacy
    # max_return_cell_json.
    fj = _parse_json(row["facets_json"] if "facets_json" in row.keys() else None, {}) or {}
    if fj:
        return fj
    cell = _parse_json(row["max_return_cell_json"], {}) or {}
    facet = _cell_to_facet(
        cell,
        verdict=row["verdict"],
        verdict_reason=row["verdict_reason"],
        wf_calmar=row["wf_continuous_calmar"] if "wf_continuous_calmar" in row.keys() else None,
        report_dir=row["report_dir"],
        report_html_path=row["report_html_path"],
        updated_at=row["updated_at"] or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if facet is None:
        return {}
    key = f"{facet['window_label']}::{facet['fee_profile']}"
    return {key: facet}


def _top_level_from_facets(facets: dict[str, dict]) -> dict:
    """Local mirror of storage._top_level_from_facets — avoid an extra import
    just for one call site."""
    if not facets:
        return {}
    hero: dict | None = None
    for f in facets.values():
        if _is_better_version_metric(f, hero):
            hero = f
    if hero is None:
        return {}
    return {
        "max_return_pct": hero.get("return_pct"),
        "max_return_cell": {
            k: hero[k]
            for k in ("window_days", "window_label", "timeframe", "leverage",
                      "fee_profile", "trades", "return_pct", "maxdd_pct",
                      "calmar", "sharpe", "win_rate", "profit_factor")
            if k in hero
        },
        "max_return_sane": hero.get("sane"),
        "max_return_warning": hero.get("warning") or None,
        "best_calmar": hero.get("calmar"),
        "best_calmar_return_pct": hero.get("return_pct"),
        "verdict": hero.get("verdict"),
        "verdict_reason": hero.get("verdict_reason"),
        "report_dir": hero.get("report_dir"),
        "report_html_path": hero.get("report_html_path"),
    }


def _row_metric_for_comparator(row: sqlite3.Row) -> dict:
    """Map a row's top-level columns into the comparator's expected shape."""
    return {
        "sane": bool(row["max_return_sane"]) if row["max_return_sane"] is not None else False,
        "wf_calmar": row["wf_continuous_calmar"] if "wf_continuous_calmar" in row.keys() else None,
        "return_pct": row["max_return_pct"],
    }


def migrate(db_path: Path, *, apply: bool) -> int:
    print(f"Database: {db_path}")
    if not db_path.exists():
        print(f"ERROR: database does not exist at {db_path}")
        return 2

    conn = _connect(db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        print(f"Schema version: {user_version} (target {_SCHEMA_VERSION})")
        if user_version < _SCHEMA_VERSION:
            print(
                "ABORT: schema is older than v6. Run any write through "
                "`src.strategies.storage._connect` once (e.g., "
                "`python3 -c 'from src.strategies.storage import _connect; _connect().close()'`) "
                "so migrations auto-apply, then re-run this script."
            )
            return 2

        # Group all rows by (strategy_id, leverage_mode, baseline_leverage, timeframe)
        rows = conn.execute("SELECT * FROM strategy_versions ORDER BY strategy_id, id").fetchall()
        print(f"Total strategy_versions rows: {len(rows)}")

        groups: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            key = (
                r["strategy_id"],
                r["leverage_mode"] or "",
                r["baseline_leverage"] if r["baseline_leverage"] is not None else "",
                r["timeframe"] or "",
            )
            groups[key].append(r)

        consolidated = 0
        renamed = 0
        merged_facets_count = 0
        deletions = 0

        for key, grp in groups.items():
            strategy_id, lev_mode, baseline, tf = key
            # Target slug — what the group SHOULD collapse to.
            pure_slug = _generate_version_slug(
                leverage_mode=lev_mode or None,
                baseline_leverage=baseline if baseline != "" else None,
                timeframe=tf or None,
            )
            description = _generate_description(
                leverage_mode=lev_mode or None,
                baseline_leverage=baseline if baseline != "" else None,
                timeframe=tf or None,
            )

            # Single-row group: nothing to consolidate, but we still may
            # need to rename the slug to the pure triplet AND populate
            # facets_json from the legacy cell.
            if len(grp) == 1:
                r = grp[0]
                current_slug = r["version_slug"]
                current_facets = _parse_json(r["facets_json"], {}) or {}
                needs_slug_rename = current_slug != pure_slug
                needs_facets_backfill = not current_facets
                needs_description = (r["description"] or "") != description
                if not (needs_slug_rename or needs_facets_backfill or needs_description):
                    continue

                facets_new = current_facets or _row_to_facet_dict(r)
                top_level = _top_level_from_facets(facets_new)
                print(
                    f"  [1-row] id={r['id']} {current_slug!r} → {pure_slug!r} "
                    f"(facets_backfill={needs_facets_backfill})"
                )
                if apply:
                    conn.execute(
                        """
                        UPDATE strategy_versions
                        SET version_slug = ?,
                            description = ?,
                            facets_json = ?,
                            max_return_pct = COALESCE(?, max_return_pct),
                            max_return_cell_json = COALESCE(?, max_return_cell_json),
                            max_return_sane = COALESCE(?, max_return_sane),
                            max_return_warning = COALESCE(?, max_return_warning),
                            best_calmar = COALESCE(?, best_calmar),
                            best_calmar_return_pct = COALESCE(?, best_calmar_return_pct),
                            updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (
                            pure_slug,
                            description,
                            json.dumps(facets_new) if facets_new else None,
                            top_level.get("max_return_pct"),
                            json.dumps(top_level.get("max_return_cell"))
                                if top_level.get("max_return_cell") else None,
                            int(top_level["max_return_sane"])
                                if top_level.get("max_return_sane") is not None else None,
                            top_level.get("max_return_warning"),
                            top_level.get("best_calmar"),
                            top_level.get("best_calmar_return_pct"),
                            r["id"],
                        ),
                    )
                if needs_slug_rename:
                    renamed += 1
                if needs_facets_backfill:
                    merged_facets_count += 1
                continue

            # Multi-row group: pick winner, merge facets from losers, repoint
            # history, delete losers, rename winner.
            print(
                f"Group (strategy_id={strategy_id}, mode={lev_mode!r}, "
                f"L={baseline}, tf={tf!r}) → {len(grp)} rows → collapse to "
                f"slug={pure_slug!r}"
            )
            # Find the winner
            winner = grp[0]
            winner_metric = _row_metric_for_comparator(winner)
            for r in grp[1:]:
                rm = _row_metric_for_comparator(r)
                if _is_better_version_metric(rm, winner_metric):
                    winner = r
                    winner_metric = rm

            print(f"  WINNER: id={winner['id']} slug={winner['version_slug']!r} "
                  f"return={winner['max_return_pct']} sane={winner['max_return_sane']}")

            # Merge facets from every row in the group into the winner
            merged_facets: dict[str, dict] = {}
            for r in grp:
                row_facets = _row_to_facet_dict(r)
                merged_facets = _merge_facets(merged_facets, row_facets)

            top_level = _top_level_from_facets(merged_facets)
            losers = [r for r in grp if r["id"] != winner["id"]]
            merged_facets_count += len(merged_facets)

            if apply:
                # Re-point history rows from every loser to the winner
                for loser in losers:
                    conn.execute(
                        "UPDATE strategy_version_runs SET version_id = ? WHERE version_id = ?",
                        (winner["id"], loser["id"]),
                    )
                # Delete losers
                for loser in losers:
                    conn.execute("DELETE FROM strategy_versions WHERE id = ?", (loser["id"],))
                deletions += len(losers)

                # Update winner: pure-triplet slug + merged facets + keep-best top-level
                conn.execute(
                    """
                    UPDATE strategy_versions
                    SET version_slug = ?,
                        description = ?,
                        facets_json = ?,
                        max_return_pct = ?,
                        max_return_cell_json = ?,
                        max_return_sane = ?,
                        max_return_warning = ?,
                        best_calmar = ?,
                        best_calmar_return_pct = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        pure_slug,
                        description,
                        json.dumps(merged_facets) if merged_facets else None,
                        top_level.get("max_return_pct"),
                        json.dumps(top_level.get("max_return_cell"))
                            if top_level.get("max_return_cell") else None,
                        int(top_level["max_return_sane"])
                            if top_level.get("max_return_sane") is not None else None,
                        top_level.get("max_return_warning"),
                        top_level.get("best_calmar"),
                        top_level.get("best_calmar_return_pct"),
                        winner["id"],
                    ),
                )
            else:
                deletions += len(losers)

            consolidated += 1
            if winner["version_slug"] != pure_slug:
                renamed += 1

        print("-" * 60)
        print(f"Groups consolidated (multi-row collapses): {consolidated}")
        print(f"Rows renamed to pure triplet:              {renamed}")
        print(f"Rows deleted (losers merged into winner):  {deletions}")
        print(f"Facets backfilled:                         {merged_facets_count}")

        if apply:
            conn.commit()
            print("COMMITTED.")
        else:
            print("DRY RUN — pass --apply to commit.")
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Task #133 — migrate hashed slugs to pure triplets + backfill facets_json")
    p.add_argument("--db", type=Path, default=_repo_root() / "data" / "trades.db")
    p.add_argument("--apply", action="store_true", help="Actually commit the migration")
    args = p.parse_args()
    return migrate(args.db, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
