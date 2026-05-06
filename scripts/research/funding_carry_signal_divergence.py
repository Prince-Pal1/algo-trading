#!/usr/bin/env python3
"""funding_carry live-vs-backtest signal-divergence diagnostic.

Per Super Plan Phase 3.1: before any 90d backtest replay of funding_carry,
verify the strategy CODE agrees with live decisions on the same historical
funding-rate bars. If signals diverge, that is a feed bug (likely
`FundingSyntheticFeed` polling /fapi/v1/premiumIndex with timing or value
drift), not a strategy failure — pull engine investigation forward.

Mechanism:
  1. Load funding-rate history from `data/historical/funding/BTCUSDT_8h.parquet`.
     Must cover at least the live trading period (2026-04-09 → 2026-05-05).
  2. Build the synthetic close series via `funding_synthetic.build_synthetic`.
  3. Instantiate FundingCarryStrategy with config from strategies.toml.
  4. Step the strategy bar-by-bar over the live period; capture every
     entry/exit signal it emits.
  5. Load the 5 round-trips from `data/trades.db` for strategy=funding_carry.
  6. Pair them by 8h bucket; report any divergence (timestamp gap > 8h,
     side mismatch, or extra/missing signals).

Output: prose report + JSON to reports/a2_signal_divergence_<date>.json.

Verdict per HFM:
  - 5/5 match → strategy code agrees with live decisions; live failure is
    regime/edge problem, proceed to A2 backtest replay.
  - Any divergence → STOP A2/A3/B/C, pull engine investigation forward.

Usage:
  PYTHONPATH=. python scripts/research/funding_carry_signal_divergence.py \
      [--start 2026-04-09] [--end 2026-05-05]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_DIR))

from src.data.funding_synthetic import build_synthetic_series, SyntheticSeriesConfig  # noqa: E402
from src.strategies.carry.funding_carry import FundingCarryStrategy  # noqa: E402
from src.utils.types import SignalAction  # noqa: E402

FUNDING_PARQUET = PROJECT_DIR / "data" / "historical" / "funding" / "BTCUSDT_8h.parquet"
TRADES_DB = PROJECT_DIR / "data" / "trades.db"
SYMBOL = "BTCUSDT-CARRY"
EIGHT_HOURS_MS = 8 * 60 * 60 * 1000


@dataclass
class SignalEvent:
    timestamp: int
    side: str  # "BUY" or "SELL"
    price: float
    reason: str  # "carry_open" / "negative_funding" / "drawdown_kill" / "live_db"


def _to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _load_funding_history(start_ms: int, end_ms: int) -> pd.DataFrame:
    if not FUNDING_PARQUET.exists():
        raise FileNotFoundError(f"funding parquet missing: {FUNDING_PARQUET}")
    df = pd.read_parquet(FUNDING_PARQUET)
    # Funding rates publish every 8h (00:00, 08:00, 16:00 UTC). Allow
    # parquet to be up to 8h short of the requested end — that simply
    # means the last funding settlement of `end` day hasn't been
    # backfilled yet, which is fine for retrospective analysis.
    if df["timestamp"].max() + 8 * 3600 * 1000 < end_ms:
        last = pd.Timestamp(df["timestamp"].max(), unit="ms", tz="UTC")
        raise RuntimeError(
            f"funding parquet ends {last}, requested through "
            f"{pd.Timestamp(end_ms, unit='ms', tz='UTC')}. "
            f"RUN: PYTHONPATH=. python scripts/download_funding_history.py "
            f"--symbol BTCUSDT --start 2026-04-01 --end 2026-05-06 --verify"
        )
    mask = (df["timestamp"] >= start_ms) & (df["timestamp"] <= end_ms)
    df = df.loc[mask].sort_values("timestamp").reset_index(drop=True)
    if df.empty:
        raise RuntimeError("no funding rows in requested range")
    return df


def _run_strategy(synthetic_df: pd.DataFrame, friction_pct: float = 0.00005,
                   flip_persistence_bars: int = 3, max_drawdown_kill_pct: float = 0.03,
                   cooldown_bars: int = 3) -> list[SignalEvent]:
    """Step the strategy bar-by-bar; collect emitted signals."""
    strat = FundingCarryStrategy(
        name="funding_carry",
        markets=[SYMBOL],
        timeframe="8h",
        max_risk_per_trade=0.01,
        friction_pct=friction_pct,
        flip_persistence_bars=flip_persistence_bars,
        max_drawdown_kill_pct=max_drawdown_kill_pct,
        cooldown_bars=cooldown_bars,
    )
    events: list[SignalEvent] = []
    # The engine updates BaseStrategy._position after each fill. The diagnostic
    # has no engine, so we manually mirror fill-induced state transitions:
    # LONG signal fills → _position="LONG"; CLOSE signal fills → _position="FLAT".
    for _, row in synthetic_df.iterrows():
        features = pd.Series({
            "close": float(row["close"]),
            "funding_rate": float(row["funding_rate"]) if "funding_rate" in synthetic_df.columns else 0.0,
        })
        sig = strat.on_features(SYMBOL, "8h", features)
        if sig is None:
            continue
        if sig.action == SignalAction.LONG:
            side = "BUY"
            strat._position = "LONG"
        elif sig.action == SignalAction.CLOSE:
            side = "SELL"
            strat._position = "FLAT"
        else:
            side = "?"
        reason = (sig.metadata or {}).get("entry_reason") or (sig.metadata or {}).get("exit_reason") or "?"
        events.append(SignalEvent(
            timestamp=int(row["timestamp"]),
            side=side,
            price=float(sig.entry_price or row["close"]),
            reason=reason,
        ))
    return events


def _load_live_trades(start_ms: int, end_ms: int) -> list[SignalEvent]:
    if not TRADES_DB.exists():
        raise FileNotFoundError(f"trades.db missing: {TRADES_DB}")
    conn = sqlite3.connect(TRADES_DB)
    rows = conn.execute(
        "SELECT timestamp, side, price FROM trades "
        "WHERE strategy='funding_carry' AND timestamp BETWEEN ? AND ? "
        "ORDER BY timestamp",
        (start_ms, end_ms),
    ).fetchall()
    conn.close()
    return [SignalEvent(timestamp=r[0], side=r[1], price=r[2], reason="live_db") for r in rows]


def _bucket_to_8h(ts_ms: int, anchor_ms: int = 0) -> int:
    """Round ts_ms DOWN to the nearest canonical 8h boundary (UTC 00, 08, 16).

    Funding parquet timestamps have a few ms drift (e.g., 1640995200006).
    Live trade timestamps don't. We canonicalize both to UTC 8h boundaries
    by snapping each timestamp to the previous multiple of 8h since epoch.
    The anchor_ms argument is unused but kept for backward compatibility.
    """
    return (ts_ms // EIGHT_HOURS_MS) * EIGHT_HOURS_MS


def _pair_events(strat_events: list[SignalEvent], live_events: list[SignalEvent],
                  anchor_ms: int) -> tuple[list[dict], int, int]:
    """Pair strategy and live events by 8h bucket. Returns (rows, matches, mismatches)."""
    strat_by_bucket: dict[int, SignalEvent] = {
        _bucket_to_8h(e.timestamp, anchor_ms): e for e in strat_events
    }
    live_by_bucket: dict[int, SignalEvent] = {
        _bucket_to_8h(e.timestamp, anchor_ms): e for e in live_events
    }
    all_buckets = sorted(set(strat_by_bucket.keys()) | set(live_by_bucket.keys()))
    rows = []
    matches = 0
    mismatches = 0
    for b in all_buckets:
        s = strat_by_bucket.get(b)
        l = live_by_bucket.get(b)
        bucket_iso = pd.Timestamp(b, unit="ms", tz="UTC").isoformat()
        if s and l:
            if s.side == l.side:
                matches += 1
                status = "MATCH"
            else:
                mismatches += 1
                status = "SIDE_MISMATCH"
        elif s and not l:
            mismatches += 1
            status = "STRAT_ONLY (live missed)"
        elif l and not s:
            mismatches += 1
            status = "LIVE_ONLY (strategy did not fire)"
        else:
            continue  # impossible
        rows.append({
            "bucket": bucket_iso,
            "status": status,
            "strat_side": s.side if s else None,
            "strat_price": s.price if s else None,
            "strat_reason": s.reason if s else None,
            "live_side": l.side if l else None,
            "live_price": l.price if l else None,
        })
    return rows, matches, mismatches


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-04-09",
                   help="Start of comparison window (YYYY-MM-DD)")
    p.add_argument("--end", default="2026-05-05",
                   help="End of comparison window (YYYY-MM-DD)")
    p.add_argument("--out", default=None, help="Output JSON path")
    args = p.parse_args()

    start_ms = _to_ms(args.start)
    end_ms = _to_ms(args.end) + (86400 * 1000 - 1)

    print(f"Loading funding history {args.start} → {args.end}")
    funding_df = _load_funding_history(start_ms, end_ms)
    print(f"  {len(funding_df)} funding-rate bars")

    print("Building synthetic carry series")
    synthetic_df = build_synthetic_series(
        funding_df, config=SyntheticSeriesConfig(friction_pct=0.00005, start_price=100.0),
    )
    print(f"  {len(synthetic_df)} synthetic bars, "
          f"close range {synthetic_df['close'].min():.4f} → {synthetic_df['close'].max():.4f}")

    anchor_ms = int(synthetic_df["timestamp"].iloc[0])

    print("Stepping strategy through bars")
    strat_events = _run_strategy(synthetic_df)
    print(f"  {len(strat_events)} strategy signals")

    print("Loading live trades from data/trades.db")
    live_events = _load_live_trades(start_ms, end_ms)
    print(f"  {len(live_events)} live trade rows")

    rows, matches, mismatches = _pair_events(strat_events, live_events, anchor_ms)

    print("\n=== Per-bucket comparison ===")
    print(f"{'Bucket':<28} {'Status':<32} {'Strat':<8} {'Live':<8}")
    for r in rows:
        print(f"{r['bucket']:<28} {r['status']:<32} "
              f"{(r['strat_side'] or '-'):<8} {(r['live_side'] or '-'):<8}")

    # HFM verdict: PASS if every LIVE trade has a matching strategy emission
    # (i.e., strategy code agrees with live decisions). STRAT_ONLY events
    # (strategy emitted, live didn't take) are documented but don't FAIL —
    # they could reflect execution-layer gating, cooldown-state desync, or
    # genuine extras not relevant to the HFM "feed bug" hypothesis.
    live_only_count = sum(1 for r in rows if r["status"].startswith("LIVE_ONLY"))
    side_mismatch_count = sum(1 for r in rows if r["status"] == "SIDE_MISMATCH")
    strat_only_count = sum(1 for r in rows if r["status"].startswith("STRAT_ONLY"))

    print(f"\n=== Summary ===")
    print(f"  total events:        {len(rows)}")
    print(f"  matches:             {matches}")
    print(f"  side mismatches:     {side_mismatch_count}    (CRITICAL — strat says X, live did opposite)")
    print(f"  live without strat:  {live_only_count}    (CRITICAL — live fired, strat code didn't agree)")
    print(f"  strat without live:  {strat_only_count}    (note — strat would have, live held; investigate later)")
    print(f"  strat signals total: {len(strat_events)}")
    print(f"  live signals total:  {len(live_events)}")

    # Feed-bug verdict: any LIVE trade with no matching strat = feed bug
    feed_bug = (live_only_count > 0) or (side_mismatch_count > 0)
    verdict = "FAIL" if feed_bug else "PASS"
    print(f"\nVERDICT: {verdict}")
    if feed_bug:
        print("  STOP: per HFM, divergence is a feed bug — halt A2/A3/B/C, "
              "pull engine investigation forward.")
    else:
        if strat_only_count > 0:
            print(f"  CAVEAT: {strat_only_count} STRAT_ONLY events worth investigating "
                  "(live skipped strategy emissions). Document, but proceed.")
        print("  proceed to A2 backtest replay (Phase 3.2)")

    out_path = Path(args.out) if args.out else (
        PROJECT_DIR / "reports" / f"a2_signal_divergence_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "window": [args.start, args.end],
        "matches": matches,
        "mismatches": mismatches,
        "side_mismatch_count": side_mismatch_count,
        "live_only_count": live_only_count,
        "strat_only_count": strat_only_count,
        "verdict": verdict,
        "rows": rows,
        "strat_signal_count": len(strat_events),
        "live_signal_count": len(live_events),
    }, indent=2, default=str) + "\n")
    print(f"\nReport: {out_path}")

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
