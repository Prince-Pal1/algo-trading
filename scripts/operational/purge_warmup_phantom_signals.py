"""One-shot purge of warmup-phantom rows from the `signals` table.

Background (2026-06-11 investigation): before the warmup signal-suppression
fix, every engine restart replayed historical candles through the
StrategyRouter with storage attached. Signals emitted during that replay
were written to the live `signals` table at stale prices — phantom rows
that poison any analytics computed from the table. See
docs/investigations/2026-06-11_vol_momentum_stale_warmup.md.

Identification rule (validated against both DBs on 2026-06-12):
    phantom :=  (timestamp % 3600000) >= 120000          -- >2 min into the hour
            AND no signal_audit row matches (strategy, symbol, signal_ts_ms)

Why both conditions:
  * Live candle-close signals are emitted within seconds of the hour
    boundary on the crypto engine, but the gold engine's cTrader trendbars
    arrive minutes late — offset alone would wrongly purge ~200 live gold
    rows. The signal_audit table is written ONLY on the live path (never
    during warmup replay), so audit linkage proves liveness.
  * ~30 crypto rows predate the signal_audit table (Phase 3c). The offset
    condition keeps those.

Usage:
    python3 scripts/operational/purge_warmup_phantom_signals.py            # dry run
    python3 scripts/operational/purge_warmup_phantom_signals.py --apply   # backup + delete
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DBS = ["data/trades.db", "data/trades_gold.db"]

PHANTOM_WHERE = """
    timestamp % 3600000 >= 120000
    AND NOT EXISTS (
        SELECT 1 FROM signal_audit a
        WHERE a.run_id = 'live'
          AND a.strategy = signals.strategy
          AND a.symbol = signals.symbol
          AND a.signal_ts_ms = signals.timestamp
    )
"""


def purge(db_path: str, apply: bool) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        f"SELECT * FROM signals WHERE {PHANTOM_WHERE} ORDER BY timestamp"
    ).fetchall()

    print(f"\n=== {db_path}: {len(rows)} phantom rows ===")
    by_key: dict[tuple[str, str], int] = {}
    for r in rows:
        k = (r["strategy"], r["action"])
        by_key[k] = by_key.get(k, 0) + 1
    for (strat, action), n in sorted(by_key.items()):
        print(f"  {strat:24s} {action:6s} {n}")

    # Safety: a phantom row must never be referenced by a trade.
    linked = con.execute(
        f"SELECT COUNT(*) FROM trades t JOIN signals s ON t.signal_id = s.id "
        f"WHERE s.timestamp % 3600000 >= 120000 AND NOT EXISTS ("
        f"  SELECT 1 FROM signal_audit a WHERE a.run_id='live' "
        f"  AND a.strategy = s.strategy AND a.symbol = s.symbol "
        f"  AND a.signal_ts_ms = s.timestamp)"
    ).fetchone()[0]
    if linked:
        print(f"  ABORT: {linked} phantom candidates are referenced by trades")
        con.close()
        return 1

    if not apply:
        print("  (dry run — pass --apply to backup + delete)")
        con.close()
        return 0

    if rows:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{db_path}.bak.phantom_purge.{stamp}"
        shutil.copy2(db_path, backup)
        print(f"  backup: {backup}")

        csv_path = Path("data") / f"purged_phantom_signals_{Path(db_path).stem}_{stamp}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            writer.writerows([tuple(r) for r in rows])
        print(f"  audit trail: {csv_path}")

        cur = con.execute(f"DELETE FROM signals WHERE {PHANTOM_WHERE}")
        con.commit()
        print(f"  deleted: {cur.rowcount}")
    con.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="backup + delete")
    parser.add_argument("--db", action="append", help="restrict to specific DB path(s)")
    args = parser.parse_args()

    rc = 0
    for db in (args.db or DBS):
        if not Path(db).exists():
            print(f"skip (missing): {db}")
            continue
        rc |= purge(db, args.apply)
    return rc


if __name__ == "__main__":
    sys.exit(main())
