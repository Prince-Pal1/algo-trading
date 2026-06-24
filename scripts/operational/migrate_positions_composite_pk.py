#!/usr/bin/env python3
"""Migrate paper_positions from symbol-only PK to (strategy_name, symbol) composite PK.

Lets multiple strategies hold the same symbol concurrently in paper trading
(Session 30 — adaptive_momentum alongside vol_momentum). SQLite can't ALTER a
primary key, so we recreate the table and copy rows. Idempotent: a no-op if the
composite PK is already present. Backs up the DB before touching it.

Usage:
    python -m scripts.operational.migrate_positions_composite_pk --db data/trades.db
    python -m scripts.operational.migrate_positions_composite_pk --db data/trades_gold.db --apply
Without --apply it runs a dry run (reports what it would do).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

NEW_SCHEMA = """
CREATE TABLE paper_positions_new (
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    unrealized_pnl REAL DEFAULT 0,
    strategy_name TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    PRIMARY KEY (strategy_name, symbol)
);
"""


def _pk_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(paper_positions)").fetchall()
    # row = (cid, name, type, notnull, dflt, pk)  pk>0 means part of PK
    return [r[1] for r in rows if r[5] > 0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true", help="actually perform the migration")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_positions'"
        ).fetchone()
        if not tbl:
            print(f"[skip] {args.db}: no paper_positions table")
            return 0

        pk = _pk_columns(conn)
        n_rows = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
        n_null = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE strategy_name IS NULL OR strategy_name=''"
        ).fetchone()[0]
        print(f"[{args.db}] current PK={pk} rows={n_rows} null_strategy={n_null}")

        if set(pk) == {"strategy_name", "symbol"}:
            print("[ok] already composite-keyed — nothing to do")
            return 0
        if n_null:
            print(f"[ABORT] {n_null} rows have empty strategy_name — cannot form composite PK. "
                  "Inspect/backfill manually first.")
            return 2

        if not args.apply:
            print("[dry-run] would: backup DB, recreate paper_positions with "
                  "PRIMARY KEY(strategy_name, symbol), copy rows. Re-run with --apply.")
            return 0

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = f"{args.db}.bak.composite_pk.{ts}"
        shutil.copy2(args.db, backup)
        print(f"[backup] {backup}")

        conn.execute("BEGIN")
        conn.executescript(NEW_SCHEMA)
        conn.execute(
            "INSERT INTO paper_positions_new "
            "(symbol, side, quantity, entry_price, current_price, unrealized_pnl, "
            " strategy_name, opened_at) "
            "SELECT symbol, side, quantity, entry_price, current_price, unrealized_pnl, "
            "       strategy_name, opened_at FROM paper_positions"
        )
        conn.execute("DROP TABLE paper_positions")
        conn.execute("ALTER TABLE paper_positions_new RENAME TO paper_positions")
        conn.commit()

        after_pk = _pk_columns(conn)
        after_rows = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
        print(f"[done] new PK={after_pk} rows={after_rows} (was {n_rows})")
        if after_rows != n_rows:
            print("[WARN] row count changed — check the backup!")
            return 2
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
