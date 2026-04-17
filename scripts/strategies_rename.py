"""One-off utility to populate human-readable descriptions for existing
strategy_versions rows (post task #118 backfill).

Run once after the 16 historical reports were backfilled with empty
descriptions. Future deep_backtest runs auto-populate description from the
strategy class docstring, but the backfill didn't have BaseStrategy instances
to introspect, so historical rows got None descriptions.

This script reads each (strategy_name, version_slug) pair from a hardcoded
renaming table and UPSERTs the description field. Idempotent — re-running
is harmless. Pass --dry-run to preview without writing.

Design: this is a ONE-OFF migration for the 11 existing rows. Future variants
should get descriptions either from the strategy class docstring (via
record_deep_backtest_result auto-introspection) or via
`scripts/strategies.py register --description X`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategies.storage import (  # noqa: E402
    StoredVersion,
    get_version,
    upsert_version,
)


# (strategy_name, version_slug) → human description
_RENAMES: list[tuple[str, str, str]] = [
    # donchian_gold
    ("donchian_gold", "1h",
     "quick 1h smoke — 2 cells, default params, no leverage mode"),
    ("donchian_gold", "1h_4f0136",
     "session-filtered full matrix — 60 cells, no leverage mode, DEPLOYABLE"),
    ("donchian_gold", "30m",
     "30m timeframe smoke test — 1 cell, default params"),
    ("donchian_gold", "risk_scaled_L10_1h",
     "G.8 smoke test — RISK_SCALED baseline=10, default params, 2 cells"),
    ("donchian_gold", "risk_scaled_L10_1h_317b7c",
     "conservative 0.5% risk — RISK_SCALED baseline=10 + session filter, 3 cells"),
    ("donchian_gold", "risk_scaled_L10_1h_f6e428",
     "⭐ HERO DEPLOYMENT — 2% risk + session filter, baseline=10, DEPLOYABLE at L=15/20, WF Calmar 11.7, +140%/yr"),

    # swift_alma
    ("swift_alma", "5m",
     "Pine-port validation — 5m matrix, 16 cells, Pine-faithful zero-cost fees"),

    # vol_momentum_gold
    ("vol_momentum_gold", "1h",
     "default 1h matrix — 4 cells, defaults, DEPLOYABLE WF Calmar 4.9"),
    ("vol_momentum_gold", "1h_0a8044",
     "long-only + session-filtered full matrix — 60 cells, DEPLOYABLE WF Calmar 6.3"),
    ("vol_momentum_gold", "margin_capped_L10_1h_bf21a9",
     "G.8 smoke test — MARGIN_CAPPED baseline=10, default vol_target, 3 cells"),
    ("vol_momentum_gold", "risk_scaled_L10_1h",
     "⚠ task #116 research-fail — RISK_SCALED + vol_target=0.3 + 1% risk, fails 100%/yr return-biased gate"),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rename existing strategy_versions descriptions.")
    ap.add_argument("--db", default="data/trades.db", help="SQLite DB path")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = ap.parse_args(argv)

    updated = 0
    missing = 0
    for strategy_name, slug, desc in _RENAMES:
        v = get_version(strategy_name, slug, db_path=args.db)
        if v is None:
            print(f"  [skip] {strategy_name}/{slug}: not in DB", file=sys.stderr)
            missing += 1
            continue

        if args.dry_run:
            print(f"  [dry] {strategy_name}/{slug}")
            print(f"        → {desc}")
            updated += 1
            continue

        # Use upsert_version with only the fields we want to change. Due to
        # the do-not-clobber semantics, passing None for other fields preserves
        # them. We build a minimal StoredVersion shell and rely on the
        # description being a non-None value (so it gets applied).
        stub = StoredVersion(
            strategy_id=v.strategy_id,
            version_slug=v.version_slug,
            description=desc,
        )
        upsert_version(stub, db_path=args.db)
        print(f"  [ok]  {strategy_name}/{slug}")
        print(f"        → {desc}")
        updated += 1

    if missing:
        print(f"\n{updated} updated, {missing} missing (not in DB)", file=sys.stderr)
    else:
        print(f"\n{updated} updated" + (" (dry-run, nothing written)" if args.dry_run else ""))
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
