#!/usr/bin/env python3
"""Close orphaned paper positions whose owning strategy is now disabled.

Created 2026-05-05 after Session 24 strategy triage disabled
vol_momentum_gold + funding_carry + donchian_ensemble_adx, leaving 3 open
positions with no strategy able to emit exit signals.

What it does (per DB, per orphaned position):
  1. Read position from `paper_positions` (entry_price, current_price, qty, side, strategy)
  2. Compute net P&L exactly like paper_executor._close_position:
       slippage_pct = 0.0005 default
       commission_pct = 0.0010 default
       BUY  → fill_price = current * (1 - slippage); pnl = (fill - entry) * qty
       SELL → fill_price = current * (1 + slippage); pnl = (entry - fill) * qty
       commission = fill_price * qty * commission_pct
       net_pnl = pnl - commission
  3. INSERT closing trade row in `trades` (opposite side at fill_price)
  4. DELETE row from `paper_positions`
  5. UPDATE `paper_equity` (realized_pnl applied to equity)

SAFETY:
  - PREREQUISITE: stop the engine that owns the DB before running, otherwise
    the engine's in-memory `_positions` dict will overwrite our changes on
    its next persist. Use:
        launchctl unload ~/Library/LaunchAgents/com.algo-trading.engine.plist
        launchctl unload ~/Library/LaunchAgents/com.algo-trading.engine-gold.plist
  - DRY-RUN by default. Pass --apply to actually mutate.
  - Re-startable: writes a backup of the DB before mutating.

Usage:
    # Show what would be closed
    python scripts/operational/close_orphan_positions.py

    # Actually close them
    launchctl unload ~/Library/LaunchAgents/com.algo-trading.engine.plist
    launchctl unload ~/Library/LaunchAgents/com.algo-trading.engine-gold.plist
    python scripts/operational/close_orphan_positions.py --apply
    launchctl load ~/Library/LaunchAgents/com.algo-trading.engine.plist
    launchctl load ~/Library/LaunchAgents/com.algo-trading.engine-gold.plist
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]

# Strategies considered orphaned (disabled in strategies.toml during 2026-05-05 triage).
ORPHANED_STRATEGIES = {
    "vol_momentum_gold",
    "funding_carry",
    "donchian_ensemble_adx",
}

DBS = [
    PROJECT_DIR / "data" / "trades.db",
    PROJECT_DIR / "data" / "trades_gold.db",
]

# Paper-executor defaults (paper_executor.py constructor).
SLIPPAGE_PCT = 0.0005
COMMISSION_PCT = 0.0010


def now_ms() -> int:
    return int(time.time() * 1000)


def backup_db(db_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_suffix(f".db.bak.{ts}")
    shutil.copy2(db_path, backup)
    return backup


def compute_close(side: str, qty: float, entry: float, current: float) -> tuple[float, float, str, float, float]:
    """Returns (fill_price, gross_pnl, opposite_side, commission, net_pnl)."""
    if side.upper() == "BUY":
        fill_price = current * (1.0 - SLIPPAGE_PCT)
        gross_pnl = (fill_price - entry) * qty
        opp = "SELL"
    elif side.upper() == "SELL":
        fill_price = current * (1.0 + SLIPPAGE_PCT)
        gross_pnl = (entry - fill_price) * qty
        opp = "BUY"
    else:
        raise ValueError(f"Unknown side: {side}")
    commission = fill_price * qty * COMMISSION_PCT
    net_pnl = gross_pnl - commission
    return fill_price, gross_pnl, opp, commission, net_pnl


def process_db(db_path: Path, apply: bool) -> dict:
    if not db_path.exists():
        return {"db": str(db_path), "skipped": "missing"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, side, quantity, entry_price, current_price, "
        "unrealized_pnl, strategy_name, opened_at FROM paper_positions"
    ).fetchall()
    if not rows:
        conn.close()
        return {"db": str(db_path), "open_positions": 0}

    plan = []
    for r in rows:
        if r["strategy_name"] not in ORPHANED_STRATEGIES:
            continue
        fill_price, gross, opp, comm, net = compute_close(
            r["side"], r["quantity"], r["entry_price"], r["current_price"],
        )
        plan.append({
            "symbol": r["symbol"],
            "strategy": r["strategy_name"],
            "side": r["side"],
            "qty": r["quantity"],
            "entry": r["entry_price"],
            "current": r["current_price"],
            "fill_price": fill_price,
            "gross_pnl": gross,
            "commission": comm,
            "net_pnl": net,
            "close_side": opp,
        })

    if not plan:
        conn.close()
        return {"db": str(db_path), "open_positions": len(rows), "orphaned": 0}

    if apply:
        backup = backup_db(db_path)
        ts = now_ms()
        total_realized = 0.0
        for p in plan:
            order_id = f"paper-orphan-{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO trades (order_id, timestamp, symbol, side, price, "
                "quantity, commission, exchange, strategy) VALUES (?,?,?,?,?,?,?,?,?)",
                (order_id, ts, p["symbol"], p["close_side"], p["fill_price"],
                 p["qty"], p["commission"], "paper", p["strategy"]),
            )
            conn.execute(
                "DELETE FROM paper_positions WHERE symbol = ?", (p["symbol"],),
            )
            total_realized += p["net_pnl"]

        # Apply realized pnl to paper_equity (id=1).
        eq_row = conn.execute(
            "SELECT equity, initial_capital, trade_count FROM paper_equity WHERE id = 1",
        ).fetchone()
        if eq_row:
            new_equity = eq_row["equity"] + total_realized
            new_count = eq_row["trade_count"] + len(plan)
            conn.execute(
                "UPDATE paper_equity SET equity = ?, trade_count = ? WHERE id = 1",
                (new_equity, new_count),
            )

        conn.commit()
        result = {
            "db": str(db_path),
            "applied": True,
            "backup": str(backup),
            "closed_count": len(plan),
            "realized_pnl": round(total_realized, 4),
            "plan": plan,
        }
    else:
        result = {
            "db": str(db_path),
            "applied": False,
            "closed_count": len(plan),
            "would_realize_pnl": round(sum(p["net_pnl"] for p in plan), 4),
            "plan": plan,
        }

    conn.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually mutate the DBs (default: dry-run).")
    args = parser.parse_args()

    if args.apply:
        # Refuse if any target engine looks running (simple PID check via launchctl).
        import subprocess
        running = []
        for label in ("com.algo-trading.engine", "com.algo-trading.engine-gold"):
            try:
                out = subprocess.run(
                    ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                    check=False, capture_output=True, text=True, timeout=5,
                )
                if "state = running" in out.stdout:
                    running.append(label)
            except Exception:
                pass
        if running:
            print(f"REFUSED: engine(s) currently running: {running}", file=sys.stderr)
            print("Stop them with `launchctl unload ~/Library/LaunchAgents/<label>.plist` first.",
                  file=sys.stderr)
            return 2

    results = []
    for db in DBS:
        results.append(process_db(db, args.apply))

    import json
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
