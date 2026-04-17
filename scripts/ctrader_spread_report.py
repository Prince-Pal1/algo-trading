#!/usr/bin/env python3
"""Bucket cTrader spread samples by trading session and compare to backtest.

Reads all data/ctrader_spread_samples_XAUUSD_*.csv files (or one specified
via --csv) and produces a markdown-formatted calibration report:

  - Per-session spread stats (min/p10/median/avg/p90/max)
  - Cost translation to $/lot/round-trip (using IC's published commission)
  - Comparison vs `ic_markets_ctrader_xauusd_normal` backtest profile
  - Recommended `base_spread_pips` update if avg drift > 10%

Output:
  - stdout: full markdown report
  - data/ctrader_spread_report.md: same content, persisted

Usage:
  python3 scripts/ctrader_spread_report.py
  python3 scripts/ctrader_spread_report.py --csv data/ctrader_spread_samples_XAUUSD_20260420T130000Z.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median


# Trading session buckets in UTC. XAUUSD trades 23/5; gaps are weekend close.
SESSIONS = [
    ("Asian",        0,  7,  "Tokyo/Sydney — thinnest gold liquidity"),
    ("London",       7, 13,  "London open → pre-NY"),
    ("Overlap",     13, 16,  "London/NY overlap — PEAK XAUUSD liquidity"),
    ("NY-only",     16, 21,  "Post-London-close, NY afternoon"),
    ("Off-hours",   21, 24,  "Pre-Asian / weekly close"),
]

# Constants — verified live from cTrader on 2026-04-17:
PIP_SIZE_API = 0.01      # cTrader pipPosition=2 → 1 pip = $0.01/oz (5-digit)
PIP_SIZE_STD = 0.10      # backtest config uses standard 4-digit pip = $0.10/oz
LOT_OZ = 100.0           # standard XAUUSD lot
COMMISSION_USD_PER_LOT_PER_SIDE = lambda mid_px: (mid_px * LOT_OZ / 100_000) * 3.0
# Backtest assumption from config/broker_fees.toml::ic_markets_ctrader_xauusd_normal
BT_BASE_SPREAD_PIPS_STD = 0.30


def load_csvs(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        try:
            with open(p) as f:
                r = csv.DictReader(f)
                for row in r:
                    rows.append({
                        "ts_iso": row["ts_iso"],
                        "ts_unix_ms": int(row["ts_unix_ms"]),
                        "bid": float(row["bid"]),
                        "ask": float(row["ask"]),
                        "spread": float(row["spread"]),
                    })
        except Exception as e:
            print(f"WARN: skipping {p}: {e}", file=sys.stderr)
    return rows


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def bucket_session(ts_unix_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_unix_ms / 1000.0, tz=timezone.utc)
    h = dt.hour
    for name, start, end, _ in SESSIONS:
        if start <= h < end:
            return name
    return "Off-hours"  # belt-and-braces


def fmt_session_row(name: str, spreads: list[float], mid_px: float) -> str:
    if not spreads:
        return f"| {name} | 0 | — | — | — | — | — | — | — |"
    n = len(spreads)
    mn = min(spreads)
    p10 = percentile(spreads, 10)
    med = median(spreads)
    avg = mean(spreads)
    p90 = percentile(spreads, 90)
    mx = max(spreads)
    avg_pips_std = avg / PIP_SIZE_STD
    spread_cost_rt = avg * LOT_OZ * 2  # round-trip
    comm_rt = COMMISSION_USD_PER_LOT_PER_SIDE(mid_px) * 2
    total_rt = spread_cost_rt + comm_rt
    return (
        f"| {name} | {n} | "
        f"${mn:.4f} | ${p10:.4f} | ${med:.4f} | ${avg:.4f} | ${p90:.4f} | ${mx:.4f} | "
        f"{avg_pips_std:.2f} | ${total_rt:.2f} |"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=str, default=None, help="Single CSV path (default: glob all data/ctrader_spread_samples_XAUUSD_*.csv)")
    p.add_argument("--output", type=str, default="data/ctrader_spread_report.md")
    args = p.parse_args()

    if args.csv:
        paths = [args.csv]
    else:
        paths = sorted(glob.glob("data/ctrader_spread_samples_XAUUSD_*.csv"))

    if not paths:
        print("ERROR: no CSV files found. Run scripts/ctrader_spread_sampler.py first.", file=sys.stderr)
        return 1

    rows = load_csvs(paths)
    if not rows:
        print("ERROR: CSV files exist but contain no rows.", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: r["ts_unix_ms"])
    first_dt = datetime.fromtimestamp(rows[0]["ts_unix_ms"] / 1000.0, tz=timezone.utc)
    last_dt = datetime.fromtimestamp(rows[-1]["ts_unix_ms"] / 1000.0, tz=timezone.utc)
    span_h = (rows[-1]["ts_unix_ms"] - rows[0]["ts_unix_ms"]) / 1000 / 3600

    # Average mid price for cost translation
    mids = [(r["bid"] + r["ask"]) / 2.0 for r in rows]
    mid_px = mean(mids)

    # Bucket
    by_session: dict[str, list[float]] = {name: [] for name, *_ in SESSIONS}
    for r in rows:
        s = bucket_session(r["ts_unix_ms"])
        by_session[s].append(r["spread"])

    # Build report
    lines: list[str] = []
    lines.append(f"# XAUUSD Spread Calibration Report")
    lines.append(f"")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"**Source CSV(s):** {len(paths)} file(s) ({', '.join(Path(p).name for p in paths)})")
    lines.append(f"**Tick count:** {len(rows):,}")
    lines.append(f"**Time range:** {first_dt.isoformat()} → {last_dt.isoformat()}  ({span_h:.1f}h span)")
    lines.append(f"**Avg mid price:** ${mid_px:.2f} → notional/lot ${mid_px * LOT_OZ:,.0f}")
    lines.append(f"")
    lines.append(f"## Spread distribution by trading session (UTC)")
    lines.append(f"")
    lines.append(f"| Session | Ticks | Min | p10 | Med | Avg | p90 | Max | Avg pips (std) | Total $/lot RT |")
    lines.append(f"|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, start, end, _desc in SESSIONS:
        lines.append(fmt_session_row(name, by_session[name], mid_px))
    # All-data row
    lines.append(fmt_session_row("**ALL**", [r["spread"] for r in rows], mid_px))
    lines.append(f"")

    # Detail per session
    lines.append(f"## Per-session detail")
    lines.append(f"")
    for name, start, end, desc in SESSIONS:
        spreads = by_session[name]
        lines.append(f"### {name} ({start:02d}:00–{end:02d}:00 UTC) — {desc}")
        lines.append(f"")
        if not spreads:
            lines.append(f"_No ticks captured in this bucket._")
        else:
            avg = mean(spreads)
            spread_cost_rt = avg * LOT_OZ * 2
            comm_rt = COMMISSION_USD_PER_LOT_PER_SIDE(mid_px) * 2
            lines.append(f"- **Tick count:** {len(spreads):,}")
            lines.append(f"- **Avg spread:** ${avg:.4f}/oz = {avg / PIP_SIZE_STD:.2f} std-pips = {avg / PIP_SIZE_API:.2f} pipettes")
            lines.append(f"- **Spread cost / lot / round-trip:** ${spread_cost_rt:.2f}")
            lines.append(f"- **Commission / lot / round-trip:** ${comm_rt:.2f}")
            lines.append(f"- **Total cost / lot / round-trip:** ${spread_cost_rt + comm_rt:.2f}")
        lines.append(f"")

    # Backtest comparison & recommendation
    overlap_spreads = by_session["Overlap"]
    lines.append(f"## Calibration verdict")
    lines.append(f"")
    lines.append(f"**Backtest profile (`ic_markets_ctrader_xauusd_normal`):**")
    lines.append(f"- `base_spread_pips = {BT_BASE_SPREAD_PIPS_STD}` (= ${BT_BASE_SPREAD_PIPS_STD * PIP_SIZE_STD:.4f}/oz = ${BT_BASE_SPREAD_PIPS_STD * PIP_SIZE_STD * LOT_OZ:.2f}/lot/side)")
    lines.append(f"- `commission = $3 per $100k notional` (= ${COMMISSION_USD_PER_LOT_PER_SIDE(mid_px):.4f}/lot/side at current mid)")
    lines.append(f"")
    if overlap_spreads:
        live_avg = mean(overlap_spreads)
        live_pips_std = live_avg / PIP_SIZE_STD
        live_total_rt = (live_avg * LOT_OZ * 2) + (COMMISSION_USD_PER_LOT_PER_SIDE(mid_px) * 2)
        bt_total_rt = (BT_BASE_SPREAD_PIPS_STD * PIP_SIZE_STD * LOT_OZ * 2) + (COMMISSION_USD_PER_LOT_PER_SIDE(mid_px) * 2)
        diff_pct = ((live_total_rt - bt_total_rt) / bt_total_rt) * 100
        lines.append(f"**Live measurement (Overlap session, the relevant comparison):**")
        lines.append(f"- avg spread: {live_pips_std:.2f} std-pips ({live_pips_std / BT_BASE_SPREAD_PIPS_STD:.2f}× the backtest assumption)")
        lines.append(f"- total cost / lot / round-trip: ${live_total_rt:.2f}")
        lines.append(f"- backtest cost / lot / round-trip: ${bt_total_rt:.2f}")
        lines.append(f"- **Δ vs backtest: {diff_pct:+.1f}%**")
        lines.append(f"")
        if abs(diff_pct) <= 10:
            lines.append(f"### ✅ VERDICT: backtest is within 10% of reality — no profile update needed.")
        else:
            recommended_pips = round(live_pips_std, 2)
            lines.append(f"### ⚠️  VERDICT: backtest underestimates cost by {diff_pct:.1f}% — recommend profile update.")
            lines.append(f"")
            lines.append(f"**Recommended change to `config/broker_fees.toml`:**")
            lines.append(f"```toml")
            lines.append(f"[profiles.ic_markets_ctrader_xauusd_normal.spread]")
            lines.append(f"base_spread_pips = {recommended_pips}    # was {BT_BASE_SPREAD_PIPS_STD}, calibrated against {len(overlap_spreads):,} live overlap ticks")
            lines.append(f"```")
            lines.append(f"")
            lines.append(f"After updating, re-validate: `python3 -m scripts.backtest validate donchian_gold --tier standard` and `python3 -m scripts.backtest validate vol_momentum_gold --tier standard`.")
    else:
        lines.append(f"**No Overlap-session ticks captured yet** — calibration verdict requires data from 13:00-16:00 UTC. Wait for the next Monday auto-run.")
        if rows:
            best_bucket_name = max(by_session, key=lambda k: len(by_session[k]))
            best = by_session[best_bucket_name]
            if best:
                bavg = mean(best) / PIP_SIZE_STD
                lines.append(f"")
                lines.append(f"_Provisional read from `{best_bucket_name}` ({len(best):,} ticks): avg {bavg:.2f} std-pips. NOT a substitute for overlap-session data._")

    report = "\n".join(lines) + "\n"
    print(report)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(report)
    print(f"# (also written to {args.output})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
