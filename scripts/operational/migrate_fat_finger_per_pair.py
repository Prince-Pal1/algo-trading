"""One-shot migration: backfill RiskState.fat_finger_avg_pairs from
historical fills in the trades table.

Replays every fill chronologically through Welford's running mean, grouped
by (strategy, symbol). Writes the result to risk_state under the new
fat_finger_avg_pairs key, and deletes the legacy scalar keys.

Why: the pre-2026-05-09 design held a single global running average
across all strategies and symbols. That average was poisoned by
small-qty fills (funding_carry BTCUSDT-CARRY ~5–31, donchian
ETH ~0.4–12) and silently rejected every vol_momentum signal
(qty 300–3,600) for ~11 days. See ARCHITECTURE.md gotcha 2026-05-09.

Usage:
    python -m scripts.operational.migrate_fat_finger_per_pair --db data/trades.db --apply
    python -m scripts.operational.migrate_fat_finger_per_pair --db data/trades_gold.db --apply

Without --apply, runs in dry-run mode and prints the per-pair averages
that would be written.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import orjson


def compute_pair_averages(db_path: str) -> dict[str, tuple[float, int]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT strategy, symbol, quantity FROM trades "
        "WHERE strategy IS NOT NULL AND strategy != '' "
        "ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()

    pairs: dict[str, tuple[float, int]] = {}
    for strategy, symbol, qty in rows:
        key = f"{strategy}/{symbol}"
        avg, count = pairs.get(key, (0.0, 0))
        count += 1
        avg += (qty - avg) / count
        pairs[key] = (avg, count)
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="SQLite path (data/trades.db or data/trades_gold.db)")
    ap.add_argument("--apply", action="store_true", help="Write to disk (default: dry-run)")
    args = ap.parse_args()

    pairs = compute_pair_averages(args.db)
    if not pairs:
        print(f"No fills found in {args.db}; nothing to migrate.")
        return 0

    print(f"Per-(strategy, symbol) averages from {args.db}:")
    for key, (avg, count) in sorted(pairs.items()):
        print(f"  {key:45s}  avg={avg:>12.4f}  count={count}")

    if not args.apply:
        print("\n(dry-run) Re-run with --apply to write.")
        return 0

    payload = orjson.dumps({k: [v[0], v[1]] for k, v in pairs.items()}).decode()
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(args.db)
    conn.execute(
        "INSERT OR REPLACE INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("fat_finger_avg_pairs", payload, now),
    )
    conn.execute(
        "DELETE FROM risk_state WHERE key IN "
        "('fat_finger_avg_trade_size', 'fat_finger_trade_count')"
    )
    conn.commit()
    conn.close()

    print(f"\nWrote {len(pairs)} per-pair entries to {args.db}::risk_state.fat_finger_avg_pairs")
    print("Removed legacy scalar keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
